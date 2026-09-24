#!/usr/bin/env python3
"""Behaviour tests for `flake-digest.yml` (narduk-reboot P3-C2).

Same discipline as `test_red_main_listener.py`: the "Build and file the
weekly flake digest" step's `run:` block is extracted from the shipped
workflow YAML and executed against a fake `gh` that answers `run list`,
`run view` and the issue-filing `api` calls from a fixture, so the test
cannot drift from the callable it describes.

Properties worth locking down:

  * Only jobs whose name matches `quarantine-job-pattern` are counted; an
    ordinary gating job in the same run is ignored.
  * A digest issue is filed once per ISO week and carries the scanned/failed
    counts; a second run in the same week UPDATES that issue rather than
    filing a second one.
  * A week with zero quarantine failures still files a digest (not a no-op
    the way red-main's success path is) so a nightly regression cannot hide
    behind "nothing happened".

Run: python3 scripts/test_flake_digest.py
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/flake-digest.yml")
STEP_NAME = "Build and file the weekly flake digest"

GH_STUB = '''#!/usr/bin/env python3
import json
import os
import re
import sys

RUNS_PATH = os.environ["GH_STUB_RUNS"]
STATE_PATH = os.environ["GH_STUB_STATE"]
LOG_PATH = os.environ["GH_STUB_LOG"]


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"issues": [], "next_number": 1}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def main():
    argv = sys.argv[1:]
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(argv) + "\\n")

    with open(RUNS_PATH, encoding="utf-8") as f:
        fixture = json.load(f)

    if argv[:1] == ["run"] and argv[1:2] == ["list"]:
        print(json.dumps(fixture["runs"]))
        return 0

    if argv[:1] == ["run"] and argv[1:2] == ["view"]:
        run_id = int(argv[2])
        jobs = fixture["jobs_by_run"].get(str(run_id), [])
        print(json.dumps({"jobs": jobs}))
        return 0

    if argv[:1] != ["api"]:
        print("{}")
        return 0

    path = argv[1]
    method = flag(argv, "-X") or "POST"
    fields = {}
    array_fields = {}
    i = 2
    while i < len(argv):
        if argv[i] == "-X":
            i += 2
            continue
        if argv[i] == "-f":
            key, _, value = argv[i + 1].partition("=")
            if key.endswith("[]"):
                array_fields.setdefault(key[:-2], []).append(value)
            else:
                fields[key] = value
            i += 2
            continue
        i += 1

    state = load_state()

    if re.fullmatch(r"repos/[^/]+/[^/]+/issues", path) and method == "GET":
        label = fields.get("labels")
        result = []
        for issue in state["issues"]:
            if label and label not in issue.get("labels", []):
                continue
            result.append(issue)
        print(json.dumps(result))
        return 0

    if re.fullmatch(r"repos/[^/]+/[^/]+/issues", path) and method == "POST":
        number = state["next_number"]
        state["next_number"] += 1
        issue = {
            "number": number,
            "title": fields.get("title", ""),
            "body": fields.get("body", ""),
            "labels": array_fields.get("labels", []),
        }
        state["issues"].append(issue)
        save_state(state)
        print(json.dumps(issue))
        return 0

    m = re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\\d+)", path)
    if m and method == "PATCH":
        number = int(m.group(1))
        for issue in state["issues"]:
            if issue["number"] == number and "body" in fields:
                issue["body"] = fields["body"]
        save_state(state)
        print(json.dumps({}))
        return 0

    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def extract_step_script() -> str:
    doc = yaml.safe_load(WORKFLOW.read_text())
    for step in doc["jobs"]["digest"]["steps"]:
        if step.get("name") == STEP_NAME:
            return step["run"]
    raise SystemExit(f"::error::no step named {STEP_NAME!r} in {WORKFLOW}")


class Harness:
    def __init__(self, tmp: Path, fixture: dict):
        self.tmp = tmp
        tmp.mkdir(parents=True, exist_ok=True)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        gh_path = bin_dir / "gh"
        gh_path.write_text(GH_STUB, encoding="utf-8")
        gh_path.chmod(gh_path.stat().st_mode | stat.S_IEXEC)
        self.bin_dir = bin_dir
        self.runs_path = tmp / "runs.json"
        self.runs_path.write_text(json.dumps(fixture), encoding="utf-8")
        self.state_path = tmp / "state.json"
        self.log_path = tmp / "log.jsonl"
        self.log_path.touch()

    def calls(self) -> list[list[str]]:
        lines = [ln for ln in self.log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return [json.loads(ln) for ln in lines]

    def run(self, script: str, **env_extra: str) -> tuple[int, str]:
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "GH_STUB_RUNS": str(self.runs_path),
            "GH_STUB_STATE": str(self.state_path),
            "GH_STUB_LOG": str(self.log_path),
            "GH_TOKEN": "stub-token",
            "REPO": "narduk-enterprises/example",
            "WORKFLOW_NAME": "CI",
            "QUARANTINE_PATTERN": "quarantine",
            "DAYS": "7",
            "LABEL": "flake-digest",
            "MAX_FAILURES_LISTED": "20",
            **env_extra,
        }
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, cwd=self.tmp)
        return result.returncode, result.stdout + result.stderr


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS  " if ok else "FAIL  ") + label)
    if not ok and detail:
        print(f"      {detail}")
    return ok


FIXTURE_MIXED = {
    "runs": [
        {"databaseId": 1, "conclusion": "success", "headBranch": "main", "createdAt": "2026-09-20T00:00:00Z", "url": "https://example/runs/1"},
        {"databaseId": 2, "conclusion": "failure", "headBranch": "main", "createdAt": "2026-09-21T00:00:00Z", "url": "https://example/runs/2"},
    ],
    "jobs_by_run": {
        "1": [
            {"name": "build", "conclusion": "success"},
            {"name": "e2e-quarantine", "conclusion": "success"},
        ],
        "2": [
            {"name": "build", "conclusion": "success"},
            {"name": "e2e-quarantine", "conclusion": "failure"},
        ],
    },
}

FIXTURE_CLEAN = {
    "runs": [
        {"databaseId": 3, "conclusion": "success", "headBranch": "main", "createdAt": "2026-09-22T00:00:00Z", "url": "https://example/runs/3"},
    ],
    "jobs_by_run": {
        "3": [
            {"name": "build", "conclusion": "success"},
            {"name": "e2e-quarantine", "conclusion": "success"},
        ],
    },
}


def main() -> int:
    script = extract_step_script()
    total = 0
    failures = 0

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)

        # 1. Mixed fixture: only the quarantine job counts, one of two failed.
        h = Harness(tmp / "mixed", FIXTURE_MIXED)
        rc, out = h.run(script)
        total += 1
        if not check("mixed fixture exits clean", rc == 0, out):
            failures += 1
        created = [c for c in h.calls() if c[:2] == ["api", "repos/narduk-enterprises/example/issues"] and "-X" not in c]
        total += 1
        if not check("mixed fixture files exactly one digest issue", len(created) == 1, out):
            failures += 1
        # The counts land in the issue body (the stub never echoes the -f
        # values back on stdout), so read them back off the logged call.
        body_arg = None
        for call in h.calls():
            if call[:2] == ["api", "repos/narduk-enterprises/example/issues"] and "-X" not in call:
                for j, tok in enumerate(call):
                    if tok == "-f" and call[j + 1].startswith("body="):
                        body_arg = call[j + 1][len("body="):]
        total += 1
        if not check("digest body reports scanned=2, failed=1", bool(body_arg) and '"scanned": 2' in body_arg and '"failed": 1' in body_arg, body_arg or ""):
            failures += 1
        total += 1
        if not check("digest body lists the failing run", bool(body_arg) and "https://example/runs/2" in body_arg, body_arg or ""):
            failures += 1

        # 2. Re-running in the same week UPDATES, does not duplicate.
        rc, out = h.run(script)
        total += 1
        if not check("second same-week run exits clean", rc == 0, out):
            failures += 1
        created_again = [c for c in h.calls() if c[:2] == ["api", "repos/narduk-enterprises/example/issues"] and "-X" not in c]
        total += 1
        if not check("second same-week run does not file a duplicate", len(created_again) == 1, out):
            failures += 1
        updated = [c for c in h.calls() if len(c) >= 2 and c[1].startswith("repos/narduk-enterprises/example/issues/") and "-X" in c and c[c.index("-X") + 1] == "PATCH"]
        total += 1
        if not check("second same-week run PATCHes the existing issue", len(updated) == 1, out):
            failures += 1

        # 3. A clean week still files a digest (zero is not a no-op).
        h2 = Harness(tmp / "clean", FIXTURE_CLEAN)
        rc, out = h2.run(script)
        total += 1
        if not check("clean-week fixture exits clean", rc == 0, out):
            failures += 1
        created_clean = [c for c in h2.calls() if c[:2] == ["api", "repos/narduk-enterprises/example/issues"] and "-X" not in c]
        total += 1
        if not check("a zero-failure week still files a digest", len(created_clean) == 1, out):
            failures += 1

    print(f"\ntest_flake_digest: {total} case(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
