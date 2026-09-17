#!/usr/bin/env python3
"""Behaviour tests for nuxt-cloudflare.yml's `Dependency audit` gate (DA1).

Like `test_script_gates.py` and `test_required_gate.py`, this does NOT test a
copy of the parser: it extracts the shipped `Dependency audit` step's `run:`
text straight out of the workflow YAML and executes that exact text under bash,
with a fake `pnpm`/`npm` on PATH that emits a fixture audit report. If someone
edits the gate, this test either still passes against the new text or it fails.

What the gate promises, and what each case below pins down:

  * a high/critical advisory WITH a published fix fails the build (exit 1)
  * a high/critical advisory with NO published fix passes with a `::warning::`
  * moderate/low never blocks
  * both report shapes parse -- pnpm/npm6 `advisories` (fixability =
    `patched_versions` not the "<0.0.0" no-patch sentinel) and npm 7+
    `auditReportVersion: 2` `vulnerabilities` (fixability = `fixAvailable`)
  * `audit-ignore` suppresses by GHSA id ONLY with a written reason; an entry
    without one fails the gate rather than silently muting an advisory
  * a stale `audit-ignore` entry warns so it gets removed
  * a missing, empty, non-JSON or unrecognised report is a HARD FAILURE --
    "the audit did not run" must never look like "the audit found nothing"

Run: python3 scripts/test_dependency_audit.py
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile

import yaml

NUXT_CF = pathlib.Path(".github/workflows/nuxt-cloudflare.yml")
STEP_NAME = "Dependency audit"


def gate_script() -> str:
    doc = yaml.safe_load(NUXT_CF.read_text())
    for step in doc["jobs"]["build"]["steps"]:
        if step.get("name") == STEP_NAME:
            return step["run"]
    raise SystemExit(f"::error::no '{STEP_NAME}' step in {NUXT_CF} job 'build'")


# --- fixtures -------------------------------------------------------------
# pnpm / npm 6 shape. "<0.0.0" is that format's explicit "no patch exists".
def pnpm_report(*advisories: dict) -> str:
    return json.dumps(
        {
            "actions": [],
            "advisories": {str(a["id"]): a for a in advisories},
            "muted": [],
            "metadata": {"vulnerabilities": {"info": 0, "low": 0, "moderate": 0, "high": 0, "critical": 0}},
        }
    )


def pnpm_advisory(ident: int, severity: str, patched: str, ghsa: str, module: str = "left-pad") -> dict:
    return {
        "id": ident,
        "github_advisory_id": ghsa,
        "severity": severity,
        "module_name": module,
        "patched_versions": patched,
        "title": f"{severity} thing in {module}",
        "url": f"https://github.com/advisories/{ghsa}",
        "findings": [],
        "cves": [],
    }


# npm 7+ shape.
def npm7_report(*vulns: dict) -> str:
    return json.dumps(
        {
            "auditReportVersion": 2,
            "vulnerabilities": {v["name"]: v for v in vulns},
            "metadata": {"vulnerabilities": {"info": 0, "low": 0, "moderate": 0, "high": 0, "critical": 0, "total": 0}},
        }
    )


def npm7_vuln(name: str, severity: str, fix_available, ghsa: str, source: int = 1088820) -> dict:
    return {
        "name": name,
        "severity": severity,
        "isDirect": True,
        "via": [
            {
                "source": source,
                "name": name,
                "dependency": name,
                "title": f"{severity} thing in {name}",
                "url": f"https://github.com/advisories/{ghsa}",
                "severity": severity,
                "range": "<1.2.3",
            }
        ],
        "effects": [],
        "range": "<1.2.3",
        "nodes": [f"node_modules/{name}"],
        "fixAvailable": fix_available,
    }


CLEAN_PNPM = pnpm_report()
FIXABLE_HIGH = pnpm_report(pnpm_advisory(1234, "high", ">=1.2.3", "GHSA-aaaa-bbbb-cccc"))
UNFIXABLE_HIGH = pnpm_report(pnpm_advisory(1234, "high", "<0.0.0", "GHSA-aaaa-bbbb-cccc"))
FIXABLE_CRITICAL = pnpm_report(pnpm_advisory(99, "critical", ">=2.0.0", "GHSA-dddd-eeee-ffff", "tar"))
FIXABLE_MODERATE = pnpm_report(pnpm_advisory(77, "moderate", ">=3.0.0", "GHSA-1111-2222-3333", "minimist"))
NO_GHSA_HIGH = pnpm_report(
    {
        "id": 4242,
        "severity": "high",
        "module_name": "no-ghsa-pkg",
        "patched_versions": ">=9.9.9",
        "title": "advisory with no GHSA id",
        "url": "https://npmjs.com/advisories/4242",
    }
)
MIXED = pnpm_report(
    pnpm_advisory(1234, "high", ">=1.2.3", "GHSA-aaaa-bbbb-cccc"),
    pnpm_advisory(99, "critical", "<0.0.0", "GHSA-dddd-eeee-ffff", "tar"),
)

NPM7_FIXABLE_HIGH = npm7_report(
    npm7_vuln("axios", "high", {"name": "axios", "version": "1.7.4", "isSemVerMajor": False}, "GHSA-8hc4-vh64-cxmj")
)
NPM7_FIXABLE_MAJOR = npm7_report(npm7_vuln("axios", "critical", True, "GHSA-8hc4-vh64-cxmj"))
NPM7_UNFIXABLE_HIGH = npm7_report(npm7_vuln("axios", "high", False, "GHSA-8hc4-vh64-cxmj"))
NPM7_CLEAN = npm7_report()

UNKNOWN_SHAPE = json.dumps({"metadata": {"vulnerabilities": {}}, "somethingElse": []})


# --- harness --------------------------------------------------------------
def run_case(script: str, *, pm: str, stdout: str, exit_code: int, audit_ignore: str = "") -> tuple[int, str, str]:
    """Execute the shipped gate text with a fake package manager on PATH."""
    tmp = tempfile.mkdtemp(prefix="dependency-audit-")
    try:
        bindir = pathlib.Path(tmp, "bin")
        bindir.mkdir()
        payload = pathlib.Path(tmp, "payload.json")
        payload.write_text(stdout)
        shim = bindir / pm
        # The shim ignores its arguments deliberately: this test is about the
        # gate's reading of the report, not about re-asserting the flags (the
        # flags are asserted separately below, against the shipped text).
        shim.write_text(f'#!/bin/sh\ncat "{payload}"\nexit {exit_code}\n')
        shim.chmod(0o755)

        summary = pathlib.Path(tmp, "summary.md")
        summary.write_text("")
        env = {
            "PATH": f"{bindir}:/usr/bin:/bin",
            "PM": pm,
            "AUDIT_IGNORE": audit_ignore,
            "RUNNER_TEMP": tmp,
            "GITHUB_STEP_SUMMARY": str(summary),
            "HOME": tmp,
        }
        proc = subprocess.run(
            ["bash", "-c", script], cwd=tmp, env=env, capture_output=True, text=True
        )
        return proc.returncode, proc.stdout + proc.stderr, summary.read_text()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


CASES: list[tuple[str, dict, int, list[str], list[str]]] = [
    # (label, run_case kwargs, expected rc, must contain, must NOT contain)
    (
        "pnpm: fixable high fails",
        {"pm": "pnpm", "stdout": FIXABLE_HIGH, "exit_code": 1},
        1,
        ["::error::high in left-pad", "FIX AVAILABLE (>=1.2.3)", "GHSA-AAAA-BBBB-CCCC"],
        ["::warning::high in left-pad"],
    ),
    (
        "pnpm: unfixable high passes with a warning",
        {"pm": "pnpm", "stdout": UNFIXABLE_HIGH, "exit_code": 1},
        0,
        ["::warning::high in left-pad", "no fix published upstream"],
        ["::error::"],
    ),
    (
        "pnpm: fixable critical fails",
        {"pm": "pnpm", "stdout": FIXABLE_CRITICAL, "exit_code": 1},
        1,
        ["::error::critical in tar", "FIX AVAILABLE (>=2.0.0)"],
        [],
    ),
    (
        "pnpm: fixable moderate never blocks",
        {"pm": "pnpm", "stdout": FIXABLE_MODERATE, "exit_code": 1},
        0,
        ["0 fixable, 0 unfixable"],
        ["::error::", "::warning::"],
    ),
    (
        "pnpm: clean report passes",
        {"pm": "pnpm", "stdout": CLEAN_PNPM, "exit_code": 0},
        0,
        ["0 fixable, 0 unfixable, 0 suppressed"],
        ["::error::", "::warning::"],
    ),
    (
        "pnpm: mixed tree fails on the fixable one and warns on the other",
        {"pm": "pnpm", "stdout": MIXED, "exit_code": 1},
        1,
        ["::error::high in left-pad", "::warning::critical in tar", "1 fixable, 1 unfixable"],
        [],
    ),
    (
        "npm 7+: fixAvailable object fails",
        {"pm": "npm", "stdout": NPM7_FIXABLE_HIGH, "exit_code": 1},
        1,
        ["::error::high in axios", "FIX AVAILABLE (axios@1.7.4)", "GHSA-8HC4-VH64-CXMJ"],
        [],
    ),
    (
        "npm 7+: fixAvailable true (major bump) still counts as fixable",
        {"pm": "npm", "stdout": NPM7_FIXABLE_MAJOR, "exit_code": 1},
        1,
        ["::error::critical in axios", "FIX AVAILABLE (upgrade available)"],
        [],
    ),
    (
        "npm 7+: fixAvailable false passes with a warning",
        {"pm": "npm", "stdout": NPM7_UNFIXABLE_HIGH, "exit_code": 1},
        0,
        ["::warning::high in axios", "no fix published upstream"],
        ["::error::"],
    ),
    (
        "npm 7+: clean report passes",
        {"pm": "npm", "stdout": NPM7_CLEAN, "exit_code": 0},
        0,
        ["0 fixable, 0 unfixable, 0 suppressed"],
        ["::error::", "::warning::"],
    ),
    (
        "audit-ignore with a reason suppresses a fixable high",
        {
            "pm": "pnpm",
            "stdout": FIXABLE_HIGH,
            "exit_code": 1,
            "audit_ignore": "GHSA-aaaa-bbbb-cccc=no upstream release yet; review 2026-12-01",
        },
        0,
        ["suppressed by audit-ignore GHSA-AAAA-BBBB-CCCC: no upstream release yet; review 2026-12-01"],
        ["::error::"],
    ),
    (
        "audit-ignore without a reason fails the gate",
        {"pm": "pnpm", "stdout": FIXABLE_HIGH, "exit_code": 1, "audit_ignore": "GHSA-aaaa-bbbb-cccc"},
        1,
        ["has no reason"],
        [],
    ),
    (
        "audit-ignore with an empty reason fails the gate",
        {"pm": "pnpm", "stdout": FIXABLE_HIGH, "exit_code": 1, "audit_ignore": "GHSA-aaaa-bbbb-cccc=   "},
        1,
        ["has no reason"],
        [],
    ),
    (
        "a stale audit-ignore entry warns but does not block",
        {
            "pm": "pnpm",
            "stdout": CLEAN_PNPM,
            "exit_code": 0,
            "audit_ignore": "GHSA-zzzz-zzzz-zzzz=fixed months ago",
        },
        0,
        ["stale suppression", "GHSA-ZZZZ-ZZZZ-ZZZZ"],
        ["::error::"],
    ),
    (
        "audit-ignore matches a GHSA-less advisory by its NPM- id",
        {"pm": "pnpm", "stdout": NO_GHSA_HIGH, "exit_code": 1, "audit_ignore": "NPM-4242=vendored fork; tracked"},
        0,
        ["suppressed by audit-ignore NPM-4242"],
        ["::error::"],
    ),
    (
        "a GHSA-less fixable advisory still fails when not suppressed",
        {"pm": "pnpm", "stdout": NO_GHSA_HIGH, "exit_code": 1},
        1,
        ["::error::high in no-ghsa-pkg", "NPM-4242"],
        [],
    ),
    (
        "an empty report is a hard failure, not a pass",
        {"pm": "pnpm", "stdout": "", "exit_code": 0},
        1,
        ["produced no report", "the audit did not run"],
        [],
    ),
    (
        "a non-JSON report is a hard failure",
        {"pm": "pnpm", "stdout": "ERR_PNPM_AUDIT_ENDPOINT_UNAVAILABLE\n", "exit_code": 1},
        1,
        ["is not JSON"],
        [],
    ),
    (
        "an unrecognised report shape is a hard failure",
        {"pm": "pnpm", "stdout": UNKNOWN_SHAPE, "exit_code": 0},
        1,
        ["format changed"],
        [],
    ),
]


def check_shipped_flags(script: str) -> list[str]:
    """The gate must ask for JSON and must not let the tool's own exit status decide."""
    problems = []
    if "--json" not in script:
        problems.append("the audit command does not request --json")
    if "--audit-level=high" not in script:
        problems.append(
            "the audit command does not pass --audit-level=high in the `=` form "
            "(npm reads a bare `--audit-level high` as a positional)"
        )
    if "audit_status=$?" not in script:
        problems.append(
            "the gate does not capture the audit command's exit status separately -- "
            "under `set -e` a non-zero audit would abort before the report is parsed"
        )
    return problems


def main() -> None:
    script = gate_script()
    failures: list[str] = []

    for problem in check_shipped_flags(script):
        failures.append(f"shipped step text: {problem}")

    for label, kwargs, want_rc, want_in, want_not_in in CASES:
        rc, out, summary = run_case(script, **kwargs)
        ok = rc == want_rc
        missing = [s for s in want_in if s not in out]
        present = [s for s in want_not_in if s in out]
        if ok and not missing and not present:
            # An advisory-driven verdict must also reach the step summary. The
            # "the audit did not run" failures below abort before a summary
            # exists, which is correct: there is nothing to summarise.
            if "FIX AVAILABLE" in out and "Dependency audit" not in summary:
                print(f"FAIL  {label}")
                failures.append(f"{label}: blocking finding never reached GITHUB_STEP_SUMMARY")
                continue
            print(f"PASS  {label}")
            continue
        print(f"FAIL  {label}")
        detail = []
        if not ok:
            detail.append(f"exit {rc}, wanted {want_rc}")
        if missing:
            detail.append(f"missing from output: {missing}")
        if present:
            detail.append(f"unexpectedly present: {present}")
        failures.append(f"{label}: {'; '.join(detail)}\n{out.strip()[:800]}")

    if failures:
        for failure in failures:
            print(f"::error::{failure}")
        raise SystemExit(1)

    print(f"test_dependency_audit: {len(CASES)} case(s) passed")


if __name__ == "__main__":
    main()
