#!/usr/bin/env python3
"""Shape and behaviour tests for `design-ledger.yml` (agent-infrastructure#1804).

Like `test_red_main_listener.py`, the behaviour half does NOT test a copy of the
step: it extracts the shipped `run:` blocks out of the workflow YAML and runs
that exact text under bash, in a throwaway product repository whose ledger maps
a two-screen canvas to two pages, with `scripts/dc_ledger.py` laid out where
the second checkout puts it.

Properties worth locking down:

  * The callable is advisory: no `Required` job, no secrets, and `check` runs
    with `contents: read` only.
  * The checker comes from THIS repository at the pinned commit
    (`job.workflow_sha`), sparse, without credentials, never from the caller.
  * `check` goes red while an entry is flagged, with a fix line per entry and
    the table appended to `$GITHUB_STEP_SUMMARY`, and green once it agrees.
  * An unknown `mode` fails loudly instead of skipping both jobs green.
  * `flag` opens the ledger's one issue through `gh` with the caller's token.
  * `scripts/dc_ledger.py` runs alone: stdlib only, nothing beside it.

Run: python3 scripts/test_design_ledger.py
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "design-ledger.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
CHECKER = ROOT / "scripts" / "dc_ledger.py"
TOOL_DIR = ".design-ledger-workflows"

BOARD = ('<div class="dc-app"><sc-if value="{{is.home}}" hint-placeholder-val="{{true}}"><div class="dc-app-body">'
         '<h1>Home</h1></div></sc-if><sc-if value="{{is.hosts}}" hint-placeholder-val="{{false}}">'
         '<div class="dc-app-body"><h1>Hosts</h1></div></sc-if></div>')

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

GH_STUB = '''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["GH_STUB_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "token": os.environ.get("GH_TOKEN", "")}) + "\\n")
if argv[:2] == ["issue", "list"]:
    print("[]")
elif argv[:2] == ["issue", "create"]:
    print("https://github.com/example/seed/issues/7")
'''

failures = 0
total = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures, total
    total += 1
    print(("PASS  " if ok else "FAIL  ") + label)
    if not ok:
        failures += 1
        if detail:
            print(f"      {detail}")


def load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def step(job: str, name: str) -> dict:
    for s in load()["jobs"][job]["steps"]:
        if s.get("name") == name:
            return s
    raise SystemExit(f"::error::no step named {name!r} in job {job!r} of {WORKFLOW}")


def ci_checkout_pin() -> str:
    for line in CI.read_text().splitlines():
        if "uses: actions/checkout@" in line:
            return line.split("uses:", 1)[1].strip()
    raise SystemExit("::error::ci.yml has no actions/checkout pin to compare against")


def check_shape() -> None:
    doc = load()
    on = doc[True]  # PyYAML parses the bare key `on:` as True
    check("callable is on: workflow_call only", list(on) == ["workflow_call"], str(list(on)))
    call = on["workflow_call"]
    inputs = call.get("inputs") or {}
    check("declares no secrets", "secrets" not in call, str(call.get("secrets")))
    check("ledger input is a required string",
          inputs.get("ledger", {}).get("required") is True and inputs["ledger"].get("type") == "string")
    check("mode input defaults to check",
          inputs.get("mode", {}).get("default") == "check" and inputs["mode"].get("required") is False)
    check("runs-on input is optional and empty by default (visibility-routed)",
          inputs.get("runs-on", {}).get("default") == "" and inputs["runs-on"].get("required") is False)
    check("top-level permissions are contents: read", doc.get("permissions") == {"contents": "read"})

    jobs = doc["jobs"]
    check("jobs are exactly check and flag", sorted(jobs) == ["check", "flag"], str(sorted(jobs)))
    check("no job is named Required (the check is advisory)",
          all((j.get("name") or jid) != "Required" for jid, j in jobs.items()))
    check("check job runs with contents: read only", jobs["check"]["permissions"] == {"contents": "read"})
    check("flag job adds issues: write and nothing else",
          jobs["flag"]["permissions"] == {"contents": "read", "issues": "write"})
    check("check job runs unless mode is flag", jobs["check"]["if"].strip() == "inputs.mode != 'flag'")
    check("flag job runs only for mode flag", jobs["flag"]["if"].strip() == "inputs.mode == 'flag'")

    pin = ci_checkout_pin()
    for jid in ("check", "flag"):
        steps = jobs[jid]["steps"]
        caller, tool = steps[0], steps[1]
        check(f"{jid}: caller checkout uses ci.yml's actions/checkout pin",
              caller["uses"] == pin.split(" #")[0], f"{caller['uses']} vs {pin}")
        check(f"{jid}: caller checkout does not persist credentials",
              caller.get("with", {}).get("persist-credentials") is False)
        w = tool.get("with", {})
        check(f"{jid}: checker checkout is this repo at job.workflow_sha",
              tool["uses"] == caller["uses"] and w.get("repository") == "narduk-enterprises/workflows"
              and w.get("ref") == "${{ job.workflow_sha }}", str(w))
        check(f"{jid}: checker checkout is sparse to dc_ledger.py, into {TOOL_DIR}, without credentials",
              w.get("sparse-checkout") == "scripts/dc_ledger.py" and w.get("sparse-checkout-cone-mode") is False
              and w.get("path") == TOOL_DIR and w.get("persist-credentials") is False, str(w))
        runs = "\n".join(s.get("run", "") for s in steps)
        check(f"{jid}: runs the checker from the pinned checkout, never the ledger's build",
              f"python3 {TOOL_DIR}/scripts/dc_ledger.py" in runs and "--build" not in runs, runs)
    flag_env = step("flag", "Keep the design drift issue").get("env", {})
    check("flag passes the caller's own github.token", flag_env.get("GH_TOKEN") == "${{ github.token }}", str(flag_env))
    check("check step gets no token", "GH_TOKEN" not in step("check", "Check the design ledger").get("env", {}))


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env={**os.environ, **GIT_ENV})


class Product:
    """A caller's checkout: a ledger, its canvas, its pages, and the checker
    where the workflow's second checkout puts it."""

    def __init__(self, tmp: Path):
        self.root = tmp / "caller"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        (self.root / "pages").mkdir()
        (self.root / "pages/index.vue").write_text("<template>home</template>\n")
        (self.root / "pages/hosts.vue").write_text("<template>hosts</template>\n")
        design = self.root / "design/canvas"
        (design / "out/project").mkdir(parents=True)
        (design / "out/project/App.dc.html").write_text(BOARD)
        self.ledger = "design/canvas/ledger.json"
        (self.root / self.ledger).write_text(json.dumps({
            "canvas": "Seed", "project": "out/project", "gate": {"state": "cleared"},
            "issue": {"repo": "example/seed", "labels": ["enhancement"]},
            "entries": [{"id": "home", "board": "App.dc.html", "screen": "home",
                         "code": ["pages/index.vue"], "built": None},
                        {"id": "hosts", "board": "App.dc.html", "screen": "hosts",
                         "code": ["pages/hosts.vue"], "built": None}]}))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "seed")
        # The second checkout lands inside the caller's workspace, untracked.
        (self.root / TOOL_DIR / "scripts").mkdir(parents=True)
        shutil.copy(CHECKER, self.root / TOOL_DIR / "scripts" / "dc_ledger.py")
        self.bin = tmp / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        self.gh_log = tmp / "gh.jsonl"
        self.gh_log.touch()
        self.summary = tmp / "summary.md"
        self.summary.write_text("")

    def run(self, script: str, **env: str) -> tuple[int, str]:
        full = {**os.environ, **GIT_ENV, "PATH": f"{self.bin}:{os.environ['PATH']}",
                "GH_STUB_LOG": str(self.gh_log), "GITHUB_STEP_SUMMARY": str(self.summary),
                "LEDGER": self.ledger, **env}
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full, cwd=self.root)
        return r.returncode, r.stdout + r.stderr

    def mark_built(self) -> None:
        subprocess.run([sys.executable, str(CHECKER), "mark-built", str(self.root / self.ledger), "--all"],
                       check=True, capture_output=True)
        git(self.root, "commit", "-q", "-am", "built")

    def gh_calls(self) -> list[dict]:
        return [json.loads(ln) for ln in self.gh_log.read_text().splitlines() if ln.strip()]


def check_behaviour() -> None:
    check_run = step("check", "Check the design ledger")["run"]
    flag_run = step("flag", "Keep the design drift issue")["run"]
    with tempfile.TemporaryDirectory() as t:
        p = Product(Path(t))
        rc, out = p.run(check_run, MODE="check")
        check("check: unbuilt screens under a cleared gate exit 1", rc == 1, out)
        check("check: one fix line per flagged entry",
              "fix: home: not-built: build it, then `mark-built home`" in out
              and "fix: hosts: not-built" in out, out)
        summary = p.summary.read_text()
        check("check: the table is appended to $GITHUB_STEP_SUMMARY",
              "### Design ledger: Seed" in summary and "| `home` | not-built | yes | never |" in summary, summary)
        check("check: never calls gh", p.gh_calls() == [], str(p.gh_calls()))

        p.mark_built()
        rc, out = p.run(check_run, MODE="check")
        check("check: an agreeing ledger exits 0", rc == 0 and "fix:" not in out, out)

        (p.root / "pages/hosts.vue").write_text("<template>hosts, sorted</template>\n")
        git(p.root, "commit", "-q", "-am", "code moved")
        rc, out = p.run(check_run, MODE="check")
        check("check: a code edit under a built screen reads design-stale and exits 1",
              rc == 1 and "fix: hosts: design-stale: amend the canvas" in out, out)

        rc, out = p.run(check_run, MODE="flagg")
        check("check: an unknown mode fails loudly", rc == 1 and "mode must be 'check' or 'flag'" in out, out)

        rc, out = p.run(flag_run, GH_TOKEN="caller-token")
        calls = p.gh_calls()
        created = [c for c in calls if c["argv"][:2] == ["issue", "create"]]
        check("flag: opens the ledger's one issue on the ledger's repo", rc == 0 and len(created) == 1
              and "example/seed" in created[0]["argv"] and "Design drift: Seed" in created[0]["argv"], out + str(calls))
        check("flag: every gh call carries the caller's token", calls and all(c["token"] == "caller-token" for c in calls))
        check("flag: prints the action", "opened #7" in out, out)


def check_standalone() -> None:
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(CHECKER, d)
        code = ("import sys; sys.path.insert(0, %r); import dc_ledger; "
                "assert 'dc_canvas' not in sys.modules; print(dc_ledger.STATUSES)" % d)
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd=d)
    check("dc_ledger.py imports alone, with nothing beside it", r.returncode == 0, r.stderr)
    text = CHECKER.read_text()
    check("dc_ledger.py does not import dc_canvas", "import dc_canvas" not in text)


def main() -> int:
    check_shape()
    check_behaviour()
    check_standalone()
    print(f"\ntest_design_ledger: {total} case(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
