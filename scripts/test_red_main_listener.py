#!/usr/bin/env python3
"""Behaviour tests for `red-main-listener.yml` (narduk-reboot P3-C2 / O-D8).

Like `test_extra_env.py`, this does NOT test a copy of the script: it extracts
the "Reconcile the red-main issue" step's `run:` block out of the shipped
workflow YAML and executes that exact text under bash, against a fake `gh`
that stands in for the GitHub API with a tiny JSON-file-backed issue store.
If someone edits the step, these tests either still pass against the new
text or they fail.

Properties worth locking down:

  * A failing run with no open `red-main` issue OPENS exactly one.
  * A second consecutive failure does not open a DUPLICATE issue -- it
    comments on the one already open.
  * A green run CLOSES the open issue with a comment.
  * A green run with no open issue is a no-op (nothing to close).
  * `cancelled` / `skipped` / any non-terminal-verdict conclusion makes no
    GitHub API call at all -- a listener that could open or close an issue
    on ambiguous input would defeat the whole point of the class rule.

Run: python3 scripts/test_red_main_listener.py
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

WORKFLOW = Path(".github/workflows/red-main-listener.yml")
STEP_NAME = "Reconcile the red-main issue"
SELF_ADOPTER = Path(".github/workflows/red-main-self.yml")

GH_STUB = '''#!/usr/bin/env python3
import json
import os
import re
import sys

STATE_PATH = os.environ["GH_STUB_STATE"]
LOG_PATH = os.environ["GH_STUB_LOG"]


def load():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"issues": [], "next_number": 1}


def save(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


def main():
    argv = sys.argv[1:]
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(argv) + "\\n")

    if not argv or argv[0] != "api":
        print("{}")
        return 0

    path = argv[1]
    method = "POST"
    fields = {}
    array_fields = {}
    i = 2
    while i < len(argv):
        if argv[i] == "-X":
            method = argv[i + 1]
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

    state = load()

    if re.fullmatch(r"repos/[^/]+/[^/]+/issues", path) and method == "GET":
        label = fields.get("labels")
        want_state = fields.get("state", "open")
        result = []
        for issue in state["issues"]:
            if want_state != "all" and issue["state"] != want_state:
                continue
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
            "state": "open",
            "labels": array_fields.get("labels", []),
        }
        state["issues"].append(issue)
        save(state)
        print(json.dumps(issue))
        return 0

    m = re.fullmatch(r"repos/[^/]+/[^/]+/issues/(\\d+)", path)
    if m and method == "PATCH":
        number = int(m.group(1))
        for issue in state["issues"]:
            if issue["number"] == number and "state" in fields:
                issue["state"] = fields["state"]
        save(state)
        print(json.dumps({}))
        return 0

    if re.fullmatch(r"repos/[^/]+/[^/]+/issues/\\d+/comments", path) and method == "POST":
        print(json.dumps({}))
        return 0

    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def extract_step_script() -> str:
    doc = yaml.safe_load(WORKFLOW.read_text())
    for step in doc["jobs"]["reconcile"]["steps"]:
        if step.get("name") == STEP_NAME:
            return step["run"]
    raise SystemExit(f"::error::no step named {STEP_NAME!r} in {WORKFLOW}")


class Harness:
    """One fake `gh`, one issue-store file, one call log, reused across the
    calls of a single test so a second run sees the first run's issue."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        tmp.mkdir(parents=True, exist_ok=True)
        self.bin_dir = tmp / "bin"
        self.bin_dir.mkdir()
        gh_path = self.bin_dir / "gh"
        gh_path.write_text(GH_STUB, encoding="utf-8")
        gh_path.chmod(gh_path.stat().st_mode | stat.S_IEXEC)
        self.state_path = tmp / "state.json"
        self.log_path = tmp / "log.jsonl"
        self.log_path.touch()

    def calls(self) -> list[list[str]]:
        lines = [ln for ln in self.log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return [json.loads(ln) for ln in lines]

    def run(self, script: str, **env_extra: str) -> tuple[int, str, str]:
        github_output = self.tmp / f"github_output_{len(self.calls())}"
        github_output.touch()
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "GH_STUB_STATE": str(self.state_path),
            "GH_STUB_LOG": str(self.log_path),
            "GITHUB_OUTPUT": str(github_output),
            "GH_TOKEN": "stub-token",
            "REPO": "narduk-enterprises/example",
            "WORKFLOW_NAME": "CI",
            "RUN_ID": "12345",
            "RUN_URL": "https://github.com/narduk-enterprises/example/actions/runs/12345",
            "HEAD_SHA": "abc123",
            "LABEL": "red-main",
            **env_extra,
        }
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, cwd=self.tmp)
        return result.returncode, result.stdout + result.stderr, github_output.read_text(encoding="utf-8")


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS  " if ok else "FAIL  ") + label)
    if not ok and detail:
        print(f"      {detail}")
    return ok


def check_self_adopter_event_guard() -> tuple[bool, str]:
    """PR #142 review (low): `head_branch == default_branch` alone also
    passes for a `pull_request` run whose PR head branch happens to be named
    `main` -- `workflow_run.head_branch` is the PR's OWN branch in that case,
    not the repository's default branch's actual tip. The job `if:` must
    also require `workflow_run.event == 'push'`."""
    doc = yaml.safe_load(SELF_ADOPTER.read_text())
    condition = doc["jobs"]["red-main"].get("if") or ""
    ok = "workflow_run.event == 'push'" in condition and "head_branch" in condition
    return ok, f"red-main-self.yml job `if:` was: {condition!r}"


def main() -> int:
    script = extract_step_script()
    total = 0
    failures = 0

    total += 1
    ok, detail = check_self_adopter_event_guard()
    if not check("self-adopter if: requires event == 'push' as well as head_branch", ok, detail):
        failures += 1

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        h = Harness(tmp)

        # 1. First failure opens exactly one issue.
        rc, out, output = h.run(script, CONCLUSION="failure")
        total += 1
        opened = [c for c in h.calls() if c[:2] == ["api", "repos/narduk-enterprises/example/issues"] and "-X" not in c]
        if not check("first failure opens exactly one issue", rc == 0 and len(opened) == 1, out):
            failures += 1
        total += 1
        if not check("first failure's payload reports state=open", '"state": "open"' in output, output):
            failures += 1

        # 2. A second consecutive failure does not open a duplicate.
        rc, out, _ = h.run(script, CONCLUSION="failure")
        total += 1
        opened_again = [c for c in h.calls() if c[:2] == ["api", "repos/narduk-enterprises/example/issues"] and "-X" not in c]
        if not check("second consecutive failure opens no duplicate", rc == 0 and len(opened_again) == 1, out):
            failures += 1
        total += 1
        commented = [c for c in h.calls() if len(c) >= 2 and c[1].endswith("/comments")]
        if not check("second consecutive failure comments instead", len(commented) >= 1, out):
            failures += 1

        # 3. A green run closes the open issue.
        rc, out, output = h.run(script, CONCLUSION="success")
        total += 1
        closed = [c for c in h.calls() if len(c) >= 4 and "-X" in c and c[c.index("-X") + 1] == "PATCH"]
        if not check("green run closes the open issue", rc == 0 and len(closed) == 1, out):
            failures += 1
        total += 1
        if not check("closing payload reports state=closed", '"state": "closed"' in output, output):
            failures += 1

        # 4. A second green run, nothing open: no-op.
        rc, out, output = h.run(script, CONCLUSION="success")
        total += 1
        if not check("green run with nothing open is a no-op", rc == 0, out):
            failures += 1
        total += 1
        if not check("no-op payload is empty", "payload<<PAYLOAD_EOF\n\nPAYLOAD_EOF" in output, output):
            failures += 1

        # 5. cancelled/skipped never call the GitHub API at all.
        for conclusion in ("cancelled", "skipped", "timed_out", "action_required"):
            h2 = Harness(tmp / f"iso-{conclusion}")
            rc, out, output = h2.run(script, CONCLUSION=conclusion)
            total += 1
            if not check(f"conclusion '{conclusion}' makes zero gh calls", rc == 0 and h2.calls() == [], out):
                failures += 1

    print(f"\ntest_red_main_listener: {total} case(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
