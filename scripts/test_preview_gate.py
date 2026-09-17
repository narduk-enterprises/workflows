#!/usr/bin/env python3
"""Behaviour tests for nuxt-cloudflare.yml's `preview` lane (V1).

Like the other behaviour tests here, nothing below is a copy of the shipped
logic: every case extracts a step's `run:` text straight out of the workflow
YAML and executes that exact text under bash, against a real local HTTP server
and a fake `gh` on PATH. If someone edits the gate, these either still pass
against the new text or they fail.

What is being protected, and why each part is worth a test:

  * THE URL IS NOT THE PROOF. A preview link in a comment says a build was
    attempted. The gate is not satisfied until the URL answers AND its
    `x-build-version` header is a prefix of the pull request's head SHA. The
    comparison is by PREFIX because Cloudflare emits a 12-character short SHA
    (buoys docs/workers-builds.md records `x-build-version: a84fa2163903`)
    while `git rev-parse --short` defaults to 7 — a fixed-width comparison is a
    gate that never passes, and a gate that never passes gets deleted.
  * FAIL CLOSED. A preview that never appears, answers 4xx, serves a different
    build, or carries no version header at all must EXIT NON-ZERO. "No preview"
    is the most common way a Workers Builds connection silently breaks.
  * ONE STICKY COMMENT. The comment is found by marker and EDITED. A gate that
    appends leaves a pull request with a column of stale preview comments and
    reviewers reading the wrong one.
  * `Required` still gates. Selected on a pull request means the job must
    SUCCEED; `preview-checks: none`, or any non-pull-request event, means it
    must be SKIPPED.

Run: python3 scripts/test_preview_gate.py
"""

from __future__ import annotations

import http.server
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading

import yaml

NUXT_CF = pathlib.Path(".github/workflows/nuxt-cloudflare.yml")
HEAD_SHA = "a84fa21639032137d811640132bbe031cf49d4ac"


def load() -> dict:
    return yaml.safe_load(NUXT_CF.read_text())


def step_run(doc: dict, job_id: str, name: str) -> str:
    for step in doc["jobs"][job_id]["steps"]:
        if step.get("name") == name:
            return step["run"]
    raise SystemExit(f"::error::no '{name}' step in {NUXT_CF} job '{job_id}'")


# --- a local origin that can be told exactly what to serve ------------------
class _Origin(http.server.BaseHTTPRequestHandler):
    status = 200
    version_header = HEAD_SHA[:12]
    paths: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server's interface
        type(self).paths.append(self.path)
        self.send_response(type(self).status)
        if type(self).version_header is not None:
            self.send_header("x-build-version", type(self).version_header)
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args) -> None:  # silence the default stderr spam
        return


class Origin:
    def __init__(self, status: int = 200, version_header: str | None = HEAD_SHA[:12]) -> None:
        handler = type("Handler", (_Origin,), {"status": status, "version_header": version_header, "paths": []})
        self.handler = handler
        self.server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "Origin":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def paths(self) -> list[str]:
        return self.handler.paths


FAKE_GH = '''#!/usr/bin/env python3
"""Fake `gh` recording its argv and replaying a fixture comment list."""
import json
import os
import sys

argv = sys.argv[1:]
with open(os.environ["GH_ARGV_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(argv) + "\\n")

if os.environ.get("GH_FAIL") == "1":
    sys.stderr.write("fake gh: HTTP 401\\n")
    sys.exit(1)

is_list = "--paginate" in argv and not any(a == "-X" for a in argv)
if is_list:
    for comment in json.loads(os.environ.get("GH_COMMENTS", "[]")):
        sys.stdout.write(json.dumps(comment) + "\\n")
sys.exit(0)
'''


def sandbox(tmp: str, *, gh_comments: str = "[]", gh_fail: bool = False) -> tuple[dict, pathlib.Path]:
    bindir = pathlib.Path(tmp, "bin")
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    argv_log = pathlib.Path(tmp, "gh-argv.log")
    argv_log.write_text("")
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin:/usr/local/bin",
        "HOME": tmp,
        "GH_ARGV_LOG": str(argv_log),
        "GH_COMMENTS": gh_comments,
        "GH_TOKEN": "fake",
    }
    if gh_fail:
        env["GH_FAIL"] = "1"
    return env, argv_log


def bash(script: str, env: dict, cwd: str) -> tuple[int, str]:
    proc = subprocess.run(["bash", "-c", script], env=env, cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


RESULTS: list[str] = []


def record(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + label)
    if not ok:
        RESULTS.append(f"{label}: {detail}")
        if detail:
            print(f"      {detail[:900]}")


# --- 1. the configuration validator ----------------------------------------
VALIDATE_CASES = [
    ("og alone is valid", "og", "pr-comment", "", 0, ""),
    ("og,e2e-subset is valid", "og,e2e-subset", "pr-comment", "", 0, ""),
    ("whitespace around checks is tolerated", " og , e2e-subset ", "pr-comment", "", 0, ""),
    ("a typo is rejected before a runner is spent", "smoke", "pr-comment", "", 1, "unknown check 'smoke'"),
    ("'ogg' is not silently read as 'og'", "ogg", "pr-comment", "", 1, "unknown check 'ogg'"),
    ("none alongside a real check is rejected", "none,og", "pr-comment", "", 1, "must be exactly 'none'"),
    ("whitespace-padded none is rejected rather than half-applied", " none ", "pr-comment", "", 1, "must be exactly 'none'"),
    ("an empty selection is rejected", ",  ,", "pr-comment", "", 1, "no checks at all"),
    ("an unknown url source is rejected", "og", "deployment", "", 1, "must be 'pr-comment' or 'url-template'"),
    ("url-template with no template is rejected", "og", "url-template", "", 1, "preview-url-template is empty"),
    ("url-template with a template is valid", "og", "url-template", "https://x-y.workers.dev", 0, ""),
]


def check_validator(doc: dict) -> None:
    script = step_run(doc, "preview", "Validate preview configuration")
    tmp = tempfile.mkdtemp(prefix="preview-validate-")
    try:
        for label, checks, source, template, want_rc, want_sub in VALIDATE_CASES:
            env = {"PATH": "/usr/bin:/bin", "CHECKS": checks, "SOURCE": source, "TEMPLATE": template}
            rc, out = bash(script, env, tmp)
            ok = rc == want_rc and want_sub in out
            record(f"validate: {label}", ok, f"rc={rc} (want {want_rc}) out={out.strip()!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- 2. resolve + verify ----------------------------------------------------
def resolve_env(tmp: str, extra: dict, **sandbox_kwargs) -> tuple[dict, pathlib.Path, pathlib.Path]:
    env, argv_log = sandbox(tmp, **sandbox_kwargs)
    outputs = pathlib.Path(tmp, "github-output")
    outputs.write_text("")
    env.update(
        {
            "SOURCE": "url-template",
            "TEMPLATE": "",
            "HEAD_SHA": HEAD_SHA,
            "HEAD_REF": "codex/Buoys_Complete",
            "PR_NUMBER": "116",
            "REPO": "narduk-enterprises/buoys",
            # 0.02 minutes -> a 1s wait, so a failing case makes exactly one
            # bounded read instead of holding the suite for the real default.
            "BUDGET_MINUTES": "0.02",
            "GITHUB_OUTPUT": str(outputs),
        }
    )
    env.update(extra)
    return env, outputs, argv_log


def check_resolver(doc: dict) -> None:
    script = step_run(doc, "preview", "Resolve and verify preview deployment")
    tmp = tempfile.mkdtemp(prefix="preview-resolve-")
    try:
        # A live preview serving this PR's head, reached through a template
        # whose {branch-alias} must be the sanitised branch name.
        with Origin() as origin:
            env, outputs, _ = resolve_env(tmp, {"TEMPLATE": origin.base + "/{branch-alias}/", "BUDGET_MINUTES": "20"})
            rc, out = bash(script, env, tmp)
            written = outputs.read_text()
            ok = (
                rc == 0
                and f"url={origin.base}/codex-buoys-complete/" in written
                and f"build-version={HEAD_SHA[:12]}" in written
                and origin.paths == ["/codex-buoys-complete/"]
            )
            record(
                "resolve: a preview serving the head SHA passes and exports the URL",
                ok,
                f"rc={rc} outputs={written!r} paths={origin.paths} out={out.strip()!r}",
            )

        # A 7-character header is still a prefix of the head SHA.
        with Origin(version_header=HEAD_SHA[:7]) as origin:
            env, outputs, _ = resolve_env(tmp, {"TEMPLATE": origin.base + "/", "BUDGET_MINUTES": "20"})
            rc, out = bash(script, env, tmp)
            record("resolve: a 7-character x-build-version is accepted", rc == 0, f"rc={rc} out={out.strip()!r}")

        # Six characters is not enough to identify a commit.
        with Origin(version_header=HEAD_SHA[:6]) as origin:
            env, _outputs, _ = resolve_env(tmp, {"TEMPLATE": origin.base + "/"})
            rc, out = bash(script, env, tmp)
            record(
                "resolve: a 6-character x-build-version is rejected",
                rc == 1 and "not this pull request's head" in out,
                f"rc={rc} out={out.strip()!r}",
            )

        # The preview is up, but it is serving a different commit.
        with Origin(version_header="5540c56c7057") as origin:
            env, _outputs, _ = resolve_env(tmp, {"TEMPLATE": origin.base + "/"})
            rc, out = bash(script, env, tmp)
            record(
                "resolve: a preview serving a DIFFERENT build fails",
                rc == 1 and "is serving build 5540c56c7057" in out,
                f"rc={rc} out={out.strip()!r}",
            )

        # Up, current commit unknown: no header at all.
        with Origin(version_header=None) as origin:
            env, _outputs, _ = resolve_env(tmp, {"TEMPLATE": origin.base + "/"})
            rc, out = bash(script, env, tmp)
            record(
                "resolve: no x-build-version header fails",
                rc == 1 and "no x-build-version header" in out,
                f"rc={rc} out={out.strip()!r}",
            )

        # A 5xx/4xx origin is not a ready preview.
        with Origin(status=503) as origin:
            env, _outputs, _ = resolve_env(tmp, {"TEMPLATE": origin.base + "/"})
            rc, out = bash(script, env, tmp)
            record(
                "resolve: an erroring origin fails",
                rc == 1 and "returned HTTP 503" in out,
                f"rc={rc} out={out.strip()!r}",
            )

        # pr-comment: only Cloudflare's own comment counts.
        env, _outputs, _ = resolve_env(
            tmp,
            {"SOURCE": "pr-comment"},
            gh_comments=(
                '[{"login": "a-human", "body": "try https://someone-elses-guess.workers.dev"},'
                ' {"login": "dependabot[bot]", "body": "bumped a thing"}]'
            ),
        )
        rc, out = bash(script, env, tmp)
        record(
            "resolve: a workers.dev URL from a non-Cloudflare author is ignored",
            rc == 1 and "no comment from Cloudflare" in out and "someone-elses-guess" not in out,
            f"rc={rc} out={out.strip()!r}",
        )

        # pr-comment: Cloudflare's comment IS read, and the URL it carries is
        # the one the gate then tries to verify.
        env, _outputs, _ = resolve_env(
            tmp,
            {"SOURCE": "pr-comment"},
            gh_comments=(
                '[{"login": "cloudflare-workers-and-pages[bot]", "body": '
                '"Deploying buoys\\n\\nPreview URL: https://codex-buoys-complete-buoys.narduk-enterprises.workers.dev"}]'
            ),
        )
        rc, out = bash(script, env, tmp)
        record(
            "resolve: Cloudflare's comment URL is extracted and then verified",
            rc == 1 and "codex-buoys-complete-buoys.narduk-enterprises.workers.dev" in out,
            f"rc={rc} out={out.strip()!r}",
        )

        # A comment read that fails is a bad round, not a green gate.
        env, _outputs, _ = resolve_env(tmp, {"SOURCE": "pr-comment"}, gh_fail=True)
        rc, out = bash(script, env, tmp)
        record(
            "resolve: an unreadable comment list warns and still fails closed",
            rc == 1 and "could not read pull-request comments" in out,
            f"rc={rc} out={out.strip()!r}",
        )

        # Not a pull request at all.
        env, _outputs, _ = resolve_env(tmp, {"HEAD_SHA": "", "TEMPLATE": "https://x.workers.dev"})
        rc, out = bash(script, env, tmp)
        record(
            "resolve: no pull-request head SHA is an immediate, explained failure",
            rc == 1 and "pull_request/pull_request_target events only" in out,
            f"rc={rc} out={out.strip()!r}",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- 3. the sticky comment --------------------------------------------------
def check_sticky_comment(doc: dict) -> None:
    script = step_run(doc, "preview", "Post or update the sticky preview comment")
    marker = "<!-- narduk-ci:preview -->"
    tmp = tempfile.mkdtemp(prefix="preview-comment-")
    try:
        base = {
            "MARKER": marker,
            "REPO": "narduk-enterprises/buoys",
            "PR_NUMBER": "116",
            "PREVIEW_URL": "https://codex-buoys-complete-buoys.narduk-enterprises.workers.dev",
            "BUILD_VERSION": HEAD_SHA[:12],
            "HEAD_SHORT": HEAD_SHA[:12],
            "RESOLVE_OUTCOME": "success",
            "OG_OUTCOME": "success",
            "E2E_OUTCOME": "skipped",
            "RUN_URL": "https://github.com/narduk-enterprises/buoys/actions/runs/1",
        }

        # No existing comment -> POST exactly once.
        env, argv_log = sandbox(tmp, gh_comments="[]")
        summary = pathlib.Path(tmp, "summary.md")
        summary.write_text("")
        env.update(base)
        env["GITHUB_STEP_SUMMARY"] = str(summary)
        rc, out = bash(script, env, tmp)
        calls = argv_log.read_text()
        record(
            "comment: with no existing comment the gate POSTs one",
            rc == 0 and '"POST"' in calls and '"PATCH"' not in calls and "issues/116/comments" in calls,
            f"rc={rc} calls={calls!r} out={out.strip()!r}",
        )
        record(
            "comment: the URL and build version also reach the job summary",
            "codex-buoys-complete-buoys" in summary.read_text() and HEAD_SHA[:12] in summary.read_text(),
            summary.read_text()[:400],
        )

        # An existing marker comment -> PATCH that id, never a second POST.
        env, argv_log = sandbox(
            tmp,
            gh_comments=f'[{{"id": 4242, "body": "{marker}\\n old body"}}, {{"id": 9, "body": "unrelated"}}]',
        )
        env.update(base)
        rc, out = bash(script, env, tmp)
        calls = argv_log.read_text()
        record(
            "comment: an existing marker comment is EDITED, not appended to",
            rc == 0 and '"PATCH"' in calls and '"POST"' not in calls and "issues/comments/4242" in calls,
            f"rc={rc} calls={calls!r} out={out.strip()!r}",
        )

        # The most valuable comment: the one saying no preview appeared.
        env, argv_log = sandbox(tmp, gh_comments="[]")
        missing_summary = pathlib.Path(tmp, "summary-missing.md")
        missing_summary.write_text("")
        env.update({**base, "PREVIEW_URL": "", "BUILD_VERSION": "", "RESOLVE_OUTCOME": "failure", "OG_OUTCOME": ""})
        env["GITHUB_STEP_SUMMARY"] = str(missing_summary)
        rc, out = bash(script, env, tmp)
        reported = missing_summary.read_text()
        record(
            "comment: a run where no preview appeared still reports, and says so",
            rc == 0
            and '"POST"' in argv_log.read_text()
            and "No preview deployment became ready" in reported
            and ":x: FAIL" in reported,
            f"rc={rc} summary={reported[:400]!r} out={out.strip()!r}",
        )

        # A comment API failure must not turn a passing lane red on its own.
        env, argv_log = sandbox(tmp, gh_comments="[]", gh_fail=True)
        env.update(base)
        rc, out = bash(script, env, tmp)
        record(
            "comment: an unusable comment API warns rather than failing the lane",
            rc == 0 and "::warning::" in out,
            f"rc={rc} out={out.strip()!r}",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- 4. Required aggregation ------------------------------------------------
def check_required(doc: dict) -> None:
    script = step_run(doc, "required", "Require enabled gates to succeed and disabled gates to skip")
    script = script.replace("${{ inputs.extra-gate-scripts }}", "")
    baseline = {
        "PATH": "/usr/bin:/bin",
        "BUILD_RESULT": "success",
        "EXTRA_GATE_RESULT": "skipped",
        "CALLER_LINT_RESULT": "success",
        "E2E_PLAN_RESULT": "skipped",
        "E2E_RESULT": "skipped",
        "E2E_REPORT_RESULT": "skipped",
        "E2E_SHARDS": "1",
        "E2E_PLAN_SKIPPED": "false",
        "RUN_E2E": "false",
        "RUN_DEPLOY_DRY_RUN": "false",
        "DEPLOY_DRY_RUN_RESULT": "skipped",
    }
    cases = [
        ("preview off must be skipped", {"PREVIEW_CHECKS": "none", "PREVIEW_RESULT": "skipped", "EVENT_NAME": "pull_request"}, 0),
        ("preview off but it RAN is a failure", {"PREVIEW_CHECKS": "none", "PREVIEW_RESULT": "success", "EVENT_NAME": "pull_request"}, 1),
        ("preview selected + green on a PR passes", {"PREVIEW_CHECKS": "og", "PREVIEW_RESULT": "success", "EVENT_NAME": "pull_request"}, 0),
        ("preview selected + FAILED on a PR is red", {"PREVIEW_CHECKS": "og", "PREVIEW_RESULT": "failure", "EVENT_NAME": "pull_request"}, 1),
        (
            "preview selected but SKIPPED on a PR is red (fail closed)",
            {"PREVIEW_CHECKS": "og", "PREVIEW_RESULT": "skipped", "EVENT_NAME": "pull_request"},
            1,
        ),
        (
            "preview selected + cancelled on a PR is red",
            {"PREVIEW_CHECKS": "og", "PREVIEW_RESULT": "cancelled", "EVENT_NAME": "pull_request"},
            1,
        ),
        (
            "preview selected on pull_request_target is required too",
            {"PREVIEW_CHECKS": "og,e2e-subset", "PREVIEW_RESULT": "failure", "EVENT_NAME": "pull_request_target"},
            1,
        ),
        ("preview selected on a push must be skipped", {"PREVIEW_CHECKS": "og", "PREVIEW_RESULT": "skipped", "EVENT_NAME": "push"}, 0),
        (
            "preview selected on a push but it ran is a failure",
            {"PREVIEW_CHECKS": "og", "PREVIEW_RESULT": "success", "EVENT_NAME": "push"},
            1,
        ),
    ]
    tmp = tempfile.mkdtemp(prefix="preview-required-")
    try:
        for label, extra, want_rc in cases:
            env = {**baseline, **extra}
            rc, out = bash(script, env, tmp)
            record(f"Required: {label}", rc == want_rc, f"rc={rc} (want {want_rc}) out={out.strip()!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- 5. structure -----------------------------------------------------------
def check_structure(doc: dict) -> None:
    preview = doc["jobs"]["preview"]
    e2e = doc["jobs"]["e2e"]

    record(
        "structure: Required needs the preview job",
        "preview" in doc["jobs"]["required"]["needs"],
        str(doc["jobs"]["required"]["needs"]),
    )
    record(
        "structure: the preview job runs on pull-request events only",
        "pull_request" in str(preview["if"]) and "pull_request_target" in str(preview["if"]),
        str(preview["if"]),
    )
    record(
        "structure: preview-checks: none turns the whole lane off",
        "inputs.preview-checks != 'none'" in " ".join(str(preview["if"]).split()),
        str(preview["if"]),
    )
    record(
        "structure: the preview job stays off the browser pool unless e2e-subset is selected",
        "contains(inputs.preview-checks, 'e2e-subset')" in str(preview["runs-on"])
        and "e2e-runner" in str(preview["runs-on"])
        and "CI_LIGHTWEIGHT_RUNNER" in str(preview["runs-on"]),
        str(preview["runs-on"]),
    )
    record(
        "structure: the preview job's timeout is caller-bounded",
        "inputs.preview-timeout-minutes" in str(preview["timeout-minutes"]),
        str(preview["timeout-minutes"]),
    )

    # The isolated preflight must be the SAME YAML node as the e2e job's, not a
    # copy: two copies of an image-equality gate is one gate and one liability.
    def run_of(job: dict, name: str) -> str:
        return next(s["run"] for s in job["steps"] if s.get("name") == name)

    for name in ("Guard isolated Playwright route", "Assert isolated Playwright toolchain", "Install Playwright browsers"):
        record(
            f"structure: preview shares e2e's '{name}' text verbatim",
            run_of(preview, name) == run_of(e2e, name),
            "the preview lane has drifted from the e2e lane's preflight",
        )

    # The anchors must be real YAML aliases in the source, not two copies that
    # happen to be equal today.
    source = NUXT_CF.read_text()
    for anchor in ("guard_isolated_playwright_route", "assert_isolated_playwright_toolchain", "install_playwright_browsers"):
        record(
            f"structure: '{anchor}' is shared by anchor, not duplicated",
            f"&{anchor}" in source and f"*{anchor}" in source,
            "anchor or alias missing",
        )

    record(
        "structure: the e2e subset against a preview passes PLAYWRIGHT_BASE_URL",
        "PLAYWRIGHT_BASE_URL" in str(
            next(s for s in preview["steps"] if s.get("name") == "Run preview e2e subset")["env"]
        ),
        "the suite would test localhost and report green",
    )
    record(
        "structure: the preview e2e subset uses e2e-pr-args, not the full argument set",
        "inputs.e2e-pr-args"
        in str(next(s for s in preview["steps"] if s.get("name") == "Run preview e2e subset")["env"]["EXTRA_ARGS"]),
        "a preview subset must not silently become the whole suite",
    )
    record(
        "structure: og:check runs --live against the resolved preview URL",
        "og:check --live --base-url" in step_run(doc, "preview", "Check preview social previews (og:check --live)"),
        "og:check is not pointed at the preview",
    )


def main() -> None:
    doc = load()
    check_validator(doc)
    check_resolver(doc)
    check_sticky_comment(doc)
    check_required(doc)
    check_structure(doc)
    if RESULTS:
        for item in RESULTS:
            print(f"::error::{item}")
        raise SystemExit(f"{len(RESULTS)} preview-gate case(s) failed")
    print("\npreview lane contract passed")


if __name__ == "__main__":
    main()
