#!/usr/bin/env python3
"""Lock the opt-in prebuilt E2E artifact handoff to fail-closed semantics."""

from copy import deepcopy
from pathlib import Path

import yaml


WORKFLOW = Path(".github/workflows/nuxt-cloudflare.yml")
UPLOAD_SHA = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
DOWNLOAD_SHA = (
    "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
)
ARTIFACT_NAME = "e2e-build-${{ github.run_id }}-${{ github.run_attempt }}"
ARTIFACT_PATH = (
    "${{ inputs.working-directory }}/${{ inputs.e2e-build-artifact-path }}"
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
    assert artifact_input["default"] == ""

    build = document["jobs"]["build"]
    e2e = document["jobs"]["e2e"]
    upload = named_step(build, "Upload prebuilt E2E application")
    download = named_step(e2e, "Download prebuilt E2E application")
    run_e2e = named_step(e2e, "Run e2e suite")

    condition = "inputs.e2e-build-artifact-path != ''"
    assert upload["if"] == condition
    assert upload["uses"] == UPLOAD_SHA
    assert upload["with"] == {
        "name": ARTIFACT_NAME,
        "path": ARTIFACT_PATH,
        "if-no-files-found": "error",
        "retention-days": 1,
    }
    assert download["if"] == condition
    assert download["uses"] == DOWNLOAD_SHA
    assert download["with"] == {
        "name": ARTIFACT_NAME,
        "path": ARTIFACT_PATH,
    }
    assert (
        run_e2e["env"]["E2E_PREBUILT_ARTIFACT"]
        == "${{ inputs.e2e-build-artifact-path != '' && '1' || '' }}"
    )

    build_names = [step.get("name") for step in build["steps"]]
    e2e_names = [step.get("name") for step in e2e["steps"]]
    assert build_names.index("Extra scripts") < build_names.index(
        "Upload prebuilt E2E application"
    )
    assert e2e_names.index("Download prebuilt E2E application") < e2e_names.index(
        "Run e2e suite"
    )


def main() -> None:
    document = yaml.safe_load(WORKFLOW.read_text())
    validate(document)

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

    print("prebuilt E2E artifact contract passed (4 negative mutations)")


if __name__ == "__main__":
    main()
