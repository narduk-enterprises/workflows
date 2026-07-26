#!/usr/bin/env python3
"""Check hosted dependency-cache paths cannot vary with runner HOME/slot."""

from pathlib import Path

import yaml

WORKFLOWS = tuple(Path(".github/workflows").glob("*.yml"))
RESOLVE = "Resolve dependency cache directory"


def main() -> int:
    sites = 0
    failures = 0
    for path in WORKFLOWS:
        doc = yaml.safe_load(path.read_text())
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if not isinstance(step, dict) or step.get("name") != RESOLVE:
                    continue
                sites += 1
                run = step.get("run", "")
                expected = (
                    'pnpm config set store-dir "$GITHUB_WORKSPACE/.cache/pnpm-store"',
                    'echo "path=.cache/pnpm-store" >> "$GITHUB_OUTPUT"',
                    'npm config set cache "$GITHUB_WORKSPACE/.cache/npm"',
                    'echo "path=.cache/npm" >> "$GITHUB_OUTPUT"',
                )
                missing = [line for line in expected if line not in run]
                if missing:
                    print(f"FAIL {path}:{job_id}: missing stable cache setup: {missing}")
                    failures += 1
                else:
                    print(f"PASS {path}:{job_id}: stable workspace-relative cache paths")
    print(f"\ntest_cache_paths: {sites} site(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
