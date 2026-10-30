#!/usr/bin/env python3
"""Freeze test: nuxt-cloudflare.yml takes no new workflow_call inputs.

taskgraph.yml is the CI path going forward (taskgraph-ci-2026-10, p5-retire):
fast-path work (fast-scripts, fast-parallel, pr-fast-only, eslint-cache,
required-reuse-pr-results, e2e-pr-only-changed, journey-smoke-*, and every
other knob) belongs there. This callable's input surface is frozen at the set
below. Removing an input is fine (delete it here too); adding one fails.

Run: python3 scripts/test_nuxt_input_freeze.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/nuxt-cloudflare.yml")

FROZEN_INPUTS = frozenset(
    {
        "artifact-scope",
        "audit-ignore",
        "build-script",
        "checks-in-build",
        "ci-concurrency-key",
        "concurrent-scripts",
        "dependency-audit",
        "e2e-args",
        "e2e-browsers",
        "e2e-browsers-path",
        "e2e-build-artifact-path",
        "e2e-full-paths",
        "e2e-install-browsers",
        "e2e-pr-args",
        "e2e-pr-only-changed",
        "e2e-pr-shards",
        "e2e-quarantine-args",
        "e2e-reuse-pr-results",
        "e2e-runner",
        "e2e-script",
        "e2e-shards",
        "e2e-skip-paths",
        "eslint-cache",
        "expected-candidate-sha",
        "extra-gate-scripts",
        "extra-scripts",
        "fast-parallel",
        "fast-scripts",
        "foundation-check",
        "foundation-check-auth",
        "foundation-check-tool-version",
        "install-script",
        "journey-smoke-args",
        "journey-smoke-rollback-script",
        "journey-smoke-rollback-to",
        "journey-smoke-url",
        "lightweight-runner",
        "mode",
        "node-version",
        "node-version-file",
        "package-manager",
        "performance-budget-args",
        "pr-fast-only",
        "preview-checks",
        "preview-timeout-minutes",
        "preview-url-source",
        "preview-url-template",
        "preview-working-directory",
        "production-output-directory",
        "quality-level",
        "quality-opt-out",
        "require-scripts",
        "required-reuse-pr-results",
        "run-e2e",
        "run-tests",
        "runner",
        "security-headers-paths",
        "security-headers-url",
        "test-script",
        "typecheck-web-script",
        "typecheck-worker-script",
        "working-directory",
        "wrangler-dry-run",
        "wrangler-env",
        "wrangler-working-directory",
    }
)


def main() -> int:
    doc = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML parses the bare key `on` as boolean True.
    live = set(doc.get("on", doc.get(True))["workflow_call"]["inputs"])
    added = sorted(live - FROZEN_INPUTS)
    stale = sorted(FROZEN_INPUTS - live)
    if added:
        print(f"FAIL  new nuxt-cloudflare input(s) {added}: the callable is frozen; use taskgraph.yml")
    if stale:
        print(f"FAIL  removed input(s) {stale}: drop them from FROZEN_INPUTS in this file")
    print(f"\ntest_nuxt_input_freeze: {len(live)} input(s), {len(added)} added, {len(stale)} stale")
    return 1 if added or stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
