#!/usr/bin/env python3
"""Lock python-data's Pyright and fail-capable configuration gates."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import yaml


WORKFLOW = Path(".github/workflows/python-data.yml")


def step(document: dict, name: str) -> dict:
    matches = [
        item for item in document["jobs"]["test"]["steps"] if item.get("name") == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one test/{name} step")
    return matches[0]


def run_config(document: dict, **overrides: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            step(document, "Validate dependency manager and fail-capable gates")[
                "run"
            ],
        ],
        env={
            **os.environ,
            "DEPENDENCY_MANAGER": "uv",
            "EXTRA_CHECKS": "",
            "RUN_PYRIGHT": "true",
            "RUN_TESTS": "false",
            **overrides,
        },
        capture_output=True,
        text=True,
    )


def main() -> None:
    document = yaml.safe_load(WORKFLOW.read_text())
    call = document.get("on", document.get(True))["workflow_call"]
    assert call["inputs"]["run-pyright"]["default"] is False
    assert call["inputs"]["pyright-version"]["default"] == "1.1.411"

    setup_node = next(
        item
        for item in document["jobs"]["test"]["steps"]
        if str(item.get("uses", "")).startswith("actions/setup-node@")
    )
    assert (
        setup_node["uses"]
        == "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020"
    )
    assert setup_node["if"] == "inputs.run-pyright"
    assert setup_node["with"]["node-version"] == "24"
    assert setup_node["with"]["package-manager-cache"] is False

    setup_uv = next(
        item
        for item in document["jobs"]["test"]["steps"]
        if str(item.get("uses", "")).startswith("astral-sh/setup-uv@")
    )
    assert setup_uv["uses"] == (
        "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9"
    )

    install = step(document, "Install pinned Pyright")
    assert install["if"] == "inputs.run-pyright"
    assert "pyright@${PYRIGHT_VERSION}" in install["run"]
    assert "--ignore-scripts" in install["run"]
    static = step(document, "Pyright")
    assert static["if"] == "inputs.run-pyright"
    assert static["run"] == "pyright ${{ inputs.pyright-args }}"
    assert all(
        "py_compile" not in str(item.get("run", ""))
        for job in document["jobs"].values()
        for item in job.get("steps", [])
    )

    assert run_config(document).returncode == 0
    for case in (
        {"DEPENDENCY_MANAGER": "ambient"},
        {"RUN_PYRIGHT": "false", "RUN_TESTS": "false", "EXTRA_CHECKS": ""},
    ):
        result = run_config(document, **case)
        assert result.returncode != 0, f"configuration mutation passed: {case}"

    print("python static gate passed (Pyright pinned; invalid manager and install-only gate fail)")


if __name__ == "__main__":
    main()
