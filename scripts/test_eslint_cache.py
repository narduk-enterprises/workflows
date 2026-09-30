#!/usr/bin/env python3
"""Behaviour tests for the `Fast` lane's ESLint result cache (input `eslint-cache`).

Like `test_store_placement.py`, this does NOT test a copy of the script: it
extracts the `Enable ESLint cache` step's `run:` block out of nuxt-cloudflare.yml
and executes that exact text under bash, then runs the generated preload
against a stub `eslint/bin/eslint.js` that prints the arguments it received.

What is being locked down:

  1. an `eslint` CLI process gets `--cache --cache-location <dir>/ --cache-strategy
     content`, however it was started (the preload is keyed on the entry script,
     not on the caller's package.json script text);
  2. anything else is untouched: another node program, `eslint --print-config`
     (the typegen script), `--version`, and an eslint that already chose its own
     `--cache*` flags;
  3. the cache directory is per lockfile hash and beside the checkout, never in it;
  4. an unwritable location degrades to "uncached" with exit 0 and no outputs,
     never a failed lint;
  5. the wiring: the steps sit in the step list `fast` and `fast-escalable`
     share, before `Run fast scripts`; the hosted restore/save carry the
     github-hosted guard and save only on the default branch; the full gate and
     escalated `Fast` never get the cache.

Run: python3 scripts/test_eslint_cache.py
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/nuxt-cloudflare.yml")
ENABLE = "Enable ESLint cache"
RESTORE = "Restore ESLint cache"
SAVE = "Save ESLint cache"
RUN_FAST = "Run fast scripts"

STUB = "process.stdout.write(JSON.stringify(process.argv.slice(2)))\n"


def steps_of(doc: dict, job: str) -> list[dict]:
    return [s for s in doc["jobs"][job]["steps"] if isinstance(s, dict)]


def step(doc: dict, job: str, name: str) -> dict:
    found = [s for s in steps_of(doc, job) if s.get("name") == name]
    assert len(found) == 1, (job, name, len(found))
    return found[0]


def run_enable(script: str, tmp: Path, *, pm: str, lockfile_body: str | None,
               workspace_parent_writable: bool = True) -> tuple[int, dict, Path, str]:
    parent = tmp / "runner-work"
    workspace = parent / "repo"
    workspace.mkdir(parents=True)
    if lockfile_body is not None:
        (workspace / ("pnpm-lock.yaml" if pm == "pnpm" else "package-lock.json")).write_text(lockfile_body)
    runner_temp = tmp / "runner-temp"
    runner_temp.mkdir()
    out = tmp / "github_output"
    out.write_text("")
    if not workspace_parent_writable:
        parent.chmod(0o500)
    env = {**os.environ, "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(runner_temp),
           "GITHUB_OUTPUT": str(out), "PM": pm}
    try:
        proc = subprocess.run(["bash", "-c", script], cwd=workspace, env=env, capture_output=True, text=True)
    finally:
        if not workspace_parent_writable:
            parent.chmod(0o700)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return proc.returncode, outputs, workspace, proc.stdout + proc.stderr


def eslint_args(shim: str | None, tmp: Path, extra: list[str], *, entry: str = "eslint") -> list[str]:
    """Run the stub entry script under the shim; return the args it saw."""
    if entry == "eslint":
        script = tmp / "node_modules" / "eslint" / "bin" / "eslint.js"
    else:
        script = tmp / "node_modules" / "other" / "cli.js"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(STUB)
    env = {k: v for k, v in os.environ.items() if k != "NODE_OPTIONS"}
    if shim:
        env["NODE_OPTIONS"] = f"--require={shim}"
    proc = subprocess.run(["node", str(script), *extra], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_generated_preload(script: str) -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        code, outputs, workspace, log = run_enable(script, tmp, pm="pnpm", lockfile_body="lockfileVersion: 9\n")
        assert code == 0, log
        assert set(outputs) == {"shim", "path", "lock"}, outputs
        lock = outputs["lock"]
        assert len(lock) == 16 and lock != "none", lock
        # Absolute: actions/cache/save rejects a pattern containing `..`.
        assert os.path.isabs(outputs["path"]) and ".." not in Path(outputs["path"]).parts, outputs
        assert outputs["path"].endswith(f"/.ci-cache/eslint/{lock}"), outputs
        cache_dir = Path(outputs["path"]).resolve()
        assert cache_dir.parent.parent.parent == workspace.resolve().parent, cache_dir
        # Beside the checkout, not in it.
        assert workspace.resolve() not in cache_dir.parents, cache_dir
        assert cache_dir.is_dir()
        shim = outputs["shim"]

        # 1. an eslint CLI process gets the four flags, after the caller's own.
        args = eslint_args(shim, tmp, [".", "--max-warnings", "0"])
        assert args[:3] == [".", "--max-warnings", "0"], args
        assert args[3:5] == ["--cache", "--cache-location"] and args[6:] == ["--cache-strategy", "content"], args
        assert args[5].endswith(os.sep) and Path(args[5]).resolve() == cache_dir, args

        # 2. everything else is untouched.
        assert eslint_args(shim, tmp, ["x"], entry="other") == ["x"]
        for left_alone in (["--print-config", "package.json"], ["--version"], ["--help"], ["-v"],
                           ["--stdin", "--stdin-filename", "a.js"], ["--env-info"],
                           [".", "--cache"], [".", "--no-cache"], [".", "--cache-location", "/x/"],
                           [".", "--cache-strategy=metadata"]):
            assert eslint_args(shim, tmp, left_alone) == left_alone, left_alone

        # 3. a different lockfile is a different (cold) directory.
        tmp2 = tmp / "second"
        tmp2.mkdir()
        _, outputs2, _, _ = run_enable(script, tmp2, pm="pnpm", lockfile_body="lockfileVersion: 9\nx: 1\n")
        assert outputs2["lock"] != lock
        # npm uses package-lock.json; no lockfile at all is a stable "none".
        tmp3 = tmp / "third"
        tmp3.mkdir()
        _, outputs3, _, _ = run_enable(script, tmp3, pm="npm", lockfile_body="{}")
        assert outputs3["lock"] not in ("none", lock), outputs3
        tmp4 = tmp / "fourth"
        tmp4.mkdir()
        _, outputs4, _, _ = run_enable(script, tmp4, pm="pnpm", lockfile_body=None)
        assert outputs4["lock"] == "none" and outputs4["path"].endswith("/eslint/none"), outputs4
    print("PASS  the preload appends the cache flags to an eslint CLI process only, and leaves --cache*/--print-config/other programs alone")


def test_unwritable_location_degrades(script: str) -> None:
    if os.geteuid() == 0:
        print("SKIP  unwritable-location test (running as root cannot be denied a directory)")
        return
    with tempfile.TemporaryDirectory() as t:
        code, outputs, _, log = run_enable(script, Path(t), pm="pnpm", lockfile_body="x\n",
                                           workspace_parent_writable=False)
        assert code == 0, log
        assert outputs == {}, outputs
        assert "linting uncached" in log, log
    print("PASS  an unwritable cache location leaves the lint uncached with exit 0 and no outputs")


def test_wiring(doc: dict) -> None:
    for job in ("fast", "fast-escalable"):
        names = [s.get("name") for s in steps_of(doc, job)]
        assert names.index(ENABLE) < names.index(RESTORE) < names.index(RUN_FAST) < names.index(SAVE), (job, names)
        enable = step(doc, job, ENABLE)
        assert "env.FAST_ENABLED == 'true'" in enable["if"] and "inputs.eslint-cache" in enable["if"], enable["if"]
        # Only the fast-scripts step sees the preload.
        run_fast = step(doc, job, RUN_FAST)
        assert "ESLINT_CACHE_SHIM" in run_fast["env"] and "NODE_OPTIONS" in run_fast["run"]
        for other in steps_of(doc, job):
            if other.get("name") not in (RUN_FAST, ENABLE):
                assert "NODE_OPTIONS" not in json.dumps(other), (job, other.get("name"))
        # Hosted only, restore everywhere, save on the default branch only (R8's guard text).
        restore, save = step(doc, job, RESTORE), step(doc, job, SAVE)
        for s in (restore, save):
            cond = " ".join(str(s["if"]).split())
            assert "runner.environment == 'github-hosted'" in cond, s["name"]
            assert "steps.eslint-cache-dir.outputs.path != ''" in cond, s["name"]
            assert s["uses"].startswith(("actions/cache/restore@", "actions/cache/save@"))
        assert "default_branch" not in restore["if"]
        assert "github.ref_name == github.event.repository.default_branch" in " ".join(save["if"].split())
        assert restore["with"]["key"] == save["with"]["key"]
        assert restore["with"]["restore-keys"].strip() == restore["with"]["key"].rsplit("-${{ github.sha }}", 1)[0].strip() + "-"
    # The gate that must lint cold never carries the cache.
    for job, body in doc["jobs"].items():
        if job in ("fast", "fast-escalable"):
            continue
        text = json.dumps([s for s in body.get("steps") or [] if isinstance(s, dict)])
        assert "eslint-cache-dir" not in text and "narduk-eslint-cache" not in text and "ESLint cache" not in text, job
    trigger = doc.get("on") or doc.get(True)
    assert trigger["workflow_call"]["inputs"]["eslint-cache"]["default"] is True
    print("PASS  wiring: Fast lanes only, before Run fast scripts, hosted-guarded restore, default-branch-only save; the full gate lints cold")


def main() -> int:
    doc = yaml.safe_load(WORKFLOW.read_text())
    fast_enable = step(doc, "fast", ENABLE)["run"]
    assert fast_enable == step(doc, "fast-escalable", ENABLE)["run"]
    test_generated_preload(fast_enable)
    test_unwritable_location_degrades(fast_enable)
    test_wiring(doc)
    print("\ntest_eslint_cache: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
