#!/usr/bin/env python3
"""Behaviour tests for node-library.yml's `Required` job package-result gate.

Like `test_script_gates.py`, this does NOT test a copy: it extracts the
`Require every package lane to succeed` step's `run:` text directly out of
the workflow YAML and executes that exact text under bash with
`PACKAGE_RESULT` set to each value GitHub can report for the `package` job.
If someone edits the case statement, this test either still passes against
the new text or it fails.

An empty matrix must skip at job level before GitHub expands the strategy.
Only that explicit no-op may pass a skipped dependency; failures, cancellation
and skipped nonempty selections fail closed. CI also calls the real workflow
with [] because a shell fixture cannot validate GitHub matrix expansion.

Run: python3 scripts/test_required_gate.py
"""

from __future__ import annotations

import pathlib
import subprocess

import yaml

NODE_LIB = pathlib.Path(".github/workflows/node-library.yml")


def gate_script() -> str:
    doc = yaml.safe_load(NODE_LIB.read_text())
    for step in doc["jobs"]["required"]["steps"]:
        if step.get("name") == "Require every package lane to succeed":
            return step["run"]
    raise SystemExit(f"::error::no 'Require every package lane to succeed' step in {NODE_LIB}")


def run(script: str, package_result: str, matrix_empty: bool) -> tuple[int, str, str]:
    completed = subprocess.run(
        ["bash", "-c", script],
        env={"PACKAGE_RESULT": package_result, "PATH": "/usr/bin:/bin", "MATRIX_EMPTY": str(matrix_empty).lower()},
        capture_output=True,
        text=True,
    )
    return completed.returncode, completed.stdout, completed.stderr


CASES: list[tuple[str, str, bool, int]] = [
    ("a successful package matrix passes", "success", False, 0),
    (
        "an intentionally empty package-matrix (skipped) passes",
        "skipped",
        True,
        0,
    ),
    ("a failed package lane fails the gate", "failure", False, 1),
    ("a cancelled package lane fails the gate", "cancelled", False, 1),
    ("skipped nonempty selection fails", "skipped", False, 1),
    ("failure with empty input still fails", "failure", True, 1),
]


def main() -> None:
    script = gate_script()
    failures: list[str] = []

    for description, package_result, matrix_empty, expected_code in CASES:
        code, _stdout, stderr = run(script, package_result, matrix_empty)
        if code != expected_code:
            failures.append(
                f"{description}: PACKAGE_RESULT={package_result!r} expected exit "
                f"{expected_code}, got {code} (stderr: {stderr.strip()!r})"
            )

    if failures:
        for failure in failures:
            print(f"::error::{failure}")
        raise SystemExit(1)

    print(f"test_required_gate: {len(CASES)} case(s) passed")


if __name__ == "__main__":
    main()
