#!/usr/bin/env python3
"""Lock the opt-in prebuilt E2E artifact handoff to fail-closed semantics."""

from copy import deepcopy
from pathlib import Path
import os
import re
import subprocess
import tempfile

import yaml


WORKFLOW = Path(".github/workflows/nuxt-cloudflare.yml")
UPLOAD_SHA = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
DOWNLOAD_SHA = (
    "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
)
# workflows#144: a bare "e2e-build-<run_id>-<run_attempt>" collides when this
# reusable workflow is called more than once in one run (two `uses:` jobs
# share github.run_id/run_attempt) -- GitHub's list-artifacts-by-run response
# then folds both uploads into one, and whichever caller captured the OTHER
# upload's numeric ID fails download with "None of the provided artifact IDs
# were found" (reproduced in development-deploy-proof PR #5, run 36004204360,
# case_b_nomatch / E2E (1)). The per-call `scope` step output closes that.
ARTIFACT_NAME = (
    "e2e-build-${{ github.run_id }}-${{ github.run_attempt }}"
    "-${{ steps.e2e-build-path.outputs.scope }}"
)


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
    e2e = document["jobs"]["e2e"]
    resolve = named_step(build, "Resolve prebuilt E2E application")
    upload = named_step(build, "Upload prebuilt E2E application")
    download = named_step(e2e, "Download prebuilt E2E application")
    run_e2e = named_step(e2e, "Run e2e suite")

    assert resolve["env"]["ARTIFACT_SCOPE"] == "${{ inputs.artifact-scope }}"
    assert upload["if"] == "steps.e2e-build-path.outputs.path != ''"
    assert build["outputs"]["e2e-build-id"] == "${{ steps.e2e-build-upload.outputs.artifact-id }}"
    assert upload["id"] == "e2e-build-upload"
    assert upload["uses"] == UPLOAD_SHA
    assert upload["with"] == {
        "name": ARTIFACT_NAME,
        "path": "${{ inputs.working-directory }}/${{ steps.e2e-build-path.outputs.path }}",
        "if-no-files-found": "error",
        "include-hidden-files": True,
        "retention-days": 1,
    }
    assert download["if"] == "needs.build.outputs.e2e-build-path != ''"
    assert download["uses"] == DOWNLOAD_SHA
    assert download["with"] == {
        "artifact-ids": "${{ needs.build.outputs.e2e-build-id }}",
        "merge-multiple": True,
        "path": "${{ inputs.working-directory }}/${{ needs.build.outputs.e2e-build-path }}",
    }
    assert (
        run_e2e["env"]["E2E_PREBUILT_ARTIFACT"]
        == "${{ needs.build.outputs.e2e-build-path != '' && '1' || '' }}"
    )

    build_names = [step.get("name") for step in build["steps"]]
    e2e_names = [step.get("name") for step in e2e["steps"]]
    assert build_names.index("Extra scripts") < build_names.index(
        "Upload prebuilt E2E application"
    )
    assert e2e_names.index("Download prebuilt E2E application") < e2e_names.index(
        "Run e2e suite"
    )


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
                # still be a stable-shaped, artifact-name-safe token.
                assert re.fullmatch(r'[0-9a-f]{12}', values['scope']), values
            else:
                assert values == {}, values
    identity = named_step(document['jobs']['e2e'], 'Require prebuilt E2E artifact identity')['run']
    for artifact_id, expected in [('12345', 0), ('', 1), ('not-an-id', 1)]:
        result = subprocess.run(['bash', '-c', identity],
            env={**os.environ, 'ARTIFACT_ID': artifact_id}, capture_output=True)
        assert result.returncode == expected
    # A directory symlink outside the checkout must never become an upload.
    with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
        root = Path(folder)
        (root / 'external').symlink_to(outside, target_is_directory=True)
        returncode, _values, _stderr = run_resolve(script, root, 'external', 'true')
        assert returncode != 0


def test_two_calls_in_one_run_do_not_collide(document: dict) -> None:
    """workflows#144 regression: simulate two `uses:` calls of this reusable
    workflow inside ONE workflow run (same github.run_id/run_attempt, as
    development-deploy-proof PR #5's case_b_match and case_b_nomatch jobs
    both were on run 36004204360) and prove the rendered e2e-build artifact
    names never collide, whether or not either caller passes artifact-scope.
    """
    script = named_step(document["jobs"]["build"], "Resolve prebuilt E2E application")["run"]
    name_template = named_step(document["jobs"]["build"], "Upload prebuilt E2E application")["with"]["name"]
    run_id, run_attempt = "36004204360", "1"  # the reproducing run, for realism

    def scope_for(folder: Path, artifact_scope: str) -> str:
        entry = folder / ".output" / "server/index.mjs"
        entry.parent.mkdir(parents=True)
        entry.write_text("// built once")
        returncode, values, stderr = run_resolve(script, folder, ".output", "true", artifact_scope)
        assert returncode == 0, stderr
        return values["scope"]

    def rendered_name(scope: str) -> str:
        name = name_template
        name = name.replace("${{ github.run_id }}", run_id)
        name = name.replace("${{ github.run_attempt }}", run_attempt)
        name = name.replace("${{ steps.e2e-build-path.outputs.scope }}", scope)
        assert "${{" not in name, name
        return name

    # Case A: neither caller knows it shares a run with another call of this
    # workflow (today's un-migrated shape) -- the random per-call fallback
    # must keep the two rendered names apart anyway.
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        scope_a = scope_for(Path(a), "")
        scope_b = scope_for(Path(b), "")
    assert scope_a != scope_b, "two unscoped calls in one run produced the same random scope"
    assert rendered_name(scope_a) != rendered_name(scope_b)

    # Case B: the actual reproducing shape -- each caller passes its own job
    # id as artifact-scope (case_b_match / case_b_nomatch). Scopes render
    # verbatim (deterministic, human-readable) and stay distinct.
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        scope_match = scope_for(Path(a), "case_b_match")
        scope_nomatch = scope_for(Path(b), "case_b_nomatch")
    assert scope_match == "case_b_match"
    assert scope_nomatch == "case_b_nomatch"
    assert rendered_name(scope_match) != rendered_name(scope_nomatch)

    # An artifact-scope value with characters unsafe in an artifact name is
    # sanitized rather than smuggled through verbatim.
    with tempfile.TemporaryDirectory() as folder:
        dirty = scope_for(Path(folder), "Case B: match!/../x")
    assert dirty == "Case-B--match--..-x", dirty
    assert re.fullmatch(r"[A-Za-z0-9._-]+", dirty)

    # The template itself must still carry the scope placeholder -- this is
    # the exact shape of the original bug (workflows#144): a name built only
    # from github.run_id/run_attempt is identical for every call in one run,
    # which is what actually collided in development-deploy-proof PR #5.
    unscoped_template = name_template.replace(
        "-${{ steps.e2e-build-path.outputs.scope }}", ""
    )
    assert unscoped_template != name_template
    assert unscoped_template == "e2e-build-${{ github.run_id }}-${{ github.run_attempt }}"


def main() -> None:
    document = yaml.safe_load(WORKFLOW.read_text())
    validate(document)
    test_resolve(document)
    test_two_calls_in_one_run_do_not_collide(document)

    # Each named condition must be capable of failing the regression test.
    mutations = []
    for mutate in (
        lambda d: named_step(d["jobs"]["build"], "Upload prebuilt E2E application")[
            "with"
        ].__setitem__("if-no-files-found", "ignore"),
        lambda d: named_step(
            d["jobs"]["build"], "Upload prebuilt E2E application"
        )["with"].__setitem__("name", "e2e-build"),
        # The exact shape of workflows#144: a name scoped only to the run,
        # not the call, which is what actually collided in
        # development-deploy-proof PR #5 (run 36004204360).
        lambda d: named_step(
            d["jobs"]["build"], "Upload prebuilt E2E application"
        )["with"].__setitem__(
            "name", "e2e-build-${{ github.run_id }}-${{ github.run_attempt }}"
        ),
        lambda d: named_step(
            d["jobs"]["build"], "Upload prebuilt E2E application"
        )["with"].__setitem__("include-hidden-files", False),
        lambda d: named_step(
            d["jobs"]["e2e"], "Download prebuilt E2E application"
        )["with"].__setitem__("path", "."),
        lambda d: named_step(d["jobs"]["e2e"], "Run e2e suite")["env"].pop(
            "E2E_PREBUILT_ARTIFACT"
        ),
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

    print(f"prebuilt E2E artifact contract passed ({len(mutations)} negative mutations)")


if __name__ == "__main__":
    main()
