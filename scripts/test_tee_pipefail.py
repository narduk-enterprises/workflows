#!/usr/bin/env python3
"""Negative + positive tests for lint_callables R10 (| tee without pipefail).

R10 exists so a future `cmd | tee log` cannot reintroduce exit-code masking.
These cases are synthetic YAML snippets fed through the same checker the CI
gate runs — they do not modify any workflow on disk.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Allow `python3 scripts/test_tee_pipefail.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lint_callables import Findings, check_tee_pipefail  # noqa: E402
import yaml  # noqa: E402


def _check(snippet: str) -> list[str]:
    doc = yaml.safe_load(snippet)
    f = Findings()
    # Write to a real path so Findings messages look like production.
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
        fh.write(snippet)
        path = Path(fh.name)
    try:
        check_tee_pipefail(path, doc, f)
    finally:
        path.unlink(missing_ok=True)
    return f.items


CASES: list[tuple[str, str, bool]] = [
    (
        "cmd | tee without pipefail is a finding",
        """
jobs:
  build:
    steps:
      - name: Build
        run: |
          npm test | tee test.log
""",
        True,
    ),
    (
        "cmd | tee with set -euo pipefail is clean",
        """
jobs:
  build:
    steps:
      - name: Build
        run: |
          set -euo pipefail
          npm test | tee test.log
""",
        False,
    ),
    (
        "cmd | tee with set -o pipefail alone is clean",
        """
jobs:
  build:
    steps:
      - name: Build
        run: |
          set -o pipefail
          npm test | tee test.log
""",
        False,
    ),
    (
        "no tee at all is clean",
        """
jobs:
  build:
    steps:
      - name: Build
        run: |
          set -euo pipefail
          npm test
""",
        False,
    ),
    (
        "comment that merely mentions tee is clean",
        """
jobs:
  build:
    steps:
      - name: Build
        run: |
          set -euo pipefail
          # do not use cmd | tee without pipefail
          npm test
""",
        False,
    ),
    (
        "word 'guaranteed' is not a false positive",
        """
jobs:
  build:
    steps:
      - name: Build
        run: |
          # not guaranteed on the self-hosted LXC images
          echo ok
""",
        False,
    ),
]


def main() -> int:
    failed = 0
    for name, snippet, expect_finding in CASES:
        items = _check(snippet)
        has = any("R10" in i for i in items)
        ok = has is expect_finding
        status = "PASS" if ok else "FAIL"
        print(f"  {status}  {name}")
        if not ok:
            failed += 1
            print(f"         expected finding={expect_finding}, got {items!r}")
    print(f"\ntest_tee_pipefail: {len(CASES)} case(s), {failed} failure(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
