#!/usr/bin/env python3
"""Behaviour tests for python-data.yml's `extra-env` handling (workflows#4).

This does NOT test a copy of the script. It extracts the `Export caller
environment` step's `run:` block out of the workflow YAML and executes that
exact text under bash with a controlled environment — so the test and the
shipped callable cannot drift apart. If someone edits the step, these tests
either still pass against the new text or they fail; there is no third option
where the test passes while the workflow is broken.

The defect being locked down (workflows#4): `with:` inputs are evaluated in the
CALLER, and a `jobs.<id>.uses:` job is never assigned a runner, so
`${{ github.workspace }}` there expands to the empty string. The runner then
received `PYTHONPATH=`, the step accepted it as a well-formed KEY=VALUE, and the
failure surfaced four steps later as `ModuleNotFoundError` naming nothing.

Run: python3 scripts/test_extra_env.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/python-data.yml")
STEP_NAME = "Export caller environment"


def extract_step_script() -> str:
    doc = yaml.safe_load(WORKFLOW.read_text())
    for step in doc["jobs"]["test"]["steps"]:
        if step.get("name") == STEP_NAME:
            return step["run"]
    raise SystemExit(f"::error::no step named {STEP_NAME!r} in {WORKFLOW}")


def run(script: str, extra_env: str, env_extra: dict[str, str]) -> tuple[int, str, str]:
    """Execute the extracted step exactly as a runner would."""
    with tempfile.TemporaryDirectory() as tmp:
        github_env = Path(tmp) / "github_env"
        github_env.touch()
        env = {
            **os.environ,
            "EXTRA_ENV": extra_env,
            "GITHUB_ENV": str(github_env),
            "RUNNER_TEMP": tmp,
            **env_extra,
        }
        proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        return proc.returncode, github_env.read_text().strip(), proc.stdout + proc.stderr


WORKSPACE = "/home/runner/work/narduk-data/narduk-data"
FIXED = {"GITHUB_WORKSPACE": WORKSPACE, "HOME_ISH": "/x", "EMPTYVAR": "", "MULTI": "a\nb"}

# (label, extra-env input, expected exit status, expected $GITHUB_ENV contents)
CASES: list[tuple[str, str, int, str]] = [
    # --- the issue's motivating example, both spellings ---
    ("issue example, $VAR", "PYTHONPATH=$GITHUB_WORKSPACE", 0, f"PYTHONPATH={WORKSPACE}"),
    ("issue example, ${VAR}", "PYTHONPATH=${GITHUB_WORKSPACE}", 0, f"PYTHONPATH={WORKSPACE}"),
    ("issue example + subpath", "P=${GITHUB_WORKSPACE}/pipelines", 0, f"P={WORKSPACE}/pipelines"),
    ("colon-joined PYTHONPATH idiom", "PYTHONPATH=$GITHUB_WORKSPACE:.", 0, f"PYTHONPATH={WORKSPACE}:."),
    # --- THE ACTUAL BUG: what a caller composing ${{ github.workspace }} sends ---
    ("caller trap: empty value is a hard error", "PYTHONPATH=", 1, ""),
    ("empty-expanding value is a hard error", "X=$TOTALLY_UNSET_XYZ", 1, ""),
    ("defined-but-empty ref is a hard error", "X=$EMPTYVAR", 1, ""),
    # --- literal values keep working: no behaviour change for a plain KEY=VALUE ---
    ("plain literal unchanged", "FOO=bar", 0, "FOO=bar"),
    ("spaces preserved", "X=a b  c", 0, "X=a b  c"),
    ("value containing = preserved", "X=a=b=c", 0, "X=a=b=c"),
    ("multiple lines", "A=$HOME_ISH\nB=lit", 0, "A=/x\nB=lit"),
    ("blank lines skipped", "\nA=1\n\n", 0, "A=1"),
    ("two refs in one value", "P=$HOME_ISH:$HOME_ISH", 0, "P=/x:/x"),
    ("partial expansion still yields a value", "X=a$TOTALLY_UNSET_XYZ", 0, "X=a"),
    # --- expansion is BOUNDED: no shell injection through an env var ---
    ("no command substitution", "X=$(id -u)", 0, "X=$(id -u)"),
    ("no backtick substitution", "X=`id -u`", 0, "X=`id -u`"),
    ("no ${VAR:-default} expansion", "X=${NOPE:-pwned}", 0, "X=${NOPE:-pwned}"),
    ("no glob expansion", "X=*", 0, "X=*"),
    ("lone $ stays literal", "X=100$", 0, "X=100$"),
    ("unterminated ${ stays literal", "X=${OPEN", 0, "X=${OPEN"),
    # --- malformed input fails loudly instead of silently ---
    ("not KEY=VALUE", "JUSTAWORD", 1, ""),
    ("key is not an identifier", "9bad=1", 1, ""),
    ("key contains a space", "a b=1", 1, ""),
    ("multi-line expansion rejected (GITHUB_ENV injection)", "X=$MULTI", 1, ""),
    ("one bad line fails the step, good lines still reported", "GOOD=1\nBAD=", 1, "GOOD=1"),
]


def main() -> int:
    script = extract_step_script()
    failures = 0
    for label, extra_env, want_status, want_env in CASES:
        status, got_env, output = run(script, extra_env, FIXED)
        ok = status == want_status and got_env == want_env
        if ok:
            print(f"PASS  {label}")
        else:
            failures += 1
            print(f"::error::FAIL {label}: status={status} (want {want_status}) "
                  f"env={got_env!r} (want {want_env!r})")
            print(output)
    print(f"\ntest_extra_env: {len(CASES)} case(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
