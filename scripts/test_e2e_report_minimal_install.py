#!/usr/bin/env python3
"""E2E report installs nothing but the pinned @playwright/test (workflows#49).

Before this change, `E2E report` ran a full `npm ci`/`pnpm install` of the
caller's whole dependency tree to execute a ~1s `playwright merge-reports`.
Measured on operator-portal run 33887424724 (cff1ff8b): setup-node 22s +
`npm ci` 23s to run a 1s merge, 57s job total.

This is a pure source contract over the shipped YAML (same discipline as
`test_playwright_toolchain.py`): assert the heavy install steps are gone,
then execute the shipped "Resolve pinned @playwright/test version" script
against fixtures to prove it fails closed on a missing or non-exact pin.

Run: python3 scripts/test_e2e_report_minimal_install.py
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile

import yaml

WORKFLOW = pathlib.Path(".github/workflows/nuxt-cloudflare.yml")

# Steps that belonged to the old full-install path and must never reappear
# in `e2e-report` — if any of these come back, the "minimal install" claim
# is false.
FORBIDDEN_STEPS = (
    "Install dependencies (pnpm)",
    "Install dependencies (npm)",
    "Install dependencies (caller script)",
    "Configure package registry auth",
    "Point pnpm at a workspace-local store (self-hosted)",
    "Remove package auth materialization",
    "Resolve dependency cache directory",
    "Restore dependency cache",
    "Save dependency cache",
)

REQUIRED_STEPS = (
    "Resolve pinned @playwright/test version",
    "Download shard evidence",
    "Merge blob reports",
    "Upload merged report",
)


def load_job() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text())
    return doc["jobs"]["e2e-report"]


def named_step(job: dict, name: str) -> dict:
    matches = [step for step in job.get("steps", []) if step.get("name") == name]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {name!r} step in e2e-report")
    return matches[0]


def check_structure() -> None:
    job = load_job()
    names = {step.get("name") for step in job["steps"] if step.get("name")}
    for forbidden in FORBIDDEN_STEPS:
        assert forbidden not in names, (
            f"e2e-report must not install the full dependency tree; "
            f"found forbidden step {forbidden!r}"
        )
    for required in REQUIRED_STEPS:
        assert required in names, f"e2e-report is missing required step {required!r}"

    merge = named_step(job, "Merge blob reports")
    run = merge["run"]
    assert "npx --yes --package" in run, (
        "Merge blob reports must install only @playwright/test via npx --package"
    )
    assert "npm ci" not in run and "pnpm install" not in run
    assert merge["env"]["PLAYWRIGHT_VERSION"] == (
        "${{ steps.playwright-version.outputs.version }}"
    )
    print("PASS  e2e-report has no full-tree install step")
    print("PASS  Merge blob reports uses npx --package pinned to the caller's version")


def resolve_script() -> str:
    job = load_job()
    return named_step(job, "Resolve pinned @playwright/test version")["run"]


def run_resolve(cwd: pathlib.Path, output_path: pathlib.Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GITHUB_OUTPUT"] = str(output_path)
    return subprocess.run(
        ["bash", "-c", resolve_script()],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


def write_package_json(root: pathlib.Path, contents: dict) -> None:
    (root / "package.json").write_text(json.dumps(contents))


def check_behavior() -> None:
    cases: list[tuple[str, dict, bool, str | None]] = [
        (
            "exact devDependency pin resolves and outputs the version",
            {"devDependencies": {"@playwright/test": "1.61.1"}},
            True,
            "1.61.1",
        ),
        (
            "exact dependency (not dev) pin also resolves",
            {"dependencies": {"@playwright/test": "1.61.1"}},
            True,
            "1.61.1",
        ),
        (
            "missing @playwright/test fails closed",
            {"devDependencies": {}},
            False,
            None,
        ),
        (
            "caret range fails closed (not exact)",
            {"devDependencies": {"@playwright/test": "^1.61.1"}},
            False,
            None,
        ),
        (
            "tilde range fails closed",
            {"devDependencies": {"@playwright/test": "~1.61.1"}},
            False,
            None,
        ),
        (
            "tag fails closed",
            {"devDependencies": {"@playwright/test": "latest"}},
            False,
            None,
        ),
    ]
    failures = 0
    for label, pkg, should_pass, expect_version in cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            write_package_json(root, pkg)
            output_path = root / "github_output"
            output_path.write_text("")
            result = run_resolve(root, output_path)
            ok = (result.returncode == 0) == should_pass
            if ok and should_pass:
                written = output_path.read_text()
                ok = written.strip() == f"version={expect_version}"
            if not ok:
                failures += 1
                print(
                    f"FAIL  {label}: rc={result.returncode} "
                    f"output={output_path.read_text()!r} stderr={result.stderr!r}"
                )
            else:
                print(f"PASS  {label}")
    if failures:
        raise SystemExit(f"{failures} case(s) failed")


def main() -> None:
    check_structure()
    check_behavior()
    print("\ne2e-report minimal-install contract passed")


if __name__ == "__main__":
    main()
