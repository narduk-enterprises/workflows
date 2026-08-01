#!/usr/bin/env python3
"""Fail-open and no-op proofs for `code-review.yml`.

Same discipline as `test_extra_env.py` and `test_script_gates.py`: the step text
is EXTRACTED from the shipped callable and EXECUTED, so these tests cannot drift
from the workflow they describe. A stub `curl` in front of `PATH` records what
the step actually asked for.

Two properties are worth this much machinery:

  * **It cannot redden a caller's CI.** Advisory means advisory. Every refusal —
    opted out, fork head, missing secret, failed dispatch, non-204 response —
    must exit 0. If any of them ever exits non-zero, an optional review becomes
    a merge blocker on the day the pool is down, which is the exact failure this
    design exists to avoid.
  * **It is a no-op until a repo opts in.** `enabled` defaults to false, so
    moving `v1` onto a commit carrying this file changes the behaviour of zero
    adopters. That is what makes the rollout safe.

And one security property: the dispatch token reaches `curl` through a 0600
config file, never through argv. On a self-hosted guest every listener runs as
the same `runner` user and can read `/proc/<pid>/cmdline`.

Run: python3 scripts/test_code_review_callable.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "code-review.yml"

CALLER_REPOSITORY = "narduk-enterprises/operator-portal"
DISPATCH_REPOSITORY = "narduk-enterprises/agent-infrastructure"
HEAD_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
TOKEN = "ghs-not-a-real-token-0000"

# A stub `curl` that records its argv, its stdin-free config file, and its data
# payload, then prints whatever HTTP status the test asked for. It validates
# nothing: assertions belong in tests, not in fixtures.
STUB_CURL = '''#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
record = {"argv": argv}
for flag, key in (("--config", "config"), ("--data", "data")):
    if flag in argv:
        value = argv[argv.index(flag) + 1]
        path = value[1:] if key == "data" and value.startswith("@") else value
        try:
            with open(path, "r", encoding="utf-8") as handle:
                record[key] = handle.read()
        except OSError:
            record[key] = "<unreadable>"
        if key == "config":
            record["config_mode"] = oct(os.stat(path).st_mode & 0o777)

with open(os.environ["STUB_CURL_LOG"], "w", encoding="utf-8") as handle:
    json.dump(record, handle)

sys.stdout.write(os.environ.get("STUB_CURL_HTTP_CODE", "204"))
sys.exit(int(os.environ.get("STUB_CURL_EXIT", "0")))
'''


def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def review_job() -> dict[str, Any]:
    return workflow()["jobs"]["request-review"]


def step_script() -> str:
    steps = review_job()["steps"]
    if len(steps) != 1:
        raise AssertionError(f"expected exactly one step in the review job, found {len(steps)}")
    return steps[0]["run"]


class Failures:
    def __init__(self) -> None:
        self.items: list[str] = []

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            self.items.append(message)

    def report(self) -> int:
        for item in self.items:
            print(f"::error::{item}")
        print(f"\ntest_code_review_callable: {len(self.items)} failure(s)")
        return 1 if self.items else 0


def run_step(
    *,
    pr_number: str = "52",
    head_repository: str = CALLER_REPOSITORY,
    opted_out: str = "false",
    token: str = TOKEN,
    http_code: str = "204",
    curl_exit: str = "0",
    tier: str = "cheapest-capable",
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    """Execute the shipped step with a stub `curl` and return what it did."""
    with tempfile.TemporaryDirectory(prefix="code-review-step-") as directory:
        base = Path(directory)
        binaries = base / "bin"
        binaries.mkdir()
        stub = binaries / "curl"
        stub.write_text(STUB_CURL, encoding="utf-8")
        stub.chmod(0o755)
        runner_temp = base / "runner-temp"
        runner_temp.mkdir()
        log = base / "curl.json"

        environment = {
            "PATH": f"{binaries}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(base),
            "RUNNER_TEMP": str(runner_temp),
            "STUB_CURL_LOG": str(log),
            "STUB_CURL_HTTP_CODE": http_code,
            "STUB_CURL_EXIT": curl_exit,
            "DISPATCH_REPOSITORY": DISPATCH_REPOSITORY,
            "DISPATCH_EVENT_TYPE": "agent-review-request",
            "REVIEW_TIER": tier,
            "CALLER_REPOSITORY": CALLER_REPOSITORY,
            "PR_NUMBER": pr_number,
            "PR_HEAD_SHA": HEAD_SHA,
            "PR_HEAD_REPOSITORY": head_repository,
            "PR_OPTED_OUT": opted_out,
            "AGENT_REVIEW_DISPATCH_TOKEN": token,
        }
        completed = subprocess.run(
            ["bash", "-c", step_script()],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        dispatched = json.loads(log.read_text(encoding="utf-8")) if log.exists() else None
        leftovers = sorted(path.name for path in runner_temp.iterdir())
    return completed, (dispatched, leftovers)  # type: ignore[return-value]


def main() -> int:
    f = Failures()

    if not shutil.which("bash"):
        print("::error::bash is required to execute the shipped step")
        return 1

    # --- the no-op property ------------------------------------------------
    call = workflow()["on"]["workflow_call"] if "on" in workflow() else workflow()[True]["workflow_call"]
    enabled = call["inputs"]["enabled"]
    f.check(
        enabled["type"] == "boolean" and enabled["default"] is False,
        "`enabled` must be a boolean defaulting to false — otherwise moving the v1 tag "
        "silently switches reviews on for every adopter at once",
    )
    f.check(
        str(review_job().get("if", "")).strip() == "${{ inputs.enabled }}",
        "the review job's `if:` must be exactly `${{ inputs.enabled }}` so a repo that has "
        "not opted in produces a skipped job and dispatches nothing",
    )
    f.check(
        call["secrets"]["AGENT_REVIEW_DISPATCH_TOKEN"].get("required") is not True,
        "AGENT_REVIEW_DISPATCH_TOKEN must stay optional (R7): a required secret hard-fails "
        "every caller that enables the review before the org secret reaches it",
    )
    f.check(
        "Required" not in {job.get("name") for job in workflow()["jobs"].values()},
        "this callable must not declare a `Required` job — it is advisory and must never "
        "become part of `ci / Required`",
    )

    # --- the happy path ----------------------------------------------------
    completed, (dispatched, leftovers) = run_step()
    f.check(completed.returncode == 0, f"happy path exited {completed.returncode}: {completed.stderr}")
    f.check(dispatched is not None, "happy path did not call curl at all")
    if dispatched:
        payload = json.loads(dispatched.get("data", "{}"))
        f.check(
            payload.get("event_type") == "agent-review-request",
            f"dispatched the wrong event_type: {payload.get('event_type')!r}",
        )
        f.check(
            payload.get("client_payload")
            == {
                "repository": CALLER_REPOSITORY,
                "pull_request": 52,
                "head_sha": HEAD_SHA,
                "tier": "cheapest-capable",
            },
            f"client_payload is wrong: {payload.get('client_payload')!r}",
        )
        f.check(
            isinstance(payload.get("client_payload", {}).get("pull_request"), int),
            "pull_request must be a JSON number, not a string — the receiving allowlist "
            "check and the supervisor's issueNumber both expect an integer",
        )
        url = dispatched["argv"][-1]
        f.check(
            url == f"https://api.github.com/repos/{DISPATCH_REPOSITORY}/dispatches",
            f"dispatched at the wrong URL: {url!r}",
        )
        # The security property.
        f.check(
            TOKEN not in " ".join(dispatched["argv"]),
            "the dispatch token appeared in curl's argv — on a self-hosted guest every "
            "listener shares the `runner` user and can read /proc/<pid>/cmdline",
        )
        f.check(
            TOKEN in dispatched.get("config", ""),
            "the dispatch token did not reach curl's --config file, so the Authorization "
            "header was never sent",
        )
        f.check(
            dispatched.get("config_mode") == "0o600",
            f"curl's config file was mode {dispatched.get('config_mode')}, not 0600",
        )
    f.check(
        leftovers == [],
        f"the step left credential-bearing files behind in RUNNER_TEMP: {leftovers}",
    )

    # --- every refusal is green -------------------------------------------
    refusals = {
        "opted out via the no-ai-review label": {"opted_out": "true"},
        "fork head": {"head_repository": "someone-else/operator-portal"},
        "missing dispatch secret": {"token": ""},
        "event carries no pull request": {"pr_number": ""},
    }
    for label, keywords in refusals.items():
        completed, (dispatched, _) = run_step(**keywords)  # type: ignore[arg-type]
        f.check(
            completed.returncode == 0,
            f"refusal '{label}' exited {completed.returncode}; every refusal must be green "
            "or an advisory review becomes a merge blocker",
        )
        f.check(
            dispatched is None,
            f"refusal '{label}' still called curl — it must dispatch nothing",
        )
        f.check(
            "::notice::" in completed.stdout,
            f"refusal '{label}' produced no ::notice:: saying why the review was skipped",
        )

    # --- a broken dispatch is a warning, never an error --------------------
    for label, keywords in {
        "curl transport failure": {"curl_exit": "7", "http_code": ""},
        "GitHub returned 404": {"http_code": "404"},
        "GitHub returned 401": {"http_code": "401"},
    }.items():
        completed, _ = run_step(**keywords)  # type: ignore[arg-type]
        f.check(
            completed.returncode == 0,
            f"'{label}' exited {completed.returncode}; a failed dispatch must not fail the caller",
        )
        f.check(
            "::warning::" in completed.stdout,
            f"'{label}' produced no ::warning:: — a silently swallowed dispatch failure is "
            "indistinguishable from a review nobody requested",
        )
        f.check(
            "::error::" not in completed.stdout,
            f"'{label}' emitted ::error::, which annotates the caller's PR for an advisory feature",
        )

    return f.report()


if __name__ == "__main__":
    sys.exit(main())
