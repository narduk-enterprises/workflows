#!/usr/bin/env python3
"""Execute the CI artifact store client against AWS's SigV4 vectors and a local S3 double.

The org's GitHub Actions artifact storage quota turned estate CI red on
2026-09-26, and the owner chose to move the prebuilt E2E application and the
reuse proofs to an R2 bucket the estate owns. nuxt-cloudflare.yml writes the
client from its own text (the `Install CI artifact store client` step) and
loads it in github-script. This test runs that exact step under bash, then
drives the module it wrote with Node against:

  - AWS's published Signature V4 examples, which an independent Python signer
    below must reproduce too;
  - a local HTTP double of R2's S3 API that authenticates every request with
    that independent signer (header-signed and presigned alike) and refuses
    unsigned, mis-signed, expired and payload-mismatched requests.

It also holds the workflow to the decision itself: no GitHub artifact action
and no artifact API anywhere in nuxt-cloudflare.yml, and no artifact upload in
reusable-browser-tests.yml.

Run: python3 scripts/test_ci_artifact_store.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
import urllib.parse

import yaml

WORKFLOW = Path(".github/workflows/nuxt-cloudflare.yml")
BROWSER_WORKFLOW = Path(".github/workflows/reusable-browser-tests.yml")
INSTALL = "Install CI artifact store client"
BUCKET = "narduk-ci-artifacts"
ACCOUNT = "0123456789abcdef0123456789abcdef"
ACCESS = "fedcba9876543210fedcba9876543210"
SECRET = "ab" * 32
HOST = f"{ACCOUNT}.r2.cloudflarestorage.com"
CREDS = {
    "CI_ARTIFACTS_R2_ACCOUNT_ID": ACCOUNT,
    "CI_ARTIFACTS_R2_ACCESS_KEY_ID": ACCESS,
    "CI_ARTIFACTS_R2_SECRET_ACCESS_KEY": SECRET,
}
REQUIRED_KEY = "required-proof-v1-" + "1" * 64
E2E_KEY = "e2e-proof-v1-" + "2" * 64


# ---- An independent Signature V4 implementation --------------------------

def rfc3986(value: str) -> str:
    return urllib.parse.quote(value, safe="-_.~")


def signature(secret: str, stamp: str, region: str, service: str, method: str, uri: str,
              query: list[tuple[str, str]], headers: dict[str, str], payload_hash: str) -> str:
    names = sorted(headers)
    canonical = "\n".join([
        method, uri,
        "&".join(f"{k}={v}" for k, v in sorted((rfc3986(k), rfc3986(v)) for k, v in query)),
        "".join(f"{name}:{' '.join(str(headers[name]).split())}\n" for name in names),
        ";".join(names), payload_hash,
    ])
    scope = f"{stamp[:8]}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret).encode()
    for part in (stamp[:8], region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()


def parse_stamp(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


# ---- A local double of R2's S3 API ---------------------------------------

class Double:
    """Path-style S3 on 127.0.0.1 that authenticates like R2 does."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.read_override: dict[str, bytes] = {}
        self.fail: list[int] = []
        self.log: list[tuple[str, str, str]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reset(self) -> None:
        self.objects.clear()
        self.read_override.clear()
        self.fail.clear()
        self.log.clear()

    def authenticate(self, method: str, raw_path: str, raw_query: str, headers, body: bytes) -> str:
        query = urllib.parse.parse_qsl(raw_query, keep_blank_values=True)
        params = dict(query)
        if "X-Amz-Signature" in params:
            stamp = params.get("X-Amz-Date", "")
            if method != "GET" or params.get("X-Amz-Algorithm") != "AWS4-HMAC-SHA256" \
                    or params.get("X-Amz-SignedHeaders") != "host" \
                    or params.get("X-Amz-Credential") != f"{ACCESS}/{stamp[:8]}/auto/s3/aws4_request":
                return "AuthorizationQueryParametersError"
            expires = int(params.get("X-Amz-Expires", "0"))
            if not 0 < expires <= 604800 or datetime.now(timezone.utc) > parse_stamp(stamp) + timedelta(seconds=expires):
                return "AccessDenied"
            unsigned = [(k, v) for k, v in query if k != "X-Amz-Signature"]
            expected = signature(SECRET, stamp, "auto", "s3", "GET", raw_path, unsigned, {"host": HOST}, "UNSIGNED-PAYLOAD")
            return "presigned" if hmac.compare_digest(expected, params["X-Amz-Signature"]) else "SignatureDoesNotMatch"
        match = re.fullmatch(
            r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/auto/s3/aws4_request, "
            r"SignedHeaders=([a-z0-9;-]+), Signature=([0-9a-f]{64})", headers.get("Authorization", ""))
        if not match:
            return "AccessDenied"
        access, date, signed, sent = match.groups()
        stamp = headers.get("x-amz-date", "")
        names = signed.split(";")
        if access != ACCESS:
            return "InvalidAccessKeyId"
        if not {"host", "x-amz-date", "x-amz-content-sha256"} <= set(names) or stamp[:8] != date:
            return "AccessDenied"
        if abs(datetime.now(timezone.utc) - parse_stamp(stamp)) > timedelta(minutes=15):
            return "RequestTimeTooSkewed"
        payload_hash = headers.get("x-amz-content-sha256", "")
        if payload_hash != hashlib.sha256(body).hexdigest():
            return "XAmzContentSHA256Mismatch"
        values = {name: HOST if name == "host" else headers.get(name, "") for name in names}
        expected = signature(SECRET, stamp, "auto", "s3", method, raw_path, query, values, payload_hash)
        return "header" if hmac.compare_digest(expected, sent) else "SignatureDoesNotMatch"

    def _handler(self):
        double = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # noqa: D401 - quiet
                pass

            def reply(self, status: int, body: bytes = b"") -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def error(self, status: int, code: str) -> None:
                self.reply(status, f"<?xml version=\"1.0\"?><Error><Code>{code}</Code></Error>".encode())

            def serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                raw_path, _, raw_query = self.path.partition("?")
                if not raw_path.startswith(f"/{BUCKET}/"):
                    return self.error(404, "NoSuchBucket")
                key = urllib.parse.unquote(raw_path[len(BUCKET) + 2:])
                auth = double.authenticate(self.command, raw_path, raw_query, self.headers, body)
                double.log.append((self.command, key, auth))
                if auth not in ("header", "presigned"):
                    return self.error(403, auth)
                if double.fail:
                    return self.error(double.fail.pop(0), "InternalError")
                if self.command == "PUT":
                    double.objects[key] = body
                    return self.reply(200)
                if self.command == "DELETE":
                    double.objects.pop(key, None)
                    return self.reply(204)
                if key not in double.objects:
                    return self.error(404, "NoSuchKey")
                return self.reply(200, double.read_override.get(key, double.objects[key]))

            do_GET = do_PUT = do_DELETE = serve

        return Handler


# ---- The shipped client ---------------------------------------------------

RUNNER = r'''
const fs = require('node:fs');
const [modulePath, snippetPath, varsPath, localOrigin] = process.argv.slice(1);
const store = require(modulePath);
const realFetch = globalThis.fetch;
globalThis.fetch = (url, init) => realFetch(String(url).replace(/^https:\/\/[0-9a-f]{32}\.r2\.cloudflarestorage\.com/, localOrigin), init);
// Production retries back off for seconds; the test takes them at once.
const realSetTimeout = setTimeout;
globalThis.setTimeout = (fn, ms, ...args) => realSetTimeout(fn, 0, ...args);
const vars = JSON.parse(fs.readFileSync(varsPath, 'utf8'));
const outputs = {}, log = [];
const core = {
  setOutput(k, v) { outputs[k] = v; },
  info(m) { log.push(['info', m]); }, notice(m) { log.push(['notice', m]); },
  warning(m) { log.push(['warning', m]); }, setFailed(m) { log.push(['failed', m]); },
};
const context = {repo: {owner: 'example', repo: 'app'}, sha: 'a'.repeat(40), eventName: vars.event || 'pull_request', payload: {repository: {id: 42}}};
(async () => {
  const body = fs.readFileSync(snippetPath, 'utf8');
  let result = null, threw = null;
  try {
    result = await new (Object.getPrototypeOf(async function () {}).constructor)('store', 'core', 'context', 'vars', body)(store, core, context, vars);
  } catch (error) {
    threw = String(error && error.message || error);
  }
  process.stdout.write(JSON.stringify({result: result === undefined ? null : result, threw, outputs, log}));
})();
'''


class Client:
    """Runs snippets against the module the workflow's install step writes."""

    def __init__(self, root: Path, double: Double) -> None:
        document = yaml.safe_load(WORKFLOW.read_text())
        install = next(s for s in document["jobs"]["reuse-plan"]["steps"] if s.get("name") == INSTALL)["run"]
        self.root = root
        self.double = double
        self.runner_temp = root / "runner-temp"
        self.runner_temp.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CI_ARTIFACTS_R2_")}
        subprocess.run(["bash", "-c", install], env={**self.env, "RUNNER_TEMP": str(self.runner_temp)},
                       check=True, timeout=30)
        self.module = self.runner_temp / "ci-artifact-store.cjs"
        assert self.module.is_file()

    def run(self, snippet: str, **variables) -> dict:
        (self.root / "snippet.js").write_text(snippet)
        (self.root / "vars.json").write_text(json.dumps(variables))
        result = subprocess.run(
            ["node", "-e", RUNNER, str(self.module), str(self.root / "snippet.js"), str(self.root / "vars.json"),
             self.double.origin],
            env=self.env, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)


def messages(outcome: dict, kind: str) -> list[str]:
    return [message for level, message in outcome["log"] if level == kind]


# ---- Workflow text --------------------------------------------------------

def test_no_github_artifacts() -> None:
    """F6: nothing in the callable touches GitHub artifact storage."""
    text = WORKFLOW.read_text()
    for forbidden in ("upload-artifact", "download-artifact", "listArtifactsForRepo",
                      "listWorkflowRunArtifacts", "downloadArtifact", "getArtifact"):
        assert forbidden not in text, f"nuxt-cloudflare.yml still names {forbidden}"
    browser = BROWSER_WORKFLOW.read_text()
    assert "upload-artifact" not in browser, "reusable-browser-tests.yml still uploads an artifact"
    print("PASS  no upload-artifact, download-artifact or artifact API in nuxt-cloudflare.yml; no browser uploads")


def test_workflow_wiring() -> None:
    document = yaml.safe_load(WORKFLOW.read_text())
    call = document[True]["workflow_call"]
    for name in CREDS:
        assert call["secrets"][name] == {"required": False}, name
    installs = {job_id: step for job_id, job in document["jobs"].items()
                for step in job.get("steps", []) if step.get("name") == INSTALL}
    assert set(installs) == {"reuse-plan", "build", "e2e-plan", "e2e", "e2e-quarantine", "required"}, set(installs)
    texts = {step["run"] for step in installs.values()}
    assert len(texts) == 1, "every install step must be the one anchored text"
    install = texts.pop()
    assert "cat > \"$RUNNER_TEMP/ci-artifact-store.cjs\" <<'CI_ARTIFACT_STORE'" in install
    body = install.split("<<'CI_ARTIFACT_STORE'\n", 1)[1]
    assert body.rstrip().endswith("CI_ARTIFACT_STORE")
    # GitHub substitutes expressions inside run: text before bash sees it.
    assert "${{" not in body, "the client text must carry no workflow expression"

    secret_bindings = set()
    for job_id, job in document["jobs"].items():
        assert "CI_ARTIFACTS_R2" not in json.dumps(job.get("env", {})), job_id
        steps = job.get("steps", [])
        for index, step in enumerate(steps):
            if "secrets.CI_ARTIFACTS_R2_SECRET_ACCESS_KEY" in json.dumps(step.get("env", {})):
                secret_bindings.add((job_id, step.get("name")))
            script = (step.get("with") or {}).get("script", "")
            if "ci-artifact-store.cjs" in script:
                assert step["uses"].startswith("actions/github-script@"), (job_id, step.get("name"))
                earlier = [s.get("name") for s in steps[:index]]
                # Required's copy of the key script runs on pull requests
                # only, and returns before it would load the client.
                early_return = "if (context.eventName !== 'push') return;"
                pull_request_only = (
                    "github.event_name == 'pull_request'" in step.get("if", "")
                    and early_return in script
                    and script.index(early_return) < script.index("ci-artifact-store.cjs"))
                assert INSTALL in earlier or pull_request_only, (
                    f"{job_id}/{step.get('name')} requires the client before installing it")
    assert secret_bindings == {
        ("reuse-plan", "Find equivalent successful PR Required proof"),
        ("build", "Publish prebuilt E2E application"),
        ("e2e-plan", "Find equivalent successful PR E2E proof"),
        ("required", "Publish Required proof ${{ steps.required-key.outputs.key }}"),
        ("required", "Publish full E2E proof ${{ needs.e2e-plan.outputs.proof-key }}"),
    }, secret_bindings
    # The attestation the lookup checks is the publish step's evaluated NAME,
    # so the names in the workflow and the prefixes in the client must agree.
    assert "const PROOF_STEP = { required: 'Publish Required proof', e2e: 'Publish full E2E proof' };" in body
    for job_id in ("reuse-plan", "e2e-plan"):
        assert document["jobs"][job_id]["permissions"]["actions"] == "read", job_id
    print("PASS  one client text, installed before every use; the secret key reaches only store writers and lookups")


# ---- Signature V4 ---------------------------------------------------------

AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
AWS_STAMP = "20130524T000000Z"
AWS_HOST = "examplebucket.s3.amazonaws.com"
EMPTY = hashlib.sha256(b"").hexdigest()
WELCOME = "Welcome to Amazon S3."
# AWS S3 documentation, "Signature Calculations for the Authorization Header"
# and "Query String Authentication": (method, uri, query, headers, payload,
# expected signature).
VECTORS = [
    ("GET", "/test.txt", [], {"host": AWS_HOST, "range": "bytes=0-9", "x-amz-content-sha256": EMPTY,
                              "x-amz-date": AWS_STAMP}, EMPTY,
     "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"),
    ("PUT", "/test%24file.text", [], {"date": "Fri, 24 May 2013 00:00:00 GMT", "host": AWS_HOST,
                                      "x-amz-date": AWS_STAMP, "x-amz-storage-class": "REDUCED_REDUNDANCY",
                                      "x-amz-content-sha256": hashlib.sha256(WELCOME.encode()).hexdigest()},
     hashlib.sha256(WELCOME.encode()).hexdigest(),
     "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"),
    ("GET", "/", [("lifecycle", "")], {"host": AWS_HOST, "x-amz-content-sha256": EMPTY, "x-amz-date": AWS_STAMP},
     EMPTY, "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"),
    ("GET", "/", [("max-keys", "2"), ("prefix", "J")],
     {"host": AWS_HOST, "x-amz-content-sha256": EMPTY, "x-amz-date": AWS_STAMP},
     EMPTY, "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"),
]
PRESIGN_VECTOR = "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"


def test_sigv4_vectors(client: Client) -> None:
    outcome = client.run('''
      const out = vars.vectors.map(([method, uri, query, headers, payloadHash]) => store.sign({
        method, uri, query: Object.fromEntries(query), headers, payloadHash,
        secretAccessKey: vars.secret, stamp: vars.stamp, region: 'us-east-1', service: 's3' }).signature);
      const presigned = store.presign({ host: vars.host, uri: '/test.txt', accessKeyId: 'AKIAIOSFODNN7EXAMPLE',
        secretAccessKey: vars.secret, stamp: vars.stamp, expires: 86400, region: 'us-east-1', service: 's3' });
      const url = store.presignedUrl({ origin: `https://${vars.host}`, uri: '/test.txt', accessKeyId: 'AKIAIOSFODNN7EXAMPLE',
        stamp: vars.stamp, expires: 86400, signature: presigned, region: 'us-east-1', service: 's3' });
      return { out, presigned, url, stamp: store.amzDate(new Date('2013-05-24T00:00:00.000Z')) };
    ''', vectors=[list(v[:5]) for v in VECTORS], secret=AWS_SECRET, stamp=AWS_STAMP, host=AWS_HOST)
    assert outcome["threw"] is None, outcome["threw"]
    result = outcome["result"]
    for (method, uri, query, headers, payload, expected), got in zip(VECTORS, result["out"]):
        assert got == expected, (method, uri, got)
        assert signature(AWS_SECRET, AWS_STAMP, "us-east-1", "s3", method, uri, query, headers, payload) == expected
    assert result["presigned"] == PRESIGN_VECTOR
    query = [("X-Amz-Algorithm", "AWS4-HMAC-SHA256"),
             ("X-Amz-Credential", f"AKIAIOSFODNN7EXAMPLE/{AWS_STAMP[:8]}/us-east-1/s3/aws4_request"),
             ("X-Amz-Date", AWS_STAMP), ("X-Amz-Expires", "86400"), ("X-Amz-SignedHeaders", "host")]
    assert signature(AWS_SECRET, AWS_STAMP, "us-east-1", "s3", "GET", "/test.txt", query, {"host": AWS_HOST},
                     "UNSIGNED-PAYLOAD") == PRESIGN_VECTOR
    assert result["url"] == (
        "https://examplebucket.s3.amazonaws.com/test.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
        f"&X-Amz-Signature={PRESIGN_VECTOR}")
    assert result["stamp"] == AWS_STAMP
    print(f"PASS  SigV4: the client and an independent signer reproduce AWS's {len(VECTORS) + 1} published examples")


def test_config_and_keys(client: Client) -> None:
    outcome = client.run('''
      const attempt = fn => { try { return { value: fn() }; } catch (error) { return { error: error.message }; } };
      return {
        none: store.storeConfig({}),
        partial: store.storeConfig({ CI_ARTIFACTS_R2_ACCOUNT_ID: vars.creds.CI_ARTIFACTS_R2_ACCOUNT_ID }),
        malformed: attempt(() => store.storeConfig({ ...vars.creds, CI_ARTIFACTS_R2_SECRET_ACCESS_KEY: 'hunter2-not-hex' })),
        valid: store.storeConfig(Object.fromEntries(Object.entries(vars.creds).map(([k, v]) => [k, ` ${v}\n`]))),
        prebuilt: store.prebuiltKey('example/app', 77, 2, 'call-1'),
        badRepo: ['example', 'example/app/x', '../app', 'example/..', 'ex ample/app'].map(r => attempt(() => store.repositoryOf(r)).error || null),
        badScope: attempt(() => store.prebuiltKey('example/app', 1, 1, 'a/b')).error || null,
        badRun: attempt(() => store.prebuiltKey('example/app', '1;x', 1, 's')).error || null,
        proof: store.proofObjectKey('example/app', 'required', vars.requiredKey),
        wrongKind: attempt(() => store.proofObjectKey('example/app', 'e2e', vars.requiredKey)).error || null,
        badKey: attempt(() => store.proofObjectKey('example/app', 'required', 'required-proof-v1-xyz')).error || null,
        badPrefix: attempt(() => store.assertKey('secrets/example/app/x')).error || null,
      };
    ''', creds=CREDS, requiredKey=REQUIRED_KEY)
    result = outcome["result"]
    assert result["none"] is None and result["partial"] is None
    assert "malformed" in result["malformed"]["error"] and "hunter2" not in result["malformed"]["error"]
    valid = result["valid"]
    assert valid["origin"] == f"https://{HOST}" and valid["host"] == HOST and valid["bucket"] == BUCKET
    assert valid["accessKeyId"] == ACCESS and valid["secretAccessKey"] == SECRET
    assert result["prebuilt"] == "prebuilt/example/app/77-2-call-1.tar.gz"
    assert all(result["badRepo"]), result["badRepo"]
    assert result["badScope"] and result["badRun"] and result["wrongKind"] and result["badKey"] and result["badPrefix"]
    assert result["proof"] == f"proof/example/app/{REQUIRED_KEY}.json"
    print("PASS  credentials are all-or-nothing and never echoed; every key is repository-scoped and shape-checked")


# ---- The prebuilt E2E application ----------------------------------------

def make_tree(root: Path) -> None:
    files = {
        "server/index.mjs": "export default 'built once';\n",
        "server/chunks/app.mjs": "x".join(str(i) for i in range(4000)),
        "public/.well-known/security.txt": "Contact: mailto:security@example.test\n",
        "nitro.json": json.dumps({"preset": "cloudflare-module"}),
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    tool = root / "server/bin/start.sh"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\necho start\n")
    tool.chmod(0o755)


def snapshot(root: Path) -> dict[str, tuple[str, bool]]:
    return {
        str(path.relative_to(root)): (hashlib.sha256(path.read_bytes()).hexdigest(),
                                      bool(path.stat().st_mode & stat.S_IXUSR))
        for path in sorted(root.rglob("*")) if path.is_file()
    }


PUBLISH = "await store.publishPrebuiltStep({ core, env: vars.env });"
FETCH = "await store.fetchPrebuiltStep({ core, env: vars.env });"


def test_prebuilt(client: Client, root: Path) -> None:
    double = client.double
    double.reset()
    source = root / "build-ws" / "apps/web/.output"
    make_tree(source)
    publish_env = {**CREDS, "BUILD_DIR": str(source), "BUILD_SCOPE": "call-1", "GITHUB_REPOSITORY": "example/app",
                   "GITHUB_RUN_ID": "77", "GITHUB_RUN_ATTEMPT": "2", "RUNNER_TEMP": str(client.runner_temp)}
    published = client.run(PUBLISH, env=publish_env)
    assert published["threw"] is None, published
    object_text = published["outputs"]["object"]
    reference = json.loads(object_text)
    key = "prebuilt/example/app/77-2-call-1.tar.gz"
    assert reference["key"] == key and key in double.objects
    assert reference["bytes"] == len(double.objects[key])
    assert reference["sha256"] == hashlib.sha256(double.objects[key]).hexdigest()
    assert double.log == [("PUT", key, "header")], double.log
    assert messages(published, "info") == [f"R2 put {key}: {reference['bytes']} bytes, sha256 {reference['sha256']}"]
    # GitHub drops a job output that contains a secret's value.
    for value in CREDS.values():
        assert value not in object_text

    workspace = root / "e2e-ws"
    target = workspace / "apps/web/.output"
    target.mkdir(parents=True)
    (target / "partial.txt").write_text("left over from an earlier attempt")
    fetch_env = {"PREBUILT_OBJECT": object_text, "BUILD_PATH": ".output", "WORKING_DIRECTORY": "apps/web",
                 "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(client.runner_temp),
                 "CI_ARTIFACTS_R2_ACCOUNT_ID": ACCOUNT, "CI_ARTIFACTS_R2_ACCESS_KEY_ID": ACCESS}
    fetched = client.run(FETCH, env=fetch_env)
    assert fetched["threw"] is None and fetched["outputs"] == {"mode": "r2"}, fetched
    assert messages(fetched, "info") == [
        f"R2 get {key}: {reference['bytes']} bytes, sha256 {reference['sha256']} verified; extracted into .output"]
    assert double.log[-1] == ("GET", key, "presigned"), double.log
    assert snapshot(target) == snapshot(source), "the unpacked application differs from what Build published"
    print("PASS  prebuilt round trip: header-signed PUT, presigned GET without the secret, identical tree and modes")

    def fetch(**overrides) -> dict:
        env = {**fetch_env, **overrides}
        return client.run(FETCH, env={k: v for k, v in env.items() if v is not None})

    # Integrity: a changed object fails the job and leaves the target alone.
    original = double.objects[key]
    (target / "sentinel.txt").write_text("untouched")
    for name, tampered in (("one byte flipped", original[:-1] + bytes([original[-1] ^ 1])),
                           ("bytes appended", original + b"\0")):
        double.objects[key] = tampered
        outcome = fetch()
        failed = messages(outcome, "failed")
        assert failed and "does not match the SHA-256 and size" in failed[0], (name, outcome)
        assert outcome["outputs"] == {"mode": "build"}, name
        assert (target / "sentinel.txt").read_text() == "untouched", name
    double.objects[key] = original
    print("PASS  a prebuilt that differs from Build's SHA-256 or size fails the job before anything is unpacked")

    stale = client.run('''
      const old = new Date(Date.now() - 3 * 86400 * 1000);
      const stamp = store.amzDate(old);
      const signature = store.presign({ host: vars.host, uri: store.canonicalUri(store.BUCKET, vars.key),
        accessKeyId: vars.access, secretAccessKey: vars.secret, stamp, expires: 86400 });
      return { ...vars.reference, date: stamp, expires: 86400, signature };
    ''', host=HOST, key=key, access=ACCESS, secret=SECRET, reference=reference)["result"]
    fallbacks = {
        "expired link": ({"PREBUILT_OBJECT": json.dumps(stale)}, "warning", "HTTP 403 AccessDenied"),
        "forged signature": ({"PREBUILT_OBJECT": json.dumps({**reference, "signature": "0" * 64})},
                             "warning", "HTTP 403 SignatureDoesNotMatch"),
        "link to another object": ({"PREBUILT_OBJECT": json.dumps({**reference, "key": "prebuilt/example/app/1-1-x.tar.gz"})},
                                   "warning", "HTTP 403 SignatureDoesNotMatch"),
        "nothing published": ({"PREBUILT_OBJECT": None}, "notice", "Build published no prebuilt application"),
        "malformed reference": ({"PREBUILT_OBJECT": '{"key": "prebuilt/x"}'}, "warning", "malformed"),
        "no store identifiers": ({"CI_ARTIFACTS_R2_ACCOUNT_ID": None}, "warning", "identifiers are not available"),
    }
    for name, (overrides, level, fragment) in fallbacks.items():
        outcome = fetch(**overrides)
        assert outcome["outputs"] == {"mode": "build"} and not messages(outcome, "failed"), (name, outcome)
        assert any(fragment in m for m in messages(outcome, level)), (name, fragment, outcome["log"])
    del double.objects[key]  # lifecycle expiry, or a bucket someone emptied
    outcome = fetch()
    assert outcome["outputs"] == {"mode": "build"} and not messages(outcome, "failed"), outcome
    assert any("HTTP 404 NoSuchKey" in m for m in messages(outcome, "warning")), outcome
    double.objects[key] = original
    double.fail[:] = [500, 500, 500]
    outcome = fetch()
    assert outcome["outputs"] == {"mode": "build"} and not messages(outcome, "failed"), outcome
    assert any("HTTP 500 InternalError after 3 attempt(s)" in m for m in messages(outcome, "warning")), outcome
    assert (target / "sentinel.txt").read_text() == "untouched"
    escaping = fetch(BUILD_PATH="../outside")
    assert escaping["threw"] and "relative directory inside working-directory" in escaping["threw"], escaping
    print(f"PASS  {len(fallbacks) + 2} unreadable, expired or absent prebuilts make the job build its own; "
          "an escaping path fails")

    before = dict(double.objects)
    double.log.clear()
    for name, overrides, level, fragment in (
        ("no credentials", {k: None for k in CREDS}, "notice", "No CI artifact store credentials"),
        ("malformed credentials", {"CI_ARTIFACTS_R2_ACCESS_KEY_ID": "nope"}, "warning", "malformed"),
        ("missing build output", {"BUILD_DIR": str(root / "absent")}, "warning", "Could not publish"),
    ):
        env = {k: v for k, v in {**publish_env, **overrides}.items() if v is not None}
        outcome = client.run(PUBLISH, env=env)
        assert outcome["threw"] is None and outcome["outputs"] == {"object": ""}, (name, outcome)
        assert any(fragment in m for m in messages(outcome, level)), (name, outcome["log"])
    assert double.log == [] and double.objects == before, "a publish without usable credentials reached the store"
    double.fail[:] = [503, 503, 503]
    outcome = client.run(PUBLISH, env=publish_env)
    assert outcome["outputs"] == {"object": ""}
    assert any("HTTP 503 InternalError after 3 attempt(s)" in m for m in messages(outcome, "warning")), outcome
    print("PASS  Build never fails on the store: no credentials, bad credentials or an outage publish nothing")


# ---- Reuse proofs ---------------------------------------------------------

FIND = '''
  const github = {
    rest: { actions: {
      async getWorkflowRun({ run_id }) { return { data: { id: run_id, run_attempt: 1, event: 'pull_request',
        status: 'completed', conclusion: 'success', path: '.github/workflows/ci.yml', repository: { id: 42 },
        head_repository: { id: 42 }, html_url: `https://example.test/run/${run_id}` } }; },
      async listJobsForWorkflowRunAttempt() { return { data: { jobs: vars.jobs } }; },
    } },
    async paginate(method, args) { return (await method(args)).data.jobs; },
  };
  return await store.findProof({ github, context, kind: vars.kind, key: vars.key,
    workflowPath: '.github/workflows/ci.yml', env: vars.env });
'''


def test_proofs(client: Client) -> None:
    double = client.double
    double.reset()
    env = {**CREDS, "GITHUB_RUN_ID": "555", "GITHUB_RUN_ATTEMPT": "1"}
    publish = "await store.publishProofStep({ core, context, kind: vars.kind, env: vars.env });"
    outcome = client.run(publish, kind="required", env={**env, "PROOF_KEY": REQUIRED_KEY})
    object_key = f"proof/example/app/{REQUIRED_KEY}.json"
    assert messages(outcome, "info") == [
        f"R2 put {object_key}: pointer to run 555 attempt 1", f"R2 get {object_key}: read-back matches"], outcome
    assert double.log == [("PUT", object_key, "header"), ("GET", object_key, "header")], double.log
    pointer = json.loads(double.objects[object_key])
    assert {k: v for k, v in pointer.items() if k != "written_at"} == {
        "schema": "narduk-ci-proof-pointer-v1", "kind": "required", "key": REQUIRED_KEY,
        "repository": "example/app", "repository_id": 42, "run_id": 555, "run_attempt": 1, "sha": "a" * 40,
    }, pointer

    attested = [{"name": "ci / Required", "conclusion": "success", "steps": [
        {"name": f"Publish Required proof {REQUIRED_KEY}", "conclusion": "success"}]}]
    found = client.run(FIND, kind="required", key=REQUIRED_KEY, env=CREDS, jobs=attested, event="push")
    assert found["threw"] is None and found["result"]["found"] is True, found
    assert found["result"]["run"]["html_url"] == "https://example.test/run/555"
    unattested = [{"name": "ci / Required", "conclusion": "success", "steps": [
        {"name": "Publish Required proof ${{ steps.required-key.outputs.key }}", "conclusion": "success"}]}]
    refused = client.run(FIND, kind="required", key=REQUIRED_KEY, env=CREDS, jobs=unattested, event="push")
    assert refused["result"]["found"] is False and "shows no successful" in refused["result"]["reason"], refused
    absent = client.run(FIND, kind="e2e", key=E2E_KEY, env=CREDS, jobs=attested, event="push")
    assert absent["result"] == {"found": False, "reason": f"no pointer at proof/example/app/{E2E_KEY}.json"}, absent
    print("PASS  a published pointer reads back byte for byte and is honoured only with the attesting step")

    double.log.clear()
    for name, kind, overrides, level, fragment in (
        ("no credentials", "e2e", {k: None for k in CREDS}, "notice", "No CI artifact store credentials"),
        ("malformed key", "e2e", {"PROOF_KEY": "e2e-proof-v1-short"}, "warning", "not a e2e proof key"),
        ("key of the other kind", "e2e", {"PROOF_KEY": REQUIRED_KEY}, "warning", "not a e2e proof key"),
    ):
        step_env = {k: v for k, v in {**env, "PROOF_KEY": E2E_KEY, **overrides}.items() if v is not None}
        outcome = client.run(publish, kind=kind, env=step_env)
        assert outcome["threw"] is None and not messages(outcome, "failed"), (name, outcome)
        assert any(fragment in m for m in messages(outcome, level)), (name, outcome["log"])
    assert double.log == [], double.log
    double.read_override[f"proof/example/app/{E2E_KEY}.json"] = b"{}"
    outcome = client.run(publish, kind="e2e", env={**env, "PROOF_KEY": E2E_KEY})
    assert any("read-back does not match" in m for m in messages(outcome, "warning")), outcome
    double.read_override.clear()
    double.fail[:] = [500, 500, 500]
    outcome = client.run(publish, kind="e2e", env={**env, "PROOF_KEY": E2E_KEY})
    assert any("HTTP 500 InternalError after 3 attempt(s)" in m for m in messages(outcome, "warning")), outcome
    assert not messages(outcome, "failed")
    print("PASS  proof publishing only warns: no credentials, a bad key, a bad read-back or an outage never fail Required")


def main() -> None:
    test_no_github_artifacts()
    test_workflow_wiring()
    double = Double()
    try:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            client = Client(root, double)
            test_sigv4_vectors(client)
            test_config_and_keys(client)
            test_prebuilt(client, root)
            test_proofs(client)
    finally:
        double.server.shutdown()
    print("ci artifact store contract passed")


if __name__ == "__main__":
    main()
