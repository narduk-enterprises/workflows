#!/usr/bin/env python3
"""Lock the opt-in prebuilt E2E application handoff to fail-closed semantics.

Build publishes the application to the CI artifact store (R2) and hands each
E2E job a reference through job outputs; each E2E job fetches it, checks it
against the SHA-256 and size Build reported, and unpacks it. When the store
has nothing for a job (no credentials, a read failure, an expired link) the job
builds its own application with `build-script`, and fails if that cannot
produce the same path. The store client itself is exercised against a local S3
double in scripts/test_ci_artifact_store.py.
"""

from copy import deepcopy
from pathlib import Path
import json
import os
import re
import subprocess
import tempfile

import yaml


WORKFLOW = Path(".github/workflows/nuxt-cloudflare.yml")
GITHUB_SCRIPT = "actions/github-script@ed597411d8f924073f98dfc5c65a23a2325f34cd"
INSTALL = "Install CI artifact store client"
PUBLISH = "Publish prebuilt E2E application"
FETCH = "Fetch prebuilt E2E application"
FALLBACK = "Build E2E application in this job"
STORE_SECRETS = ("CI_ARTIFACTS_R2_ACCOUNT_ID", "CI_ARTIFACTS_R2_ACCESS_KEY_ID", "CI_ARTIFACTS_R2_SECRET_ACCESS_KEY")


def named_step(job: dict, name: str) -> dict:
    matches = [step for step in job.get("steps", []) if step.get("name") == name]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {name!r} step")
    return matches[0]


def validate(document: dict) -> None:
    trigger = document.get(True, document.get("on", {}))
    artifact_input = trigger["workflow_call"]["inputs"]["e2e-build-artifact-path"]
    assert artifact_input["required"] is False
    assert artifact_input["type"] == "string"
    assert artifact_input["default"] == "auto"

    scope_input = trigger["workflow_call"]["inputs"]["artifact-scope"]
    assert scope_input["required"] is False
    assert scope_input["type"] == "string"
    assert scope_input["default"] == ""

    build = document["jobs"]["build"]
    resolve = named_step(build, "Resolve prebuilt E2E application")
    install = named_step(build, INSTALL)
    publish = named_step(build, PUBLISH)
    assert resolve["env"]["ARTIFACT_SCOPE"] == "${{ inputs.artifact-scope }}"
    assert install["if"] == publish["if"] == "steps.e2e-build-path.outputs.path != ''"
    assert publish["id"] == "e2e-build-publish"
    assert publish["uses"] == GITHUB_SCRIPT
    assert publish["env"]["BUILD_DIR"] == (
        "${{ github.workspace }}/${{ inputs.working-directory }}/${{ steps.e2e-build-path.outputs.path }}")
    assert publish["env"]["BUILD_SCOPE"] == "${{ steps.e2e-build-path.outputs.scope }}"
    for name in STORE_SECRETS:
        assert publish["env"][name] == "${{ secrets.%s }}" % name, name
    assert "store.publishPrebuiltStep({ core })" in publish["with"]["script"]
    # Publishing never fails Build: no `continue-on-error` is needed because
    # the client only warns, and a missing object sends E2E to its fallback.
    assert "continue-on-error" not in publish
    assert build["outputs"]["e2e-build-path"] == "${{ steps.e2e-build-path.outputs.path }}"
    assert build["outputs"]["e2e-build-object"] == "${{ steps.e2e-build-publish.outputs.object }}"
    build_names = [step.get("name") for step in build["steps"]]
    assert (build_names.index("Build") < build_names.index("Extra scripts")
            < build_names.index("Resolve prebuilt E2E application") < build_names.index(INSTALL)
            < build_names.index(PUBLISH))

    for job_id in ("e2e", "e2e-quarantine"):
        e2e = document["jobs"][job_id]
        install = named_step(e2e, INSTALL)
        fetch = named_step(e2e, FETCH)
        fallback = named_step(e2e, FALLBACK)
        run_e2e = named_step(e2e, "Run e2e suite")
        assert install["if"] == fetch["if"] == "needs.build.outputs.e2e-build-path != ''"
        assert fetch["id"] == "e2e-build-fetch"
        assert fetch["uses"] == GITHUB_SCRIPT
        # Browser guests read through the presigned GET Build signed: they get
        # the account and access key IDs, never the secret access key.
        assert fetch["env"] == {
            "PREBUILT_OBJECT": "${{ needs.build.outputs.e2e-build-object }}",
            "BUILD_PATH": "${{ needs.build.outputs.e2e-build-path }}",
            "WORKING_DIRECTORY": "${{ inputs.working-directory }}",
            "CI_ARTIFACTS_R2_ACCOUNT_ID": "${{ secrets.CI_ARTIFACTS_R2_ACCOUNT_ID }}",
            "CI_ARTIFACTS_R2_ACCESS_KEY_ID": "${{ secrets.CI_ARTIFACTS_R2_ACCESS_KEY_ID }}",
        }
        assert "store.fetchPrebuiltStep({ core })" in fetch["with"]["script"]
        assert "continue-on-error" not in fetch
        assert "secrets.CI_ARTIFACTS_R2_SECRET_ACCESS_KEY" not in json.dumps(e2e)
        assert fallback["if"] == (
            "needs.build.outputs.e2e-build-path != '' && steps.e2e-build-fetch.outputs.mode != 'r2'")
        assert fallback["env"] == {
            "SCRIPT": "${{ inputs.build-script }}",
            "PM": "${{ inputs.package-manager }}",
            "BUILD_PATH": "${{ needs.build.outputs.e2e-build-path }}",
        }
        assert (
            run_e2e["env"]["E2E_PREBUILT_ARTIFACT"]
            == "${{ needs.build.outputs.e2e-build-path != '' && '1' || '' }}"
        )
        names = [step.get("name") for step in e2e["steps"]]
        # Fetch first (it needs no toolchain), build after dependencies and the
        # toolchain assertion (a broken pool fails before it spends a build).
        assert names.index(INSTALL) < names.index(FETCH) < names.index("Install dependencies (pnpm)")
        assert names.index("Assert isolated Playwright toolchain") < names.index(FALLBACK) < names.index("Run e2e suite")


def run_resolve(script: str, folder: Path, build_path: str, run_e2e: str,
                 artifact_scope: str = "") -> tuple[int, dict, str]:
    output = folder / "step-output"
    result = subprocess.run(
        ["bash", "-c", script], cwd=folder,
        env={**os.environ, "BUILD_PATH": build_path, "RUN_E2E": run_e2e,
             "ARTIFACT_SCOPE": artifact_scope, "GITHUB_OUTPUT": str(output)},
        capture_output=True, text=True,
    )
    text = output.read_text() if output.exists() else ""
    values = dict(line.split("=", 1) for line in text.splitlines() if line)
    return result.returncode, values, result.stderr


def test_resolve(document: dict) -> None:
    script = named_step(document['jobs']['build'], 'Resolve prebuilt E2E application')['run']
    cases = [
        ('auto', 'true', ['.output'], 0, '.output'),
        ('auto', 'true', ['apps/web/.output'], 0, 'apps/web/.output'),
        ('auto', 'true', [], 1, ''),
        ('auto', 'true', ['.output', 'apps/web/.output'], 1, ''),
        ('auto', 'false', [], 0, ''),
        ('', 'true', [], 0, ''),
        ('custom/build', 'false', ['custom/build'], 0, 'custom/build'),
        ('render-output', 'true', ['render-output'], 0, 'render-output'),
        ('missing', 'true', [], 1, ''),
        ('../outside', 'true', [], 1, ''),
        ('/tmp', 'true', [], 1, ''),
        ('.', 'true', [], 1, ''),
        ('bad\npath', 'true', [], 1, ''),
    ]
    for requested, enabled, directories, status, expected in cases:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for directory in directories:
                entry = root / directory / 'server/index.mjs'
                entry.parent.mkdir(parents=True)
                entry.write_text('// built once')
            returncode, values, stderr = run_resolve(script, root, requested, enabled)
            assert (returncode != 0) == bool(status), (requested, stderr)
            if expected:
                assert values == {
                    'path': expected,
                    'scope': values.get('scope', ''),
                }, values
                # No caller-supplied artifact-scope: the random fallback must
                # still be a stable-shaped, key-segment-safe token.
                assert re.fullmatch(r'[0-9a-f]{12}', values['scope']), values
            else:
                assert values == {}, values
    # A directory symlink outside the checkout must never become an upload.
    with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
        root = Path(folder)
        (root / 'external').symlink_to(outside, target_is_directory=True)
        returncode, _values, _stderr = run_resolve(script, root, 'external', 'true')
        assert returncode != 0


def prebuilt_keys(document: dict, scopes: list[str]) -> list[str]:
    """Render the store key the shipped client derives for each scope."""
    install = named_step(document["jobs"]["build"], INSTALL)["run"]
    with tempfile.TemporaryDirectory() as folder:
        env = {k: v for k, v in os.environ.items() if not k.startswith("CI_ARTIFACTS_R2_")}
        env["RUNNER_TEMP"] = folder
        subprocess.run(["bash", "-c", install], env=env, check=True, timeout=30)
        result = subprocess.run(
            ["node", "-e",
             "const s = require(process.argv[1]); const scopes = JSON.parse(process.argv[2]);"
             "console.log(JSON.stringify(scopes.map(scope => s.prebuiltKey('example/app', '36004204360', '1', scope))))",
             f"{folder}/ci-artifact-store.cjs", json.dumps(scopes)],
            env=env, capture_output=True, text=True, check=True, timeout=30)
        return json.loads(result.stdout)


def test_two_calls_in_one_run_do_not_collide(document: dict) -> None:
    """workflows#144 regression: simulate two `uses:` calls of this reusable
    workflow inside ONE workflow run (same github.run_id/run_attempt, as
    development-deploy-proof PR #5's case_b_match and case_b_nomatch jobs
    both were on run 36004204360) and prove the rendered store keys never
    collide, whether or not either caller passes artifact-scope.
    """
    script = named_step(document["jobs"]["build"], "Resolve prebuilt E2E application")["run"]

    def scope_for(folder: Path, artifact_scope: str) -> str:
        entry = folder / ".output" / "server/index.mjs"
        entry.parent.mkdir(parents=True)
        entry.write_text("// built once")
        returncode, values, stderr = run_resolve(script, folder, ".output", "true", artifact_scope)
        assert returncode == 0, stderr
        return values["scope"]

    # Case A: neither caller knows it shares a run with another call of this
    # workflow -- the random per-call fallback keeps the two keys apart.
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        scope_a = scope_for(Path(a), "")
        scope_b = scope_for(Path(b), "")
    assert scope_a != scope_b, "two unscoped calls in one run produced the same random scope"

    # Case B: the reproducing shape -- each caller passes its own job id as
    # artifact-scope. Scopes render verbatim and stay distinct.
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        scope_match = scope_for(Path(a), "case_b_match")
        scope_nomatch = scope_for(Path(b), "case_b_nomatch")
    assert scope_match == "case_b_match"
    assert scope_nomatch == "case_b_nomatch"

    # An artifact-scope value with characters unsafe in a key segment is
    # sanitized rather than smuggled through verbatim.
    with tempfile.TemporaryDirectory() as folder:
        dirty = scope_for(Path(folder), "Case B: match!/../x")
    assert dirty == "Case-B--match--..-x", dirty

    keys = prebuilt_keys(document, [scope_a, scope_b, scope_match, scope_nomatch, dirty])
    assert len(set(keys)) == len(keys), keys
    assert keys[2] == "prebuilt/example/app/36004204360-1-case_b_match.tar.gz", keys
    assert keys[4] == "prebuilt/example/app/36004204360-1-Case-B--match--..-x.tar.gz", keys
    # The scope reaches the client: without BUILD_SCOPE every call in a run
    # would derive the same key, the original shape of workflows#144.
    publish = named_step(document["jobs"]["build"], PUBLISH)
    assert publish["env"]["BUILD_SCOPE"] == "${{ steps.e2e-build-path.outputs.scope }}"


def run_fallback(document: dict, folder: Path, script: str, build_path: str,
                 build_command: str | None) -> subprocess.CompletedProcess:
    package = {"name": "fixture", "version": "1.0.0", "private": True, "scripts": {}}
    if build_command is not None:
        package["scripts"]["build"] = build_command
    (folder / "package.json").write_text(json.dumps(package))
    step = named_step(document["jobs"]["e2e"], FALLBACK)["run"]
    return subprocess.run(
        ["bash", "-c", step], cwd=folder,
        env={**os.environ, "SCRIPT": script, "PM": "npm", "BUILD_PATH": build_path},
        capture_output=True, text=True, timeout=120,
    )


def test_fallback_build(document: dict) -> None:
    make = "mkdir -p .output/server && echo fresh > .output/server/index.mjs"
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        stale = root / ".output" / "stale.txt"
        stale.parent.mkdir()
        stale.write_text("from a previous build")
        result = run_fallback(document, root, "build", ".output", make)
        assert result.returncode == 0, result.stderr + result.stdout
        assert (root / ".output/server/index.mjs").read_text() == "fresh\n"
        assert not stale.exists(), "the fallback must build from scratch, not over a partial download"
        assert "Built .output in this job" in result.stdout
    for name, script, build_path, command, message in (
        ("empty build-script", "", ".output", make, "build-script is empty"),
        ("missing build script", "build", ".output", None, 'no "build" script exists'),
        ("build leaves nothing", "build", ".output", "true", "left no application at .output"),
        ("build fails", "build", ".output", "exit 3", ""),
        ("absolute path", "build", "/tmp/x", make, "relative directory inside working-directory"),
        ("escaping path", "build", "../x", make, "relative directory inside working-directory"),
    ):
        with tempfile.TemporaryDirectory() as folder:
            result = run_fallback(document, Path(folder), script, build_path, command)
            assert result.returncode != 0, name
            assert message in result.stdout + result.stderr, (name, result.stdout, result.stderr)


def main() -> None:
    document = yaml.safe_load(WORKFLOW.read_text())
    validate(document)
    test_resolve(document)
    test_two_calls_in_one_run_do_not_collide(document)
    test_fallback_build(document)

    # Each named condition must be capable of failing the regression test.
    mutations = []
    for mutate in (
        # Publishing on every run, not only when a build path resolved.
        lambda d: named_step(d["jobs"]["build"], PUBLISH).__setitem__("if", "always()"),
        # The exact shape of workflows#144: a key scoped only to the run.
        lambda d: named_step(d["jobs"]["build"], PUBLISH)["env"].pop("BUILD_SCOPE"),
        # Handing the secret access key to the browser pool.
        lambda d: named_step(d["jobs"]["e2e"], FETCH)["env"].__setitem__(
            "CI_ARTIFACTS_R2_SECRET_ACCESS_KEY", "${{ secrets.CI_ARTIFACTS_R2_SECRET_ACCESS_KEY }}"),
        # Letting a fetch failure pass silently instead of building.
        lambda d: named_step(d["jobs"]["e2e"], FALLBACK).__setitem__(
            "if", "needs.build.outputs.e2e-build-path != ''  && false"),
        lambda d: named_step(d["jobs"]["e2e-quarantine"], FETCH).__setitem__("continue-on-error", True),
        lambda d: named_step(d["jobs"]["e2e"], FETCH)["env"].__setitem__("BUILD_PATH", "."),
        lambda d: named_step(d["jobs"]["e2e"], "Run e2e suite")["env"].pop(
            "E2E_PREBUILT_ARTIFACT"
        ),
        lambda d: d["jobs"]["build"]["outputs"].pop("e2e-build-object"),
        lambda d: d.get(True, d.get("on", {}))["workflow_call"]["inputs"][
            "artifact-scope"
        ].__setitem__("default", "no-scope-should-not-be-required"),
        lambda d: named_step(d["jobs"]["build"], "Resolve prebuilt E2E application")[
            "env"
        ].pop("ARTIFACT_SCOPE"),
    ):
        candidate = deepcopy(document)
        mutate(candidate)
        mutations.append(candidate)

    for candidate in mutations:
        try:
            validate(candidate)
        except (AssertionError, KeyError):
            continue
        raise AssertionError("artifact regression mutation did not fail")

    print(f"prebuilt E2E application contract passed ({len(mutations)} negative mutations)")


if __name__ == "__main__":
    main()
