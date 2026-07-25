#!/usr/bin/env python3
"""Behaviour tests for node-library.yml's script gates and `require-scripts`.

Like `test_extra_env.py`, this does NOT test a copy: it extracts each gate's
`run:` block out of the workflow YAML and executes that exact text under bash
against real `package.json` fixtures. If someone edits a gate, these tests
either still pass against the new text or they fail.

The defect being locked down: every gate runs `--if-present`, so a gate whose
script does not exist matches nothing, exits 0, and reports a GREEN lane that
ran no tests at all. `run-tests: true` is a caller asserting tests exist.
narduk-libs hit this — its packages define `test:unit`, not `test`.

Run: python3 scripts/test_script_gates.py
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

import yaml

WORKFLOW = pathlib.Path(".github/workflows/node-library.yml")
GATES = {"Lint": "lint", "Typecheck": "typecheck", "Test": "test", "Build": "build"}


def gate_script(name: str) -> str:
    doc = yaml.safe_load(WORKFLOW.read_text())
    for step in doc["jobs"]["package"]["steps"]:
        if step.get("name") == name:
            return step["run"]
    raise SystemExit(f"::error::no step named {name!r} in {WORKFLOW}")


def run(script: str, fixture: dict, *, script_name: str, require: str) -> tuple[int, str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        pathlib.Path(tmp, "package.json").write_text(json.dumps({"name": "f", "scripts": fixture}))
        summary = pathlib.Path(tmp, "summary")
        summary.touch()
        env = {
            **os.environ,
            "SCRIPT": script_name,
            "FILTER": "",
            "PM": "npm",
            "REQUIRE": require,
            "GITHUB_STEP_SUMMARY": str(summary),
        }
        p = subprocess.run(["bash", "-c", script], cwd=tmp, env=env, capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr, summary.read_text()


def main() -> int:
    failures = 0
    for gate, script_name in GATES.items():
        script = gate_script(gate)
        cases = [
            # (label, fixture scripts, require-scripts, expected rc, expected substring)
            ("missing + require=false -> warns but PASSES (today's default)",
             {"other": "echo x"}, "false", 0, "::warning::"),
            ("missing + require=true  -> FAILS loudly",
             {"other": "echo x"}, "true", 1, "::error::"),
            ("present + require=false -> actually runs the script",
             {script_name: "echo RAN"}, "false", 0, "RAN"),
            ("present + require=true  -> actually runs the script",
             {script_name: "echo RAN"}, "true", 0, "RAN"),
            ("declared but EMPTY -> counts as missing, not as a script",
             {script_name: ""}, "true", 1, "::error::"),
        ]
        for label, fixture, require, want_rc, want_sub in cases:
            rc, out, summary = run(script, fixture, script_name=script_name, require=require)
            ok = rc == want_rc and want_sub in out
            if ok and "missing" in label:
                # The loss must be visible in the job summary even when it passes.
                ok = ":warning:" in summary
            print(("PASS  " if ok else "FAIL  ") + f"{gate:10s} {label}")
            if not ok:
                failures += 1
                print(f"      rc={rc} (want {want_rc}); output:\n{out.strip()[:500]}")
    total = len(GATES) * 5
    print(f"\ntest_script_gates: {total} case(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
