#!/usr/bin/env python3
"""Tests for the composite actions under `actions/` (v3).

Nothing here tests a copy: each test extracts the shipped `run:` text of a step out of its `action.yml`, or runs the
shipped `verify.py`, and executes it. `df` and `curl` targets are chosen by the test (a shim, a local HTTP server), so
it runs the same on a developer Mac and on the Linux runner.

What is locked down:
  shape          every action is composite, every `run:` step names bash and passes shellcheck (actionlint does not
                 read a composite action, so nothing else would), every `uses:` is a full SHA with a version comment,
                 and no `run:` text interpolates `${{ }}` (an input reaches a script through `env:`).
  setup          the three places a pnpm store can live: in the workspace with the cache (workspace on `/`), beside
                 the workspace without a cache (a persistent runner), and nowhere (no writable store: degrade, never fail).
  version-proof  green only when the URL answers exactly the expected text; red, after every attempt, when it does not.
  health         green on a 200; red, with the body's head, on anything else or no answer.
  verify         the selection from a gate file (rules, `always`, the `full` swap, a deleted file), the whole list,
                 side-by-side runs that keep going past a red check, `alone` order, and a refused unknown `alone` name.

Run: python3 scripts/test_actions.py
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ROOT / "actions"
USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)\s*(#.*)?$")
FAILED: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        FAILED.append(what)


def load(name: str) -> dict:
    return yaml.safe_load((ACTIONS / name / "action.yml").read_text())


def step_script(name: str, step_name: str) -> str:
    for step in load(name)["runs"]["steps"]:
        if step.get("name") == step_name:
            return step["run"]
    raise SystemExit(f"::error::actions/{name}/action.yml has no step named {step_name!r}")


def bash(script: str, env: dict, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], env={**os.environ, **env}, cwd=cwd, capture_output=True, text=True, timeout=120)


# -- shape ------------------------------------------------------------------------------------------------------------

def shellcheck(script: str) -> bool:
    """True when shellcheck passes the script. Without the binary: a failure in CI, a skip on a desk."""
    if not shutil.which("shellcheck"):
        if os.environ.get("CI"):
            return False
        print("     (shellcheck is not installed: skipped)")
        return True
    with tempfile.NamedTemporaryFile("w", suffix=".sh") as fh:
        fh.write("#!/usr/bin/env bash\n" + script)
        fh.flush()
        done = subprocess.run(["shellcheck", "-s", "bash", fh.name], capture_output=True, text=True)
    if done.returncode != 0:
        print(done.stdout)
    return done.returncode == 0


def test_shape() -> None:
    names = sorted(p.parent.name for p in ACTIONS.glob("*/action.yml"))
    check(names == ["health", "setup", "verify", "version-proof"], f"the actions are health, setup, verify, version-proof (found {names})")
    for name in names:
        doc = load(name)
        text = (ACTIONS / name / "action.yml").read_text()
        check(doc["runs"]["using"] == "composite", f"{name}: composite")
        check(bool(doc.get("name")) and bool(doc.get("description")), f"{name}: has a name and a description")
        for step in doc["runs"]["steps"]:
            if "run" in step:
                check(step.get("shell") == "bash", f"{name}: step {step.get('name')!r} names bash")
                check("${{" not in step["run"], f"{name}: step {step.get('name')!r} reads inputs through env, not in the script")
                check(shellcheck(step["run"]), f"{name}: step {step.get('name')!r} passes shellcheck")
        for line in text.splitlines():
            m = USES.match(line)
            if not m:
                continue
            ref = m.group(1).split("@")[-1]
            check(re.fullmatch(r"[0-9a-f]{40}", ref) is not None and re.search(r"#\s*v\d", m.group(2) or "") is not None,
                  f"{name}: {m.group(1).split('@')[0]} is pinned to a full SHA with its version")
        for key, spec in (doc.get("inputs") or {}).items():
            check(bool(spec.get("description")), f"{name}: input {key} is described")


# -- setup ------------------------------------------------------------------------------------------------------------

def run_store(tmp: Path, mount: str) -> tuple[subprocess.CompletedProcess, str, str]:
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    df = bindir / "df"
    df.write_text(f"#!/bin/sh\necho 'Mounted on'\necho '{mount}'\n")
    df.chmod(df.stat().st_mode | stat.S_IEXEC)
    out, env = tmp / "out", tmp / "env"
    out.write_text("")
    env.write_text("")
    workspace = tmp / "volume" / "work"
    workspace.mkdir(parents=True, exist_ok=True)
    done = bash(step_script("setup", "Where the pnpm store lives"), {
        "PATH": f"{bindir}:{os.environ['PATH']}", "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_OUTPUT": str(out), "GITHUB_ENV": str(env),
    })
    return done, out.read_text(), env.read_text()


def test_setup() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        done, out, env = run_store(tmp, "/")
        check(done.returncode == 0 and out.strip() == "cache=true" and env.strip() == f"npm_config_store_dir={tmp}/volume/work/.pnpm-store",
              "setup: a workspace on / gets a store inside the workspace, with the cache")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "volume" / "pnpm-store").mkdir(parents=True)
        done, out, env = run_store(tmp, str(tmp / "volume"))
        check(done.returncode == 0 and out.strip() == "cache=false" and env.strip() == f"npm_config_store_dir={tmp}/volume/pnpm-store",
              "setup: a runner with a store beside the workspace uses it, without the cache")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        done, out, env = run_store(tmp, str(tmp / "volume"))
        check(done.returncode == 0 and out.strip() == "cache=false" and env.strip() == "",
              "setup: no writable store beside the workspace degrades to pnpm's default, and does not fail")
    doc = load("setup")
    nodes = [s for s in doc["runs"]["steps"] if str(s.get("uses", "")).startswith("actions/setup-node@")]
    check(len(nodes) == 2 and nodes[0]["if"] == "steps.store.outputs.cache != 'true'" and nodes[1]["if"] == "steps.store.outputs.cache == 'true'"
          and nodes[0]["with"].get("package-manager-cache") is False and nodes[1]["with"].get("cache") == "pnpm",
          "setup: setup-node restores a cache only where the store is in the workspace")
    install = step_script("setup", "Install dependencies")
    check("pnpm install --frozen-lockfile $INSTALL_ARGS" in install, "setup: the install is frozen and takes the caller's flags")


# -- version-proof and health -----------------------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    hits: dict = {}

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        Handler.hits[path] = Handler.hits.get(path, 0) + 1
        answers = Handler.routes.get(path, [(404, "not here")])
        code, body = answers[min(Handler.hits[path], len(answers)) - 1]
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


def serve() -> tuple[http.server.ThreadingHTTPServer, str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_probes() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    Handler.routes = {
        "/version": [(200, sha)],
        "/late": [(200, "old"), (200, "old"), (200, sha)],
        "/stale": [(200, "old")],
        "/healthz": [(200, "ok")],
        "/warming": [(503, "warming"), (200, "ok")],
        "/broken": [(503, "the control database is not migrated")],
    }
    Handler.hits = {}
    server, base = serve()
    try:
        proof = step_script("version-proof", "Ask the live host which commit it serves")
        env = {"PROOF_EXPECTED": sha, "PROOF_ATTEMPTS": "3", "PROOF_INTERVAL": "0"}
        done = bash(proof, {**env, "PROOF_URL": f"{base}/version"})
        check(done.returncode == 0 and Handler.hits["/version"] == 1, "version-proof: green at the first answer that is the commit")
        done = bash(proof, {**env, "PROOF_URL": f"{base}/late"})
        check(done.returncode == 0 and Handler.hits["/late"] == 3, "version-proof: keeps asking while the old commit answers")
        done = bash(proof, {**env, "PROOF_URL": f"{base}/stale"})
        check(done.returncode == 1 and Handler.hits["/stale"] == 3 and "::error::" in done.stdout and "'old'" in done.stdout,
              "version-proof: red after every attempt, naming what answered")
        done = bash(proof, {**env, "PROOF_URL": "http://127.0.0.1:9/version", "PROOF_ATTEMPTS": "2"})
        check(done.returncode == 1 and "<unreachable>" in done.stdout, "version-proof: an unreachable host is red, not an error in the script")

        health = step_script("health", "Probe the health URL")
        env = {"HEALTH_ATTEMPTS": "3", "HEALTH_INTERVAL": "0", "HEALTH_MEANS": "the binding is wrong"}
        done = bash(health, {**env, "HEALTH_URL": f"{base}/healthz?deep=1"})
        check(done.returncode == 0 and Handler.hits["/healthz"] == 1, "health: green on a 200")
        done = bash(health, {**env, "HEALTH_URL": f"{base}/warming"})
        check(done.returncode == 0 and Handler.hits["/warming"] == 2, "health: waits through a 503")
        done = bash(health, {**env, "HEALTH_URL": f"{base}/broken"})
        check(done.returncode == 1 and Handler.hits["/broken"] == 3 and "the binding is wrong" in done.stdout
              and "the control database is not migrated" in done.stdout, "health: red after every attempt, with the reason and the body")
        done = bash(health, {**env, "HEALTH_URL": "http://127.0.0.1:9/healthz", "HEALTH_ATTEMPTS": "1"})
        check(done.returncode == 1 and "-> 000" in done.stdout, "health: no answer is 000 and red")
    finally:
        server.shutdown()


# -- verify -----------------------------------------------------------------------------------------------------------

GATE = {
    "schemaVersion": 1,
    "checks": {
        "lint": "echo lint-all",
        "lint-files": "echo lint {files}",
        "types": "echo types",
        "test": "echo test",
        "slow": "sleep 0.4 && echo slow",
        "red": "echo broken && exit 3",
        "build": "echo build",
    },
    "default": ["lint", "types", "test"],
    "changed": {
        "rules": [
            {"paths": ["**/*.ts"], "checks": ["lint-files"]},
            {"paths": ["eslint.config.*", "package.json"], "checks": ["lint"]},
            {"paths": ["src/**", "package.json"], "checks": ["types"]},
            {"paths": ["package.json"], "checks": ["build"]},
        ],
        "always": ["test"],
        "full": {"paths": ["eslint.config.*", "package.json"], "swap": {"lint-files": "lint"}},
    },
}
VERIFY = ACTIONS / "verify" / "verify.py"


def verify(repo: Path, *args: str, stdin: str = "", alone: str = "", gate: dict | None = None) -> subprocess.CompletedProcess:
    (repo / "Config").mkdir(exist_ok=True)
    (repo / "Config" / "remote-gate.json").write_text(json.dumps(gate or GATE))
    env = {**os.environ, "GITHUB_WORKSPACE": str(repo), "RUNNER_TEMP": str(repo), "VERIFY_ALONE": alone}
    env.pop("GITHUB_STEP_SUMMARY", None)
    return subprocess.run([sys.executable, str(VERIFY), *args], env=env, input=stdin, capture_output=True, text=True, timeout=120)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True)


def test_verify() -> None:
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        (repo / "src").mkdir()
        (repo / "src" / "a.ts").write_text("a")
        (repo / "README.md").write_text("r")

        def select(*files: str) -> list[str]:
            return verify(repo, "select", stdin="\n".join(files)).stdout.split()

        check(select("README.md") == ["test"], "verify: a file no rule names still runs what `always` lists")
        check(select("src/a.ts") == ["lint-files", "types", "test"], "verify: a source file picks its lint, its typecheck and the tests")
        check(select("src/a.ts", "package.json") == ["lint", "types", "build", "test"], "verify: a `full` path swaps the changed-files lint for the whole lint")
        check(select("docs/gone.ts") == ["test"], "verify: a changed-files lint with no file left to open is dropped")

        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        (repo / "src" / "b.ts").write_text("b")
        (repo / "src" / "a.ts").unlink()
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "change")
        done = verify(repo, "plan")
        plan = json.loads((repo / "verify-plan.json").read_text())["checks"]
        check(done.returncode == 0 and plan == [["lint-files", "echo lint ./src/b.ts"], ["types", "echo types"], ["test", "echo test"]],
              f"verify: a pull request's plan lints the files that still exist, never a deleted one ({plan})")
        done = verify(repo, "run")
        check(done.returncode == 0 and "lint ./src/b.ts" in done.stdout and "| types | pass |" in done.stdout and "verify checks total" in done.stdout,
              "verify: run prints each check's log and one timed line each")

        done = verify(repo, "plan", "--all")
        plan = json.loads((repo / "verify-plan.json").read_text())["checks"]
        check(done.returncode == 0 and [n for n, _ in plan] == ["lint", "types", "test"], "verify: plan --all is the gate file's default list")

        gate = {**GATE, "default": ["slow", "red", "test", "build"]}
        verify(repo, "plan", "--all", gate=gate, alone="build")
        done = verify(repo, "run", gate=gate, alone="build")
        rows = [line for line in done.stdout.splitlines() if line.startswith("| ") and "check | result" not in line and "---" not in line]
        check(done.returncode == 1 and "::error::verify failed: red" in done.stdout, "verify: one red check fails the job and is named")
        check(len(rows) == 4 and rows[1].startswith("| red | FAIL(3) |") and rows[2].startswith("| test | pass |") and rows[3].startswith("| build | pass |"),
              f"verify: the checks after a red one still run, and an `alone` check runs last ({rows})")
        took = {row.split("|")[1].strip(): float(row.split("|")[3].split()[0]) for row in rows}
        check(took["test"] < took["slow"], "verify: side by side, a quick check is timed as its own, not as the slowest one's")

        done = verify(repo, "plan", "--all", alone="nope")
        check(done.returncode != 0 and "nope" in done.stderr, "verify: an `alone` name the gate file does not have is refused")
        done = verify(repo, "plan", "--everything")
        check(done.returncode != 0 and "usage" in done.stderr, "verify: an unknown flag is refused")

    script = step_script("verify", "Plan and run the checks")
    check('verify.py" plan --all' in script and 'verify.py" plan ;;' in script and 'verify.py" run' in script and "$GITHUB_ACTION_PATH" in script,
          "verify: the action plans by scope, then runs, from its own directory")


if __name__ == "__main__":
    test_shape()
    test_setup()
    test_probes()
    test_verify()
    if FAILED:
        for what in FAILED:
            print(f"::error::{what}")
        sys.exit(1)
    print("actions: all checks passed")
