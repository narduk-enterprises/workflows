#!/usr/bin/env python3
"""Prove Apple routing stays split and the Xcode job cannot become echo-only."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile

import yaml


WORKFLOW = Path(".github/workflows/apple.yml")


def step(document: dict, name: str) -> dict:
    matches = [
        item for item in document["jobs"]["xcode"]["steps"] if item.get("name") == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one xcode/{name} step")
    return matches[0]


def run_gate(
    document: dict,
    fake_bin: str,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            step(document, "Validate Apple toolchain and gate configuration")["run"],
        ],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "BUILD_COMMAND": "swift build",
            "RUN_BUILD": "true",
            "RUN_TESTS": "true",
            "TEST_COMMAND": "swift test",
            **overrides,
        },
        capture_output=True,
        text=True,
    )


def main() -> None:
    document = yaml.safe_load(WORKFLOW.read_text())
    call = document.get("on", document.get(True))["workflow_call"]
    assert "secrets" not in call
    assert call["inputs"]["apple-runner"]["required"] is True
    assert document["jobs"]["lint"]["runs-on"] == "${{ fromJSON(inputs.lint-runner) }}"
    assert document["jobs"]["xcode"]["runs-on"] == "${{ fromJSON(inputs.apple-runner) }}"
    assert document["jobs"]["required"]["runs-on"] == "${{ fromJSON(inputs.lint-runner) }}"
    assert document["jobs"]["required"]["needs"] == ["lint", "xcode"]
    install_swiftlint = next(
        item
        for item in document["jobs"]["lint"]["steps"]
        if item.get("name") == "Install SwiftLint (official Linux release binary)"
    )["run"]
    assert "sha256sum --check --strict" in install_swiftlint
    assert "python3 -m zipfile -e" in install_swiftlint
    assert 'find "$dest" -type f -name swiftlint -print -quit' in install_swiftlint
    assert "/usr/lib/libsourcekitdInProc.so" in install_swiftlint
    assert "LINUX_SOURCEKIT_LIB_PATH=" in install_swiftlint
    swiftlint = next(
        item
        for item in document["jobs"]["lint"]["steps"]
        if item.get("name") == "SwiftLint"
    )["run"]
    assert '${RUNNER_TEMP}/swiftlint-cache' in swiftlint
    assert '--cache-path "$cache_path"' in swiftlint

    validation = step(
        document, "Validate Apple toolchain and gate configuration"
    )["run"]
    assert "|| true" not in validation
    assert "xcodebuild -version" in validation
    assert "swift --version" in validation
    text = WORKFLOW.read_text().lower()
    for forbidden in ("codesign", "keychain", "notarize", "testflight", "sparkle"):
        # Scope comments explain why release/signing is excluded; no command may
        # invoke it.
        assert all(
            forbidden not in str(item.get("run", "")).lower()
            for job in document["jobs"].values()
            for item in job.get("steps", [])
        )

    with tempfile.TemporaryDirectory() as temp:
        fake_bin = Path(temp)
        for command in ("xcodebuild", "swift"):
            path = fake_bin / command
            path.write_text("#!/bin/sh\nprintf '%s\\n' fake-toolchain\n")
            path.chmod(0o755)

        assert run_gate(document, temp).returncode == 0
        for case in (
            {"RUN_BUILD": "false", "RUN_TESTS": "false"},
            {"RUN_BUILD": "true", "BUILD_COMMAND": ""},
            {"RUN_TESTS": "true", "TEST_COMMAND": ""},
        ):
            result = run_gate(document, temp, **case)
            assert result.returncode != 0, f"Apple gate mutation passed: {case}"

    print("Apple gate passed (per-job routing; echo-only and empty commands fail)")


if __name__ == "__main__":
    main()
