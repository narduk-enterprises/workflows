#!/usr/bin/env python3
"""Behaviour tests for the self-hosted pnpm store-placement step (company-hq#269).

Like `test_extra_env.py`, this does NOT test a copy of the script: it extracts
the `Point pnpm at a workspace-local store (self-hosted)` step's `run:` block
out of every callable that carries it and executes that exact text under bash,
so the test and the shipped workflows cannot drift apart.

What is being locked down. pnpm materialises `node_modules` by HARD-LINKING out
of its store, and a hard link cannot cross a device boundary; when store and
workspace sit on different filesystems pnpm silently falls back to copying every
file. On `linux-ci` the workspace is on the transient volume and pnpm's default
store is under `$HOME` on the root volume, so every install there was copying —
3.93s/3.03s/2.83s and 220 MiB of transient volume for a 212 MiB dependency tree,
against 2.71s/2.02s/2.32s and 17 MiB once the store shares the workspace's
filesystem. The step is load-bearing for wall time and for the volume whose 80%
mark blocks new allocations, and all three of its branches have to hold:

  1. workspace on `/`             -> do nothing (default store already shares it)
  2. no writable store directory  -> do nothing (slower install, never a failure)
  3. writable store directory     -> export `npm_config_store_dir`

Branch 2 is the one worth the most: the transient mount root is root-owned, so
the store directory is provisioned out of band, and a guest that has not been
provisioned yet must degrade quietly rather than fail every run on it.

`df` is shimmed rather than called for real, so the mount point under test is
chosen by the test instead of by the host it runs on — which is also what lets
this run identically on a developer Mac (whose `df` has no `--output`) and on
the Linux runner.

Run: python3 scripts/test_store_placement.py
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import yaml

STEP_NAME = "Point pnpm at a workspace-local store (self-hosted)"
WORKFLOWS = (
    Path(".github/workflows/nuxt-cloudflare.yml"),
    Path(".github/workflows/node-library.yml"),
    Path(".github/workflows/reusable-node-ci.yml"),
    Path(".github/workflows/reusable-browser-tests.yml"),
)


def extract_steps() -> list[tuple[str, str, str]]:
    """Every copy of the step in every callable: (workflow, job id, script)."""
    found: list[tuple[str, str, str]] = []
    for wf in WORKFLOWS:
        doc = yaml.safe_load(wf.read_text())
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if isinstance(step, dict) and step.get("name") == STEP_NAME:
                    found.append((wf.name, job_id, step["run"]))
    if not found:
        raise SystemExit(f"::error::no step named {STEP_NAME!r} in any callable")
    return found


def run(script: str, workspace: Path, df_target: str, tmp: Path) -> tuple[int, str, str]:
    """Execute the extracted step with `df` answering `df_target`."""
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "df"
    shim.write_text(f'#!/bin/sh\necho "Mounted on"\necho "{df_target}"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    github_env = tmp / "github_env"
    github_env.write_text("")
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_ENV": str(github_env),
    }
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    return proc.returncode, github_env.read_text().strip(), proc.stdout + proc.stderr


def main() -> int:
    steps = extract_steps()
    failures = 0
    checks = 0

    for wf_name, job_id, script in steps:
        label = f"{wf_name}:{job_id}"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = tmp / "work" / "repo" / "repo"
            workspace.mkdir(parents=True)
            mount = tmp / "mnt"
            mount.mkdir()
            store = mount / "pnpm-store"

            def check(case: str, target: str, want_rc: int, want_env: str) -> None:
                nonlocal failures, checks
                checks += 1
                rc, env_out, log = run(script, workspace, target, tmp)
                if rc != want_rc or env_out != want_env:
                    print(
                        f"FAIL  {label}  {case}\n"
                        f"        want rc={want_rc} env={want_env!r}\n"
                        f"        got  rc={rc} env={env_out!r}\n"
                        f"        {log.strip()}"
                    )
                    failures += 1
                else:
                    print(f"PASS  {label}  {case}")

            # The runner is on a guest with no separate volume for the
            # workspace. pnpm's default store under $HOME is already on the same
            # device, so moving it buys nothing and the step must not try.
            check("workspace on / -> untouched", "/", 0, "")

            # The store directory has not been provisioned on this guest. This
            # is the quiet-degradation case: an unprovisioned guest keeps
            # copying, which is slow, and must not turn every run on it red.
            check("store dir absent -> untouched, exit 0", str(mount), 0, "")

            # The provisioned, runner-owned directory: the whole point.
            store.mkdir()
            check(
                "writable store dir -> exported",
                str(mount),
                0,
                f"npm_config_store_dir={store}",
            )

            # Provisioned but owned by someone else. Same contract as absent:
            # never fail the run over a store the job cannot write.
            if os.geteuid() != 0:
                store.chmod(0o500)
                try:
                    check("store dir not writable -> untouched, exit 0", str(mount), 0, "")
                finally:
                    store.chmod(0o755)
            else:
                print(f"SKIP  {label}  read-only store case (running as root, which can write anyway)")

    print(f"\ntest_store_placement: {len(steps)} step copies, {checks} case(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
