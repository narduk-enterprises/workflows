#!/usr/bin/env python3
"""Lock the opt-in prebuilt E2E artifact handoff to fail-closed semantics."""

from copy import deepcopy
from pathlib import Path
import os
import subprocess
import tempfile

import yaml


WORKFLOW = Path(".github/workflows/nuxt-cloudflare.yml")
UPLOAD_SHA = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
DOWNLOAD_SHA = (
    "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
)
ARTIFACT_NAME = "e2e-build-${{ github.run_id }}-${{ github.run_attempt }}"


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

    build = document["jobs"]["build"]
    e2e = document["jobs"]["e2e"]
    upload = named_step(build, "Upload prebuilt E2E application")
    download = named_step(e2e, "Download prebuilt E2E application")
    run_e2e = named_step(e2e, "Run e2e suite")

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
            output = root / 'step-output'
            for directory in directories:
                entry = root / directory / 'server/index.mjs'
                entry.parent.mkdir(parents=True)
                entry.write_text('// built once')
            result = subprocess.run(['bash', '-c', script], cwd=root,
                env={**os.environ, 'BUILD_PATH': requested, 'RUN_E2E': enabled,
                     'GITHUB_OUTPUT': str(output)}, capture_output=True, text=True)
            assert (result.returncode != 0) == bool(status), (requested, result.stderr)
            actual = output.read_text() if output.exists() else ''
            assert actual == (f'path={expected}\n' if expected else ''), actual
    identity = named_step(document['jobs']['e2e'], 'Require prebuilt E2E artifact identity')['run']
    for artifact_id, expected in [('12345', 0), ('', 1), ('not-an-id', 1)]:
        result = subprocess.run(['bash', '-c', identity],
            env={**os.environ, 'ARTIFACT_ID': artifact_id}, capture_output=True)
        assert result.returncode == expected
    # A directory symlink outside the checkout must never become an upload.
    with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
        root = Path(folder)
        (root / 'external').symlink_to(outside, target_is_directory=True)
        result = subprocess.run(['bash', '-c', script], cwd=root,
            env={**os.environ, 'BUILD_PATH': 'external', 'RUN_E2E': 'true',
                 'GITHUB_OUTPUT': str(root / 'step-output')}, capture_output=True)
        assert result.returncode != 0


def main() -> None:
    document = yaml.safe_load(WORKFLOW.read_text())
    validate(document)
    test_resolve(document)

    # Each named condition must be capable of failing the regression test.
    mutations = []
    for mutate in (
        lambda d: named_step(d["jobs"]["build"], "Upload prebuilt E2E application")[
            "with"
        ].__setitem__("if-no-files-found", "ignore"),
        lambda d: named_step(
            d["jobs"]["build"], "Upload prebuilt E2E application"
        )["with"].__setitem__("name", "e2e-build"),
        lambda d: named_step(
            d["jobs"]["build"], "Upload prebuilt E2E application"
        )["with"].__setitem__("include-hidden-files", False),
        lambda d: named_step(
            d["jobs"]["e2e"], "Download prebuilt E2E application"
        )["with"].__setitem__("path", "."),
        lambda d: named_step(d["jobs"]["e2e"], "Run e2e suite")["env"].pop(
            "E2E_PREBUILT_ARTIFACT"
        ),
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

    print("prebuilt E2E artifact contract passed (5 negative mutations)")


if __name__ == "__main__":
    main()
