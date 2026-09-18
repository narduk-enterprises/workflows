#!/usr/bin/env python3
"""Pull-request E2E subset: `e2e-pr-shards` / `e2e-pr-args` (workflows#83).

The estate's `playwright-isolated` pool is three effective slots against a
declared seven, shared by roughly ten repositories, so a pull request waits on
QUEUE rather than on compute: buoys PR run 35165732448 queued 295s to run a
128s shard, against 1s of queue on the same repo's push run 35165183477.
Fewer pull-request lanes is the only lever a caller has over that from inside
this callable.

Two things have to hold, and only one of them is about speed:

1. **Unset is today's behaviour.** `e2e-pr-shards: 0` and `e2e-pr-args: ""`
   are the defaults, and with them a pull request must resolve to exactly the
   same shard count and argument list as a push. Every existing adopter is
   pinned by SHA and passes neither input, so a regression here is a silent
   change to what ten repositories test on every pull request.

2. **The effective values are resolved ONCE.** `e2e` builds
   `--shard=<n>/<total>`, `e2e-report` decides whether there is anything to
   merge, and `required` decides whether to demand that report. All three read
   the plan job's outputs. If any of them went back to reading
   `inputs.e2e-shards` directly, a pull-request override would make them
   disagree: Playwright told it is shard 1 of 3 while one lane exists runs a
   third of the suite and reports success, and `Required` demands a report job
   that was never fanned out. Those are a false green and a false red on a
   correct run, so the wiring is asserted structurally, not just behaviourally.

Same discipline as the sibling tests: assert the shipped source, then execute
the shipped `run:` text against fixtures.

Run: python3 scripts/test_e2e_pr_subset.py
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile

import yaml

WORKFLOW = pathlib.Path(".github/workflows/nuxt-cloudflare.yml")

PUSH_EVENTS = ("push", "workflow_dispatch", "schedule", "merge_group", "release")
PR_EVENTS = ("pull_request", "pull_request_target")


def load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def named_step(job: dict, name: str) -> dict:
    matches = [s for s in job.get("steps", []) if s.get("name") == name]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {name!r} step")
    return matches[0]


def plan_step_script() -> str:
    return named_step(load()["jobs"]["e2e-plan"], "Compute shard list")["run"]


# --------------------------------------------------------------------------
# Source contract
# --------------------------------------------------------------------------


def check_inputs_declared() -> None:
    doc = load()
    # PyYAML resolves the bare key `on:` to the boolean True (YAML 1.1
    # "y/yes/on" truthiness), so every sibling test reads it this way.
    trigger = doc.get(True, doc.get("on", {}))
    inputs = trigger["workflow_call"]["inputs"]
    for name, kind, default in (
        ("e2e-pr-shards", "number", 0),
        ("e2e-pr-args", "string", ""),
    ):
        assert name in inputs, f"{name} input must be declared"
        spec = inputs[name]
        assert spec.get("required") is False, f"{name} must be optional"
        assert spec.get("type") == kind, f"{name} must be type {kind}"
        assert spec.get("default") == default, (
            f"{name} default must be {default!r} — the UNSET sentinel that "
            f"preserves today's behaviour for every pinned adopter"
        )
    print("PASS  e2e-pr-shards / e2e-pr-args declared, optional, unset by default")


def check_single_resolution_point() -> None:
    """The three consumers read the plan job's outputs, never the raw inputs."""
    doc = load()
    jobs = doc["jobs"]

    outputs = jobs["e2e-plan"].get("outputs", {})
    for key in ("shard-total", "e2e-args"):
        assert key in outputs, f"e2e-plan must output {key!r}"

    run_step = named_step(jobs["e2e"], "Run e2e suite")
    env = run_step.get("env", {})
    assert "needs.e2e-plan.outputs.shard-total" in str(env.get("TOTAL", "")), (
        "e2e 'Run e2e suite' TOTAL must come from the plan job's shard-total: "
        "the matrix was fanned out from it, so --shard=<n>/<total> must match "
        "or part of the suite silently never runs"
    )
    assert "needs.e2e-plan.outputs.e2e-args" in str(env.get("EXTRA_ARGS", "")), (
        "e2e 'Run e2e suite' EXTRA_ARGS must come from the plan job's e2e-args"
    )
    assert "inputs.e2e-shards" not in str(env.get("TOTAL", "")), (
        "e2e must not read inputs.e2e-shards directly"
    )

    report_if = str(jobs["e2e-report"].get("if", ""))
    assert "needs.e2e-plan.outputs.shard-total" in report_if, (
        "e2e-report's if: must gate on the plan job's shard-total"
    )
    assert "inputs.e2e-shards" not in report_if, (
        "e2e-report must not gate on inputs.e2e-shards — with a pull-request "
        "override it would demand a merge of a single unsharded lane"
    )

    required_env = named_step(
        jobs["required"], "Require enabled gates to succeed and disabled gates to skip"
    ).get("env", {})
    shards_expr = str(required_env.get("E2E_SHARDS", ""))
    assert "needs.e2e-plan.outputs.shard-total" in shards_expr, (
        "required must aggregate on the plan job's shard-total"
    )
    print("PASS  e2e / e2e-report / required all read the plan job's effective values")


# --------------------------------------------------------------------------
# Behaviour
# --------------------------------------------------------------------------


def run_plan(
    *,
    event_name: str,
    total: str,
    pr_total: str,
    args: str,
    pr_args: str,
    skipped: str = "false",
    full: str = "",
) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        output_path = root / "github_output"
        output_path.write_text("")
        summary_path = root / "github_step_summary"
        summary_path.write_text("")
        env = dict(os.environ)
        env.update(
            {
                "EVENT_NAME": event_name,
                "TOTAL": total,
                "PR_TOTAL": pr_total,
                "ARGS": args,
                "PR_ARGS": pr_args,
                "SKIPPED": skipped,
                "FULL": full,
                "GITHUB_OUTPUT": str(output_path),
                "GITHUB_STEP_SUMMARY": str(summary_path),
            }
        )
        result = subprocess.run(
            ["bash", "-c", plan_step_script()],
            capture_output=True,
            text=True,
            env=env,
        )
        written: dict[str, str] = {}
        for line in output_path.read_text().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                written[key] = value
        return result, written


def check_behaviour() -> None:
    failures = 0

    def case(label: str, *, expect_rc: int = 0, expect: dict[str, str] | None = None, **kw):
        nonlocal failures
        result, written = run_plan(**kw)
        ok = result.returncode == expect_rc
        if ok and expect is not None:
            ok = all(written.get(k) == v for k, v in expect.items())
        if ok:
            print(f"PASS  {label}")
        else:
            failures += 1
            print(
                f"FAIL  {label}: rc={result.returncode} (want {expect_rc}) "
                f"written={written!r} want~{expect!r} stderr={result.stderr!r}"
            )

    # -- 1. Unset overrides are exactly today's behaviour, on every event. --
    for event in PUSH_EVENTS + PR_EVENTS:
        case(
            f"{event}: overrides unset -> e2e-shards/e2e-args unchanged",
            event_name=event,
            total="3",
            pr_total="0",
            args="--workers=1",
            pr_args="",
            expect={"shard-total": "3", "e2e-args": "--workers=1", "shards": "[1,2,3]"},
        )

    # -- 2. A pull request takes the override. --
    for event in PR_EVENTS:
        case(
            f"{event}: shard + args override applied",
            event_name=event,
            total="3",
            pr_total="1",
            args="--workers=1",
            pr_args="--project=smoke",
            expect={"shard-total": "1", "e2e-args": "--project=smoke", "shards": "[1]"},
        )

    # -- 3. A push NEVER takes it: the canonical-branch validation this input
    #       must not weaken, the same confinement e2e-skip-paths has. --
    for event in PUSH_EVENTS:
        case(
            f"{event}: override present but ignored -> full suite",
            event_name=event,
            total="3",
            pr_total="1",
            args="--workers=1",
            pr_args="--project=smoke",
            expect={"shard-total": "3", "e2e-args": "--workers=1", "shards": "[1,2,3]"},
        )

    # -- 3b. e2e-full-paths: a PR whose files matched (FULL=true) runs the
    #        full e2e-args/e2e-shards; FULL=false keeps the PR tier. --
    for event in PR_EVENTS:
        case(
            f"{event}: FULL=true -> override ignored, full suite",
            event_name=event,
            total="3",
            pr_total="1",
            args="--workers=1",
            pr_args="--project=smoke",
            full="true",
            expect={"shard-total": "3", "e2e-args": "--workers=1", "shards": "[1,2,3]"},
        )
        case(
            f"{event}: FULL=false -> pull-request tier",
            event_name=event,
            total="3",
            pr_total="1",
            args="--workers=1",
            pr_args="--project=smoke",
            full="false",
            expect={"shard-total": "1", "e2e-args": "--project=smoke", "shards": "[1]"},
        )

    # -- 4. An unrecognised event falls through to the FULL configuration.
    #       Fail-closed direction: running too much costs runner time,
    #       running too little merges something unproven. --
    case(
        "unknown event name -> full suite (fail closed)",
        event_name="some_future_event",
        total="3",
        pr_total="1",
        args="",
        pr_args="--project=smoke",
        expect={"shard-total": "3", "shards": "[1,2,3]"},
    )

    # -- 5. Partial overrides are independent. --
    case(
        "pull_request: shards overridden, args inherited",
        event_name="pull_request",
        total="3",
        pr_total="1",
        args="--workers=1",
        pr_args="",
        expect={"shard-total": "1", "e2e-args": "--workers=1"},
    )
    case(
        "pull_request: args overridden, shards inherited",
        event_name="pull_request",
        total="3",
        pr_total="0",
        args="--workers=1",
        pr_args="--project=smoke",
        expect={"shard-total": "3", "e2e-args": "--project=smoke"},
    )

    # -- 6. The skip decision still wins over any override. --
    case(
        "pull_request: e2e-skip-paths skipped -> empty matrix despite override",
        event_name="pull_request",
        total="3",
        pr_total="1",
        args="",
        pr_args="--project=smoke",
        skipped="true",
        expect={"shards": "[]", "shard-total": "1"},
    )

    # -- 7. Validation fails closed. --
    case(
        "e2e-shards non-numeric -> error",
        event_name="push",
        total="abc",
        pr_total="0",
        args="",
        pr_args="",
        expect_rc=1,
    )
    case(
        "e2e-pr-shards negative -> error",
        event_name="pull_request",
        total="3",
        pr_total="-1",
        args="",
        pr_args="",
        expect_rc=1,
    )
    case(
        "e2e-pr-shards non-numeric -> error, even on a push that would ignore it",
        event_name="push",
        total="3",
        pr_total="nope",
        args="",
        pr_args="",
        expect_rc=1,
    )
    # A newline would truncate the value in $GITHUB_OUTPUT and let the
    # remainder be read as a further output assignment.
    case(
        "newline in e2e-args -> error",
        event_name="push",
        total="3",
        pr_total="0",
        args="--workers=1\nshard-total=99",
        pr_args="",
        expect_rc=1,
    )
    case(
        "newline in e2e-pr-args -> error, even on a push that would ignore it",
        event_name="push",
        total="3",
        pr_total="0",
        args="",
        pr_args="--project=smoke\nshard-total=99",
        expect_rc=1,
    )

    if failures:
        raise SystemExit(f"{failures} case(s) failed")



def required_step_script(*, extra_gate_scripts: str = "") -> str:
    """The shipped `Required` aggregation, with its one inline expression bound.

    The step interpolates `${{ inputs.extra-gate-scripts }}` directly into the
    script text rather than passing it through `env:`, so executing the shipped
    text means substituting it the way Actions would.
    """
    script = named_step(
        load()["jobs"]["required"],
        "Require enabled gates to succeed and disabled gates to skip",
    )["run"]
    token = "${{ inputs.extra-gate-scripts }}"
    assert token in script, "Required step no longer interpolates extra-gate-scripts"
    return script.replace(token, extra_gate_scripts)


def run_required(**env_overrides) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        {
            "BUILD_RESULT": "success",
            "CHECKS_RESULT": "success",
            "EXTRA_GATE_RESULT": "skipped",
            "CALLER_LINT_RESULT": "success",
            "E2E_PLAN_RESULT": "success",
            "E2E_PLAN_SKIPPED": "false",
            "E2E_RESULT": "success",
            "E2E_REPORT_RESULT": "skipped",
            "E2E_SHARDS": "1",
            "RUN_E2E": "true",
            "RUN_DEPLOY_DRY_RUN": "false",
            "DEPLOY_DRY_RUN_RESULT": "skipped",
            # The preview lane (V1) is off in these cases: they are about the
            # E2E subset's aggregation, and `preview-checks: none` is the state
            # that must leave `Required` reading exactly as it did before.
            "PREVIEW_CHECKS": "none",
            "PREVIEW_RESULT": "skipped",
            "EVENT_NAME": "pull_request",
        }
    )
    env.update({k: str(v) for k, v in env_overrides.items()})
    return subprocess.run(
        ["bash", "-c", required_step_script()],
        capture_output=True,
        text=True,
        env=env,
    )


def check_required_still_gates() -> None:
    """A subset is a SMALLER gate, not a disabled one.

    The whole risk of shipping a pull-request subset is that it quietly stops
    gating: fewer lanes must still mean a red `Required` when a test in the
    surviving lane fails. These execute the shipped aggregation with the
    subset active (one effective shard) rather than asserting its text.
    """
    failures = 0

    def case(label: str, *, expect_rc: int, expect_text: str | None = None, **env):
        nonlocal failures
        result = run_required(**env)
        ok = result.returncode == expect_rc
        if ok and expect_text is not None:
            ok = expect_text in (result.stdout + result.stderr)
        if ok:
            print(f"PASS  {label}")
        else:
            failures += 1
            print(
                f"FAIL  {label}: rc={result.returncode} (want {expect_rc}) "
                f"out={result.stdout!r} err={result.stderr!r}"
            )

    # Typecheck and unit tests moved out of `build` into `checks` (row 19):
    # a red `checks` must still turn Required red.
    case(
        "checks failed -> Required RED",
        expect_rc=1,
        expect_text="checks job reported 'failure'",
        CHECKS_RESULT="failure",
    )
    case(
        "checks skipped -> Required RED",
        expect_rc=1,
        CHECKS_RESULT="skipped",
    )

    # The negative proof: one lane, and that lane fails.
    case(
        "subset active + e2e failed -> Required RED",
        expect_rc=1,
        expect_text="e2e job reported 'failure'",
        E2E_SHARDS="1",
        E2E_RESULT="failure",
    )
    case(
        "subset active + e2e cancelled -> Required RED",
        expect_rc=1,
        E2E_SHARDS="1",
        E2E_RESULT="cancelled",
    )
    # A skipped e2e is never a success substitute: an expression mistake that
    # skipped the only lane must not report green.
    case(
        "subset active + e2e skipped while enabled -> Required RED",
        expect_rc=1,
        expect_text="e2e job reported 'skipped'",
        E2E_SHARDS="1",
        E2E_RESULT="skipped",
    )
    # The green shape the gonogo canary actually produced.
    case(
        "subset active + e2e green + report skipped -> Required GREEN",
        expect_rc=0,
        E2E_SHARDS="1",
        E2E_RESULT="success",
        E2E_REPORT_RESULT="skipped",
    )
    # One lane must NOT expect a merged report; if one ran, the plan and the
    # matrix disagreed and that disagreement is the bug this PR exists to stop.
    case(
        "subset active + report ran anyway -> Required RED",
        expect_rc=1,
        E2E_SHARDS="1",
        E2E_RESULT="success",
        E2E_REPORT_RESULT="success",
    )
    # Unset overrides: the full-suite shape still demands the report.
    case(
        "no override (3 shards) + report skipped -> Required RED",
        expect_rc=1,
        E2E_SHARDS="3",
        E2E_REPORT_RESULT="skipped",
    )
    case(
        "no override (3 shards) + report green -> Required GREEN",
        expect_rc=0,
        E2E_SHARDS="3",
        E2E_REPORT_RESULT="success",
    )
    # The numeric guard: a non-count must name itself rather than aborting
    # wordlessly on `-gt` under `set -e`.
    for bad in ("", "0", "abc"):
        case(
            f"effective shard count {bad!r} -> actionable ::error::",
            expect_rc=1,
            expect_text="effective e2e shard count is not a positive integer",
            E2E_SHARDS=bad,
        )

    if failures:
        raise SystemExit(f"{failures} Required case(s) failed")


def main() -> None:
    check_inputs_declared()
    check_single_resolution_point()
    check_behaviour()
    check_required_still_gates()
    print("\ne2e pull-request subset contract passed")


if __name__ == "__main__":
    main()
