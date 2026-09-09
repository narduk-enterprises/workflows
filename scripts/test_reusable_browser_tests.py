#!/usr/bin/env python3
"""Exercise the standalone browser callable's fail-closed contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

import yaml

import test_playwright_toolchain as toolchain


WORKFLOW = Path(".github/workflows/reusable-browser-tests.yml")
ROUTE = json.dumps(
    {
        "group": "playwright-isolated",
        "labels": [
            "self-hosted",
            "Linux",
            "X64",
            "proxmox-playwright-x64",
        ],
    },
    separators=(",", ":"),
)
LINUX_ROUTE = json.dumps(
    {
        "group": "linux-ci",
        "labels": ["self-hosted", "Linux", "X64", "proxmox", "linux-ci"],
    },
    separators=(",", ":"),
)


def load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def step(document: dict, job: str, name: str) -> dict:
    matches = [
        item
        for item in document["jobs"][job]["steps"]
        if item.get("name") == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {job}/{name} step")
    return matches[0]


def workflow_call(document: dict) -> dict:
    return document.get("on", document.get(True))["workflow_call"]


def validate_structure(document: dict) -> None:
    call = workflow_call(document)
    assert set(call["secrets"]) == {"NARDUK_PLATFORM_GH_PACKAGES_READ"}
    assert call["secrets"]["NARDUK_PLATFORM_GH_PACKAGES_READ"]["required"] is False
    for name in ("linux-runner", "browser-runner", "build-artifact-path",
                 "build-artifact-marker", "playwright-version"):
        assert call["inputs"][name]["required"] is True
    assert call["inputs"]["shards"]["default"] == 3
    assert call["inputs"]["run-webkit"]["default"] is False
    assert set(call["outputs"]) == {
        "build-artifact",
        "report-artifact",
        "playwright-version",
    }

    jobs = document["jobs"]
    assert jobs["contract"]["runs-on"] == "ubuntu-latest"
    assert jobs["chromium"]["runs-on"] == "${{ fromJSON(inputs.browser-runner) }}"
    assert jobs["webkit"]["runs-on"] == "${{ fromJSON(inputs.browser-runner) }}"
    assert jobs["validate"]["runs-on"] == "${{ fromJSON(inputs.linux-runner) }}"
    assert jobs["report"]["runs-on"] == "${{ fromJSON(inputs.linux-runner) }}"
    assert jobs["required"]["runs-on"] == "${{ fromJSON(vars.CI_LIGHTWEIGHT_RUNNER || '\"ubuntu-latest\"') }}"
    assert jobs["validate"]["needs"] == "contract"
    assert jobs["chromium"]["needs"] == ["contract", "validate"]
    assert jobs["webkit"]["needs"] == ["contract", "validate", "chromium"]
    assert jobs["chromium"]["strategy"]["max-parallel"] == 3
    assert jobs["webkit"]["strategy"]["max-parallel"] == 3
    assert jobs["chromium"]["env"]["E2E_PREBUILT_ARTIFACT"] == "1"
    assert jobs["webkit"]["env"]["E2E_PREBUILT_ARTIFACT"] == "1"
    assert jobs["required"]["needs"] == [
        "contract",
        "validate",
        "chromium",
        "webkit",
        "report",
    ]

    chromium_assertion = step(
        document, "chromium", "Assert isolated Playwright toolchain"
    )["run"]
    webkit_assertion = step(
        document, "webkit", "Assert isolated Playwright toolchain"
    )["run"]
    assert chromium_assertion == webkit_assertion
    assert "EXPECTED_PLAYWRIGHT_VERSION" in chromium_assertion
    assert "browser manifest mismatch" in chromium_assertion
    assert "launch canary" in chromium_assertion

    text = WORKFLOW.read_text()
    for forbidden in (
        "playwright install",
        "--if-present",
        "continue-on-error",
        "secrets: inherit",
    ):
        assert forbidden not in text
    assert "proxmox-playwright-x64" in text
    assert "playwright-isolated" in text

    for job in ("chromium", "webkit"):
        upload = step(
            document,
            job,
            f"Upload {'Chromium' if job == 'chromium' else 'WebKit'} blob report",
        )
        assert upload["if"] == "always()"
        assert upload["with"]["retention-days"] == 1
        assert "${{ github.run_id }}" in upload["with"]["name"]
        assert "${{ github.run_attempt }}" in upload["with"]["name"]

    for job in ("chromium", "webkit", "report"):
        auth_env = step(document, job, "Configure package registry auth")["env"]
        assert auth_env["NARDUK_PLATFORM_GH_PACKAGES_READ"] == (
            "${{ secrets.NARDUK_PLATFORM_GH_PACKAGES_READ || github.token }}"
        )

    report_upload = step(document, "report", "Upload merged HTML and traces")
    assert report_upload["with"]["retention-days"] == 14
    assert report_upload["with"]["if-no-files-found"] == "error"
    merge = step(document, "report", "Merge blob reports")["run"]
    assert "exit 1" in merge
    assert "no blob report" in merge

    required = step(document, "required", "Require every enabled browser gate")[
        "run"
    ]
    for name in ("validate", "chromium", "report", "webkit"):
        assert name in required
    assert "require_success chromium" in required
    assert "require_success webkit" in required


def run_contract(document: dict, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "BROWSER_ROUTE": ROUTE,
        "BUILD_ARTIFACT_PATH": "apps/web/.output",
        "BUILD_ARTIFACT_MARKER": "server/index.mjs",
        "E2E_SCRIPT": "test:e2e:ci",
        "EXPECTED_PLAYWRIGHT_VERSION": toolchain.VERSION,
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": "1234",
        "LINUX_ROUTE": LINUX_ROUTE,
        "PACKAGE_MANAGER": "pnpm",
        "SHARDS": "3",
        "WORKING_DIRECTORY": ".",
        **overrides,
    }
    with tempfile.NamedTemporaryFile() as output:
        env["GITHUB_OUTPUT"] = output.name
        return subprocess.run(
            [
                "bash",
                "-c",
                step(document, "contract", "Validate dedicated route and inputs")[
                    "run"
                ],
            ],
            env=env,
            capture_output=True,
            text=True,
        )


def run_toolchain(
    document: dict,
    fixture: dict[str, Path],
    expected_version: str,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "EXPECTED_PLAYWRIGHT_VERSION": expected_version,
        "GITHUB_STEP_SUMMARY": str(fixture["summary"]),
        "GITHUB_WORKSPACE": str(fixture["project"]),
        "PLAYWRIGHT_ALLOWED_BROWSER_PREFIX": str(fixture["visible"].parent),
        "PLAYWRIGHT_BROWSERS_PATH": str(fixture["visible"]),
        "PLAYWRIGHT_TOOLCHAIN_OWNER_UID": str(os.getuid()),
        "PLAYWRIGHT_TOOLCHAIN_ROOT": str(fixture["image"]),
        "REQUIRED_BROWSERS": "chromium",
        "RUNNER_TEMP": str(fixture["runner_temp"]),
    }
    return subprocess.run(
        [
            "bash",
            "-c",
            step(document, "chromium", "Assert isolated Playwright toolchain")[
                "run"
            ],
        ],
        cwd=fixture["project"],
        env=env,
        capture_output=True,
        text=True,
    )


def main() -> None:
    document = load()
    validate_structure(document)

    good = run_contract(document)
    assert good.returncode == 0, good.stdout + good.stderr

    bad_cases = [
        {"BROWSER_ROUTE": json.dumps({"group": "linux-ci", "labels": []})},
        {"BROWSER_ROUTE": json.dumps(["proxmox-playwright-x64"])},
        {"LINUX_ROUTE": json.dumps(["self-hosted", "linux-ci"])},
        {"BUILD_ARTIFACT_PATH": "../../host"},
        {"WORKING_DIRECTORY": "/tmp/escape"},
        {"PACKAGE_MANAGER": "yarn"},
        {"EXPECTED_PLAYWRIGHT_VERSION": "^1.61.1"},
        {"SHARDS": "0"},
    ]
    for case in bad_cases:
        result = run_contract(document, **case)
        assert result.returncode != 0, f"contract mutation passed: {case}"

    with tempfile.TemporaryDirectory() as temp:
        fixture = toolchain.make_fixture(Path(temp) / "toolchain")
        exact = run_toolchain(document, fixture, toolchain.VERSION)
        assert exact.returncode == 0, exact.stdout + exact.stderr
        skew = run_toolchain(document, fixture, "1.59.1")
        assert skew.returncode != 0
        assert "does not equal required exact version" in skew.stdout + skew.stderr

    print("reusable browser contract passed (8 route/input failures, version-skew failure, exact image pass)")


if __name__ == "__main__":
    main()
