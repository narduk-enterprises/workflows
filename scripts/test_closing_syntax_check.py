#!/usr/bin/env python3
"""Behaviour tests for closing-syntax-check.yml (agent-infrastructure#837, #1085).

Like `test_extra_env.py` and `test_script_gates.py`, this does NOT test a copy
of the checker: it extracts the "Write vendored checker" and "Run PR
closing-syntax check" `run:` blocks straight out of the workflow YAML and
executes that exact text under bash, against a real temp git repository and a
crafted `GITHUB_EVENT_PATH`. If someone edits the embedded checker or the
step around it, these tests either still pass against the new text or they
fail — there is no third option where the test passes while the shipped
callable is broken.

This is the estate's one PR-closing-syntax gate distributed to every private
repo: it validates the PR body (an ambiguous closing list, or a keyword
outside a canonical position — agent-infrastructure#755) AND every commit
message in the PR's own commit range (a squash-merge commit concatenates
every squashed commit's own subject/body and GitHub scans THAT independently
of the curated PR body — agent-infrastructure#1085, PR #1080's stale
`Closes #1056` trailer surviving inside a reverted commit).

Run: python3 scripts/test_closing_syntax_check.py
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/closing-syntax-check.yml")
WRITE_STEP = "Write vendored checker"
RUN_STEP = "Run PR closing-syntax check"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def extract_steps() -> tuple[str, str]:
    doc = yaml.safe_load(WORKFLOW.read_text())
    steps = {step.get("name"): step["run"] for step in doc["jobs"]["check"]["steps"] if "run" in step}
    for name in (WRITE_STEP, RUN_STEP):
        if name not in steps:
            raise SystemExit(f"::error::no step named {name!r} in {WORKFLOW}")
    return steps[WRITE_STEP], steps[RUN_STEP]


def git(repo_dir: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(
        ["git", *args], cwd=repo_dir, env=env, check=True, capture_output=True, text=True
    )


def run_gate(repo_dir: str, runner_temp: str, write_step: str, run_step: str, event: dict) -> subprocess.CompletedProcess:
    """Execute the two shipped steps exactly as a runner would, in order."""
    event_path = Path(runner_temp) / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    env = {
        **os.environ,
        "RUNNER_TEMP": runner_temp,
        "GITHUB_EVENT_PATH": str(event_path),
    }
    write_result = subprocess.run(
        ["bash", "-c", write_step], cwd=repo_dir, env=env, capture_output=True, text=True
    )
    assert write_result.returncode == 0, write_result.stdout + write_result.stderr
    checker_path = Path(runner_temp) / "check_pr_closing_syntax.py"
    assert checker_path.is_file(), "the write step did not produce the checker file"
    return subprocess.run(["bash", "-c", run_step], cwd=repo_dir, env=env, capture_output=True, text=True)


def seed_repo(repo_dir: str) -> str:
    """One base commit on `main`; returns its sha."""
    git(repo_dir, "init", "--quiet", "--initial-branch=main")
    (Path(repo_dir) / "a.txt").write_text("one\n", encoding="utf-8")
    git(repo_dir, "add", "a.txt")
    git(repo_dir, "commit", "--quiet", "-m", "chore: seed repo")
    return git(repo_dir, "rev-parse", "HEAD").stdout.strip()


def add_commit(repo_dir: str, contents: str, message: str) -> str:
    (Path(repo_dir) / "a.txt").write_text(contents, encoding="utf-8")
    git(repo_dir, "add", "a.txt")
    git(repo_dir, "commit", "--quiet", "-m", message)
    return git(repo_dir, "rev-parse", "HEAD").stdout.strip()


def main() -> int:
    write_step, run_step = extract_steps()
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        if not ok:
            failures.append(f"{label}: {detail}")

    # --- clean PR body, clean commits: the gate must report success ---
    with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as runner_temp:
        base_sha = seed_repo(repo_dir)
        head_sha = add_commit(repo_dir, "two\n", "docs: tidy up\n\nNo issue reference here.\n")
        result = run_gate(
            repo_dir,
            runner_temp,
            write_step,
            run_step,
            {
                "pull_request": {
                    "body": "Refs #10\n",
                    "base": {"sha": base_sha},
                    "head": {"sha": head_sha},
                }
            },
        )
        check(
            "clean PR body and commits pass",
            result.returncode == 0 and "safe" in result.stdout,
            result.stdout + result.stderr,
        )

    # --- PR body carries a closing keyword outside a canonical position ---
    with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as runner_temp:
        base_sha = seed_repo(repo_dir)
        head_sha = add_commit(repo_dir, "two\n", "docs: tidy up\n")
        result = run_gate(
            repo_dir,
            runner_temp,
            write_step,
            run_step,
            {
                "pull_request": {
                    "body": 'Refs #10 -- the brief said "Closes #10", which this PR declines.\n',
                    "base": {"sha": base_sha},
                    "head": {"sha": head_sha},
                }
            },
        )
        check(
            "misplaced PR-body closing keyword fails",
            result.returncode == 1 and "Closing keyword out of position" in result.stdout,
            result.stdout + result.stderr,
        )

    # --- PR body is clean, but an individual commit carries a closing trailer ---
    # This is agent-infrastructure PR #1080's exact shape (#1085): the final
    # body is correct, but a commit in the branch still carries the keyword.
    with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as runner_temp:
        base_sha = seed_repo(repo_dir)
        add_commit(repo_dir, "two\n", "docs(nvault): add fleet-runner-reaper\n\nCloses #1056\n")
        head_sha = add_commit(repo_dir, "three\n", "revert: drop fleet-runner-reaper, keep #1056 open\n")
        result = run_gate(
            repo_dir,
            runner_temp,
            write_step,
            run_step,
            {
                "pull_request": {
                    "body": "Refs #1056\n",
                    "base": {"sha": base_sha},
                    "head": {"sha": head_sha},
                }
            },
        )
        check(
            "clean PR body with an offending commit still fails",
            result.returncode == 1
            and "Closing keyword in commit message" in result.stdout
            and "closing keywords belong in the PR body only" in result.stdout,
            result.stdout + result.stderr,
        )

    # --- a non-PR event (push/workflow_dispatch): no commit scan, stays safe ---
    with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as runner_temp:
        seed_repo(repo_dir)
        result = run_gate(repo_dir, runner_temp, write_step, run_step, {"ref": "refs/heads/main"})
        check(
            "non-PR event stays safe with no commit scan",
            result.returncode == 0 and "safe" in result.stdout,
            result.stdout + result.stderr,
        )

    if failures:
        for failure in failures:
            print(f"::error::{failure}")
        print(f"test_closing_syntax_check: {len(failures)} failure(s)")
        return 1
    print("test_closing_syntax_check: all cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
