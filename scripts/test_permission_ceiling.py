#!/usr/bin/env python3
"""R12: a callable job may not request a permission its callers do not grant.

(workflows#59) A job in a CALLED workflow can only ask for permissions the
CALLER already granted on its `uses:` job. Ask for one it did not and GitHub
kills the caller's ENTIRE run at startup — `conclusion: startup_failure`,
`jobs: []`, no logs, no check-run annotation — before a single job is created.
Nothing in the caller's repository changed, so the failure looks like an
infrastructure outage rather than an interface break, and it hits every adopter
at once the moment the moving `v1` tag advances.

That is not hypothetical: `pull-requests: read` on nuxt-cloudflare.yml's
`E2E plan` job took down every `@v1` nuxt-cloudflare adopter (harvest-tracker,
marketing-web, vtraceroute, hydrogen) on 2026-09-04, while the two repos pinned
to older commit SHAs kept running normally.

This test proves the rule is fail-capable rather than decorative: the shipped
callables pass it, and a seeded escalation — the exact one that caused
workflows#59 — is rejected.

Run: python3 scripts/test_permission_ceiling.py
"""

from __future__ import annotations

import copy
import pathlib
import sys

import yaml

# Import the linter as a module without leaving a `scripts/__pycache__/`
# behind in the checkout this repository's own CI job runs from.
sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lint_callables as lint  # noqa: E402

WORKFLOWS = pathlib.Path(".github/workflows")


def findings_for(path: pathlib.Path, doc: dict) -> list[str]:
    f = lint.Findings()
    lint.check_permission_ceiling(path, doc, f)
    return f.items


def check_shipped_callables_pass() -> None:
    failures = 0
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        on = doc.get("on", doc.get(True))
        if "workflow_call" not in str(on):
            continue
        items = findings_for(path, doc)
        if items:
            failures += 1
            print(f"FAIL  {path.name} violates R12: {items}")
    if failures:
        raise SystemExit(f"{failures} shipped callable(s) exceed their caller grant")
    print("PASS  every shipped callable stays inside its documented caller grant")


def check_e2e_plan_is_contents_only() -> None:
    """The exact regression: `E2E plan` must ask for `contents: read` alone."""
    doc = yaml.safe_load((WORKFLOWS / "nuxt-cloudflare.yml").read_text())
    perms = doc["jobs"]["e2e-plan"]["permissions"]
    assert perms == {"contents": "read"}, f"E2E plan permissions drifted: {perms}"
    print("PASS  nuxt-cloudflare.yml `E2E plan` requests contents: read and nothing else")


def check_seeded_escalation_is_rejected() -> None:
    """Re-introduce workflows#59's escalation and require the linter to catch it."""
    path = WORKFLOWS / "nuxt-cloudflare.yml"
    doc = copy.deepcopy(yaml.safe_load(path.read_text()))
    doc["jobs"]["e2e-plan"]["permissions"]["pull-requests"] = "read"
    items = findings_for(path, doc)
    if not any("pull-requests" in item and "e2e-plan" in item for item in items):
        raise SystemExit(
            "FAIL  seeded `pull-requests: read` on E2E plan was NOT rejected — "
            f"findings: {items}"
        )
    print("PASS  seeded workflows#59 escalation (`pull-requests: read`) is rejected")


def check_undeclared_callable_is_rejected() -> None:
    fake = pathlib.Path(".github/workflows/not-declared-anywhere.yml")
    doc = {"jobs": {"only": {"permissions": {"contents": "read"}}}}
    items = findings_for(fake, doc)
    if not any("CALLER_GRANTS" in item for item in items):
        raise SystemExit(f"FAIL  a callable with no declared grant was accepted: {items}")
    print("PASS  a callable with no declared caller grant is rejected")


def check_shorthand_permissions_are_rejected() -> None:
    path = WORKFLOWS / "nuxt-cloudflare.yml"
    doc = {"jobs": {"build": {"permissions": "write-all"}}}
    items = findings_for(path, doc)
    if not any("shorthand" in item for item in items):
        raise SystemExit(f"FAIL  shorthand `permissions:` was accepted: {items}")
    print("PASS  shorthand `permissions:` in a callable is rejected")


def main() -> None:
    check_shipped_callables_pass()
    check_e2e_plan_is_contents_only()
    check_seeded_escalation_is_rejected()
    check_undeclared_callable_is_rejected()
    check_shorthand_permissions_are_rejected()
    print("\ncallable permission-ceiling contract passed")


if __name__ == "__main__":
    main()
