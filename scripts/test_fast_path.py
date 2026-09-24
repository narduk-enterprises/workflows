#!/usr/bin/env python3
"""Contract tests for nuxt-cloudflare.yml's fast-path inputs (narduk reboot P3-C1).

Covers `fast-scripts` (`ci / Fast`), `required-reuse-pr-results` (tree-match
reuse of the whole `Required` result), `e2e-pr-only-changed`, and the
post-deploy journey smoke with its rollback hook (`journey-smoke-*`).

Like the other test_*.py files here, nothing is tested against a copy: job
conditions and names are EVALUATED from the shipped YAML with the expression
interpreter in test_runner_default.py, and every `run:`/`script:` block is
extracted and executed against fixtures.

The first group is the backward-compatibility contract: with every new input
at its default, no new job runs, no job is named `Fast`, every pre-existing
lane keeps its old run condition, and `Required` reads exactly as before.

Run: python3 scripts/test_fast_path.py
"""

from __future__ import annotations

import itertools
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_e2e_reuse  # noqa: E402  (shared github-script harness)
from test_runner_default import Evaluator  # noqa: E402

WORKFLOW = Path(".github/workflows/nuxt-cloudflare.yml")
DOC = yaml.safe_load(WORKFLOW.read_text())
JOBS = DOC["jobs"]
INPUTS = DOC[True]["workflow_call"]["inputs"]
NEW_INPUTS = {
    "fast-scripts": "",
    "required-reuse-pr-results": False,
    "e2e-pr-only-changed": False,
    "journey-smoke-url": "",
    "journey-smoke-args": "--grep=@smoke --retries=1",
    "journey-smoke-rollback-script": "",
    "journey-smoke-rollback-to": "",
}
GATE_STEP = "Require enabled gates to succeed and disabled gates to skip"
FAST_STEP = "Require fast-path and journey-smoke lanes"


class StatusEvaluator(Evaluator):
    """Adds the job-status functions a job-level `if:` may call."""

    def call(self, name, args):  # type: ignore[override]
        status = self.ctx.get("__status", "success")
        if name == "always":
            return True
        if name == "cancelled":
            return status == "cancelled"
        if name == "success":
            return status == "success"
        if name == "failure":
            return status == "failure"
        return Evaluator.call(name, args)


def ev(expr, ctx: dict):
    if isinstance(expr, bool):
        return expr
    text = str(expr).strip()
    m = re.fullmatch(r"\$\{\{(.*)\}\}", text, re.S)
    return StatusEvaluator(ctx).run(m.group(1) if m else text)


def defaults() -> dict:
    return {name: spec.get("default") for name, spec in INPUTS.items()}


def context(event: str, ref: str = "refs/heads/main", inputs: dict | None = None,
            needs: dict | None = None, status: str = "success") -> dict:
    merged = defaults()
    merged.update(inputs or {})
    return {
        "inputs": merged,
        "github": {"event_name": event, "ref": ref,
                   "event": {"repository": {"default_branch": "main", "private": True}}},
        "needs": needs or {},
        "__status": status,
    }


EVENTS = [("pull_request", "refs/pull/1/merge"), ("push", "refs/heads/main"),
          ("push", "refs/heads/feature"), ("schedule", "refs/heads/main"),
          ("workflow_dispatch", "refs/heads/main"), ("pull_request_target", "refs/heads/main")]
SKIPPED_REUSE = {"reuse-plan": {"result": "skipped", "outputs": {}}}


def step(job: str, name: str) -> dict:
    return next(s for s in JOBS[job]["steps"] if s.get("name") == name)


def test_new_inputs_default_off() -> None:
    for name, default in NEW_INPUTS.items():
        spec = INPUTS[name]
        assert spec.get("required") is False, name
        assert spec.get("default") == default, (name, spec.get("default"))
    assert DOC[True]["workflow_call"]["secrets"]["JOURNEY_SMOKE_ROLLBACK_TOKEN"] == {"required": False}
    print("PASS  every fast-path input is optional and defaults off")


def test_defaults_leave_every_lane_as_before() -> None:
    # Pre-existing root lanes: old condition vs. new condition under defaults.
    old_conditions = {"build": "true", "checks": "true",
                      "extra-gate": "inputs.extra-gate-scripts != ''", "e2e-plan": "inputs.run-e2e"}
    for (event, ref), extra_gate, run_e2e, status in itertools.product(
            EVENTS, ["", "lint"], [True, False], ["success", "cancelled"]):
        ctx = context(event, ref, {"extra-gate-scripts": extra_gate, "run-e2e": run_e2e},
                      SKIPPED_REUSE, status)
        assert ev(JOBS["reuse-plan"]["if"], ctx) is False
        for job, old in old_conditions.items():
            # A job with no needs runs unless the run was cancelled.
            expected = (status != "cancelled") and bool(ev(old, ctx))
            assert bool(ev(JOBS[job]["if"], ctx)) == expected, (job, event, status)
        needs = dict(SKIPPED_REUSE, **{"e2e-plan": {"result": "success", "outputs": {"full": "true"}}})
        ctx["needs"] = needs
        assert ev(JOBS["fast"]["if"], ctx) is False
        assert ev(JOBS["fast"]["name"], ctx) == "Fast (not run)"
        assert ev(JOBS["fast-escalated"]["if"], ctx) is False
        assert ev(JOBS["fast-escalated"]["name"], ctx) != "Fast"
        assert ev(JOBS["journey-smoke"]["if"], ctx) is False
        key_step = step("required", "Compute Required proof key")
        assert ev(key_step["if"], ctx) is False
    checkout = JOBS["e2e"]["steps"][0]
    for event, ref in EVENTS:
        assert ev(checkout["with"]["fetch-depth"], context(event, ref)) == 1
    print("PASS  defaults: new jobs skip, no job is named Fast, old lanes keep their conditions")


def run_bash(script: str, env: dict, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], env={**os.environ, **env}, cwd=cwd,
                          capture_output=True, text=True)


def gate_script() -> str:
    return step("required", GATE_STEP)["run"].replace("${{ inputs.extra-gate-scripts }}", "")


GATE_BASE = {
    "BUILD_RESULT": "success", "CHECKS_RESULT": "success", "CALLER_LINT_RESULT": "success",
    "EXTRA_GATE_RESULT": "skipped", "E2E_PLAN_RESULT": "success", "E2E_RESULT": "success",
    "E2E_REPORT_RESULT": "skipped", "DEPLOY_DRY_RUN_RESULT": "skipped", "PREVIEW_RESULT": "skipped",
    "PREVIEW_CHECKS": "none", "EVENT_NAME": "push", "RUN_E2E": "true", "E2E_SHARDS": "1",
    "E2E_PLAN_SKIPPED": "false", "RUN_DEPLOY_DRY_RUN": "false", "EXPECTED_CANDIDATE": "",
}
LANES = ["BUILD_RESULT", "CHECKS_RESULT", "EXTRA_GATE_RESULT", "E2E_PLAN_RESULT", "E2E_RESULT",
         "E2E_REPORT_RESULT", "PREVIEW_RESULT", "DEPLOY_DRY_RUN_RESULT"]


def test_required_gate_reuse_and_smoke_branches() -> None:
    script = gate_script()
    # Default (unset) keeps the old behaviour: a failed build still fails.
    assert run_bash(script, GATE_BASE).returncode == 0
    assert run_bash(script, {**GATE_BASE, "BUILD_RESULT": "failure"}).returncode == 1
    assert run_bash(script, {**GATE_BASE, "REUSED": "", "JOURNEY_SMOKE_URL": "",
                             "BUILD_RESULT": "skipped"}).returncode == 1
    all_skipped = {**GATE_BASE, **{lane: "skipped" for lane in LANES}}
    for mode in ({"REUSED": "true"}, {"JOURNEY_SMOKE_URL": "https://app.example"}):
        assert run_bash(script, {**all_skipped, **mode}).returncode == 0, mode
        for lane, outcome in itertools.product(LANES, ["success", "failure", "cancelled"]):
            result = run_bash(script, {**all_skipped, **mode, lane: outcome})
            assert result.returncode == 1, (mode, lane, outcome)
        for outcome in ["failure", "skipped", "cancelled"]:
            result = run_bash(script, {**all_skipped, **mode, "CALLER_LINT_RESULT": outcome})
            assert result.returncode == 1, (mode, outcome)
    print("PASS  Required: reuse/smoke mode demands every CI lane skipped; default path unchanged")


def test_required_fast_path_step() -> None:
    script = step("required", FAST_STEP)["run"]
    base = {"REUSE_APPLIES": "false", "REUSE_PLAN_RESULT": "skipped", "REUSED": "",
            "FAST_SCRIPTS": "", "FAST_RESULT": "skipped", "ESCALATED": "false",
            "FAST_ESCALATED_RESULT": "skipped", "JOURNEY_SMOKE_URL": "", "JOURNEY_SMOKE_RESULT": "skipped"}
    cases = [
        ({}, 0),
        ({"FAST_RESULT": "success"}, 1),
        ({"JOURNEY_SMOKE_RESULT": "success"}, 1),
        ({"REUSE_PLAN_RESULT": "success"}, 1),
        ({"FAST_SCRIPTS": "lint", "FAST_RESULT": "success"}, 0),
        ({"FAST_SCRIPTS": "lint", "FAST_RESULT": "failure"}, 1),
        ({"FAST_SCRIPTS": "lint", "FAST_RESULT": "skipped"}, 1),
        ({"FAST_SCRIPTS": "lint", "FAST_RESULT": "success", "ESCALATED": "true",
          "FAST_ESCALATED_RESULT": "success"}, 0),
        ({"FAST_SCRIPTS": "lint", "FAST_RESULT": "success", "ESCALATED": "true",
          "FAST_ESCALATED_RESULT": "failure"}, 1),
        ({"FAST_SCRIPTS": "lint", "FAST_RESULT": "success", "ESCALATED": "true",
          "FAST_ESCALATED_RESULT": "skipped"}, 1),
        ({"FAST_SCRIPTS": "lint", "FAST_RESULT": "success", "FAST_ESCALATED_RESULT": "success"}, 1),
        ({"REUSE_APPLIES": "true", "REUSE_PLAN_RESULT": "success"}, 0),
        ({"REUSE_APPLIES": "true", "REUSE_PLAN_RESULT": "failure"}, 1),
        ({"REUSE_APPLIES": "true", "REUSE_PLAN_RESULT": "success", "REUSED": "true",
          "FAST_SCRIPTS": "lint", "FAST_RESULT": "skipped"}, 0),
        ({"JOURNEY_SMOKE_URL": "https://app.example", "JOURNEY_SMOKE_RESULT": "success"}, 0),
        ({"JOURNEY_SMOKE_URL": "https://app.example", "JOURNEY_SMOKE_RESULT": "failure"}, 1),
        ({"JOURNEY_SMOKE_URL": "https://app.example", "JOURNEY_SMOKE_RESULT": "success",
          "FAST_SCRIPTS": "lint", "FAST_RESULT": "skipped"}, 0),
    ]
    for overrides, expected in cases:
        result = run_bash(script, {**base, **overrides})
        assert result.returncode == expected, (overrides, result.stdout, result.stderr)
    print(f"PASS  Required fast-path/smoke aggregation ({len(cases)} cases)")


def test_fast_naming_never_skips_a_fast_check() -> None:
    """A job evaluated to the name `Fast` must always run: GitHub reports a
    skipped job as a passing check, so a skipped `Fast` would satisfy a ruleset
    without proving anything."""
    matrix = itertools.product(
        EVENTS, ["", "lint typecheck"], ["", "https://app.example"], ["", "true", "false"],
        ["true", "false", ""], ["success", "cancelled"])
    seen_fast = 0
    for (event, ref), scripts, smoke, reused, full, status in matrix:
        needs = {"reuse-plan": {"result": "success", "outputs": {"reused": reused}},
                 "e2e-plan": {"result": "success", "outputs": {"full": full}}}
        ctx = context(event, ref, {"fast-scripts": scripts, "journey-smoke-url": smoke}, needs, status)
        named_fast = []
        for job in ("fast", "fast-escalated"):
            name = ev(JOBS[job]["name"], ctx)
            if name == "Fast":
                assert ev(JOBS[job]["if"], ctx) is True, (job, event, scripts, smoke, reused, full)
                named_fast.append(job)
        runs_fast = bool(scripts) and not smoke and reused != "true"
        assert len(named_fast) == (1 if runs_fast else 0), (named_fast, event, scripts, smoke, reused, full)
        if runs_fast:
            seen_fast += 1
            escalated = event in ("pull_request", "pull_request_target") and full == "true"
            assert named_fast == (["fast-escalated"] if escalated else ["fast"])
    assert seen_fast
    print("PASS  exactly one running job is named Fast when enabled; none otherwise; escalation moves it")


def test_fast_escalated_reuses_the_required_gate() -> None:
    job = JOBS["fast-escalated"]
    assert job["steps"][-1] == step("required", GATE_STEP), "escalated Fast must run the Required gate step"
    referenced = set(re.findall(r"needs\.([a-z0-9-]+)\.", json.dumps(job["steps"][-1])))
    assert referenced <= set(job["needs"]), referenced - set(job["needs"])
    assert "fast" in job["needs"] and "required" not in job["needs"]
    assert "fast-escalated" in JOBS["required"]["needs"]
    print("PASS  escalated Fast is the Required gate step itself, with every lane it reads in needs")


def test_fast_scripts_step() -> None:
    script = step("fast", "Run fast scripts")["run"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bindir = root / "bin"
        bindir.mkdir()
        pnpm = bindir / "pnpm"
        pnpm.write_text('#!/bin/bash\necho "ran $2 base=${FAST_BASE_REF:-}" >> "$FAST_LOG"\n')
        pnpm.chmod(pnpm.stat().st_mode | stat.S_IEXEC)
        (root / "package.json").write_text(json.dumps({"name": "fixture", "scripts": {"lint": "x", "test:changed": "y"}}))
        log = root / "log"
        env = {"PATH": f"{bindir}:{os.environ['PATH']}", "PM": "pnpm", "FAST_LOG": str(log),
               "SCRIPTS": "lint test:changed", "FAST_BASE_REF": "HEAD^1"}
        result = run_bash(script, env, cwd=tmp)
        assert result.returncode == 0, result.stderr
        assert log.read_text().splitlines() == ["ran lint base=HEAD^1", "ran test:changed base=HEAD^1"]
        log.unlink()
        result = run_bash(script, {**env, "SCRIPTS": "lint missing"}, cwd=tmp)
        assert result.returncode == 1 and "no such script" in result.stdout
    event_expr = step("fast", "Run fast scripts")["env"]["FAST_BASE_REF"]
    assert ev(event_expr, context("pull_request")) == "HEAD^1"
    assert ev(event_expr, context("push")) == ""
    checkout = next(s for s in JOBS["fast"]["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 2
    print("PASS  fast scripts run in order with FAST_BASE_REF on PRs; a missing script fails")


def test_escalation_fails_closed_on_unknown_plan() -> None:
    script = step("fast", "Resolve protected-path escalation")["run"]
    ok = {"RUN_E2E": "true", "FULL_PATHS": "src/auth/**", "E2E_PLAN_RESULT": "success", "ESCALATED": "false"}
    assert run_bash(script, ok).returncode == 0
    for outcome in ("failure", "cancelled", "skipped", ""):
        assert run_bash(script, {**ok, "E2E_PLAN_RESULT": outcome}).returncode == 1, outcome
    assert run_bash(script, {**ok, "FULL_PATHS": "", "E2E_PLAN_RESULT": "failure"}).returncode == 0
    print("PASS  escalation fails closed when e2e-full-paths is set and the plan did not succeed")


def run_script(script: str, scenario: dict, inputs: dict, env_name: str,
               workflow: str = "example/app/.github/workflows/ci.yml@refs/heads/main") -> dict:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "scenario.json").write_text(json.dumps(scenario))
        (root / "script.js").write_text(script)
        result = subprocess.run(
            ["node", "-e", test_e2e_reuse.HARNESS, str(root / "scenario.json"), str(root / "script.js")],
            env={**os.environ, "CALLER_WORKFLOW_REF": workflow, env_name: json.dumps(inputs)},
            capture_output=True, text=True, check=True)
        return json.loads(result.stdout)


def test_required_reuse_lookup() -> None:
    lookup = next(s for s in JOBS["reuse-plan"]["steps"] if s.get("id") == "proof")
    script = lookup["with"]["script"]
    assert step("required", "Compute Required proof key")["with"]["script"] == script, "key script drifted"
    inputs = {**defaults(), "required-reuse-pr-results": True}
    pushed = run_script(script, {}, inputs, "PROOF_INPUTS")
    assert pushed["outputs"]["reused"] == "true"
    key = pushed["outputs"]["key"]
    assert key.startswith("required-proof-v1-") and len(key) == len("required-proof-v1-") + 64
    pr = run_script(script, {"event": "pull_request"}, inputs, "PROOF_INPUTS",
                    workflow="example/app/.github/workflows/ci.yml@refs/pull/1/merge")
    assert pr["outputs"] == {"reused": "false", "key": key} and pr["calls"] == []
    for name, scenario in {
        "missing proof": {"missing": True}, "failed source run": {"run": {"conclusion": "failure"}},
        "in-progress source run": {"run": {"status": "in_progress"}},
        "push source run": {"run": {"event": "push"}}, "fork proof": {"run": {"head_repository": {"id": 43}}},
        "merged tree changed": {"tree": "c" * 40, "proofKey": key}, "unreadable API": {"apiError": True},
    }.items():
        assert run_script(script, scenario, inputs, "PROOF_INPUTS")["outputs"]["reused"] == "false", name
    changed = run_script(script, {"proofKey": key}, {**inputs, "e2e-args": "--project=other"}, "PROOF_INPUTS")
    assert changed["outputs"]["reused"] == "false"
    # Proof is minted only from a full, same-repository PR run.
    mint_if = " ".join(step("required", "Compute Required proof key")["if"].split())
    for guard in ("success()", "inputs.required-reuse-pr-results", "github.event_name == 'pull_request'",
                  "github.event.pull_request.head.repo.full_name == github.repository",
                  "needs.build.result == 'success'", "needs.checks.result == 'success'",
                  "needs.e2e.result == 'success'", "needs.e2e-plan.outputs.e2e-args == inputs.e2e-args"):
        assert guard in mint_if, guard
    gated = ["build", "checks", "extra-gate", "e2e-plan"]
    for job in gated:
        assert "reuse-plan" in JOBS[job]["needs"]
        ctx = context("push", inputs={"required-reuse-pr-results": True, "extra-gate-scripts": "x", "run-e2e": True},
                      needs={"reuse-plan": {"result": "success", "outputs": {"reused": "true"}}})
        assert ev(JOBS[job]["if"], ctx) is False, job
        ctx["needs"]["reuse-plan"]["outputs"]["reused"] = "false"
        assert ev(JOBS[job]["if"], ctx) is True, job
    print("PASS  Required reuse: same key on PR and push; only a completed successful same-repo PR run is reused")


def test_e2e_proof_key_ignores_default_fast_path_inputs() -> None:
    script = next(s for s in JOBS["e2e-plan"]["steps"] if s.get("id") == "proof")["with"]["script"]
    legacy = {"e2e-args": "--project=web", "e2e-shards": 3}
    base = run_script(script, {}, legacy, "E2E_INPUTS")["outputs"]["key"]
    with_defaults = run_script(script, {}, {**legacy, **NEW_INPUTS}, "E2E_INPUTS")["outputs"]["key"]
    assert with_defaults == base, "default fast-path inputs changed the E2E proof key"
    for name, value in [("fast-scripts", "lint"), ("e2e-pr-only-changed", True), ("required-reuse-pr-results", True)]:
        other = run_script(script, {}, {**legacy, **NEW_INPUTS, name: value}, "E2E_INPUTS")["outputs"]["key"]
        assert other != base, name
    print("PASS  E2E proof keys minted before these inputs existed still match; non-defaults change the key")


def run_plan(event: str, full: str, only_changed: str, pr_args: str = "", args: str = "--project=web") -> dict:
    script = step("e2e-plan", "Compute shard list")["run"]
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp, "out")
        out.write_text("")
        env = {"TOTAL": "2", "PR_TOTAL": "0", "ARGS": args, "PR_ARGS": pr_args, "EVENT_NAME": event,
               "SKIPPED": "false", "FULL": full, "ONLY_CHANGED": only_changed, "GITHUB_OUTPUT": str(out)}
        result = run_bash(script, env)
        assert result.returncode == 0, result.stderr
        return dict(line.split("=", 1) for line in out.read_text().splitlines())


def test_only_changed() -> None:
    suffix = "--only-changed=HEAD^1 --pass-with-no-tests"
    assert run_plan("pull_request", "false", "true")["e2e-args"] == f"--project=web {suffix}"
    assert run_plan("pull_request", "", "true", pr_args="--project=pr")["e2e-args"] == f"--project=pr {suffix}"
    assert run_plan("pull_request", "false", "true", args="")["e2e-args"] == suffix
    for event, full, flag in [("pull_request", "true", "true"), ("push", "false", "true"),
                              ("pull_request_target", "false", "true"), ("schedule", "", "true"),
                              ("pull_request", "false", "false")]:
        assert suffix not in run_plan(event, full, flag)["e2e-args"], (event, full, flag)
    depth = JOBS["e2e"]["steps"][0]["with"]["fetch-depth"]
    assert ev(depth, context("pull_request", inputs={"e2e-pr-only-changed": True})) == 2
    assert ev(depth, context("push", inputs={"e2e-pr-only-changed": True})) == 1
    assert JOBS["e2e-quarantine"]["steps"] == JOBS["e2e"]["steps"]
    print("PASS  --only-changed applies to the PR tier only, and only the PR checkout deepens")


SMOKE_PNPM = r"""#!/bin/bash
# fixture Playwright wrapper: writes the JSON report the scenario asks for
case "$SMOKE_SCENARIO" in
  sleep) sleep 5; printf '%s' '{"stats":{"expected":0,"unexpected":1}}' > "$PLAYWRIGHT_JSON_OUTPUT_FILE"; exit 1 ;;
  noreport) exit 1 ;;
  *) printf '%s' "$SMOKE_REPORT" > "$PLAYWRIGHT_JSON_OUTPUT_FILE" ;;
esac
printf '%s\n' "$*" > "$SMOKE_ARGV"
exit "${SMOKE_RC:-0}"
"""


def run_smoke(scenario: str, report: dict | None, rc: int = 0, budget: str = "90") -> tuple[int, str, str]:
    script = step("journey-smoke", "Run journey smoke")["run"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bindir = root / "bin"
        bindir.mkdir()
        pnpm = bindir / "pnpm"
        pnpm.write_text(SMOKE_PNPM)
        pnpm.chmod(pnpm.stat().st_mode | stat.S_IEXEC)
        (root / "package.json").write_text(json.dumps({"name": "fixture", "scripts": {"test:e2e": "playwright test"}}))
        out = root / "out"
        out.write_text("")
        env = {"PATH": f"{bindir}:{os.environ['PATH']}", "PM": "pnpm", "SCRIPT": "test:e2e",
               "GATE": "Run journey smoke", "SMOKE_ARGS": "--grep=@smoke --retries=1",
               "PLAYWRIGHT_BASE_URL": "https://app.example", "BUDGET_SECONDS": budget,
               "RUNNER_TEMP": tmp, "GITHUB_OUTPUT": str(out), "SMOKE_SCENARIO": scenario,
               "SMOKE_REPORT": json.dumps(report or {}), "SMOKE_RC": str(rc), "SMOKE_ARGV": str(root / "argv")}
        result = run_bash(script, env, cwd=tmp)
        verdict = dict(line.split("=", 1) for line in out.read_text().splitlines()).get("verdict", "")
        argv = (root / "argv").read_text() if (root / "argv").exists() else ""
        return result.returncode, verdict, argv


def test_journey_smoke_verdicts() -> None:
    passed = {"stats": {"expected": 3, "unexpected": 0, "flaky": 1}, "errors": []}
    rc, verdict, argv = run_smoke("report", passed)
    assert (rc, verdict) == (0, "passed"), (rc, verdict)
    assert "--grep=@smoke --retries=1 --reporter=line,json" in argv, argv
    cases = [
        ("report", {"stats": {"expected": 2, "unexpected": 1}}, 1, "journey-failed"),
        ("report", {"stats": {"expected": 0, "unexpected": 0}}, 1, "inconclusive"),
        ("report", {"stats": {"expected": 0, "unexpected": 0}}, 0, "inconclusive"),
        ("report", {"stats": {"expected": 2, "unexpected": 0}, "errors": [{"message": "setup"}]}, 1, "inconclusive"),
        ("report", {"stats": {"expected": 2, "unexpected": 0}}, 2, "inconclusive"),
        ("noreport", None, 1, "inconclusive"),
    ]
    for scenario, report, code, expected in cases:
        rc, verdict, _ = run_smoke(scenario, report, code)
        assert rc == 1 and verdict == expected, (scenario, report, code, rc, verdict)
    # A budget overrun is inconclusive even when a (late) report says a journey
    # failed: the fixture would report journey-failed after 5 s, the budget is 1 s.
    rc, verdict, _ = run_smoke("sleep", None, 0, budget="1")
    assert rc == 1 and verdict == "inconclusive", (rc, verdict)
    rollback = step("journey-smoke", "Roll back (journey failed)")
    assert " ".join(rollback["if"].split()) == (
        "failure() && steps.smoke.outputs.verdict == 'journey-failed' && inputs.journey-smoke-rollback-script != ''")
    assert rollback["env"]["ROLLBACK_TOKEN"] == "${{ secrets.JOURNEY_SMOKE_ROLLBACK_TOKEN }}"
    holders = [(job_id, s.get("name")) for job_id, job in JOBS.items() for s in job.get("steps", [])
               if "JOURNEY_SMOKE_ROLLBACK_TOKEN" in json.dumps(s)]
    assert holders == [("journey-smoke", "Roll back (journey failed)")], holders
    print("PASS  journey smoke: only a journey that failed after retry reaches the rollback hook")


def test_journey_smoke_rollback_hook() -> None:
    script = step("journey-smoke", "Roll back (journey failed)")["run"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bindir = root / "bin"
        bindir.mkdir()
        pnpm = bindir / "pnpm"
        pnpm.write_text('#!/bin/bash\necho "$2 to=$ROLLBACK_TO_VERSION token=${ROLLBACK_TOKEN:+set}" > "$HOOK_LOG"\n')
        pnpm.chmod(pnpm.stat().st_mode | stat.S_IEXEC)
        (root / "package.json").write_text(json.dumps({"name": "f", "scripts": {"deploy:rollback": "x"}}))
        env = {"PATH": f"{bindir}:{os.environ['PATH']}", "PM": "pnpm", "SCRIPT": "deploy:rollback",
               "ROLLBACK_TO_VERSION": "v-123", "ROLLBACK_TOKEN": "t", "HOOK_LOG": str(root / "hook")}
        assert run_bash(script, env, cwd=tmp).returncode == 0
        assert (root / "hook").read_text().strip() == "deploy:rollback to=v-123 token=set"
        assert run_bash(script, {**env, "SCRIPT": "missing"}, cwd=tmp).returncode == 1
    print("PASS  rollback hook runs the named script with its version and token; a missing script fails")


def test_journey_smoke_validation() -> None:
    script = step("journey-smoke", "Validate journey smoke configuration")["run"]
    ok = {"SMOKE_URL": "https://app.example.com/", "SMOKE_ARGS": "--grep=@smoke --retries=1",
          "SCRIPT": "test:e2e", "ROLLBACK_SCRIPT": "deploy:rollback", "ROLLBACK_TO": "abc-123.4"}
    assert run_bash(script, ok).returncode == 0
    for override in ({"SMOKE_URL": "http://app.example.com"}, {"SMOKE_URL": "https://a b"},
                     {"SMOKE_URL": "https://app.example.com\nx"}, {"SMOKE_ARGS": ""},
                     {"SMOKE_ARGS": "--grep=@smoke\n--x"}, {"SCRIPT": ""},
                     {"ROLLBACK_SCRIPT": "rm -rf /"}, {"ROLLBACK_SCRIPT": "$(touch x)"},
                     {"ROLLBACK_TO": "v1; rm"}):
        assert run_bash(script, {**ok, **override}).returncode == 1, override
    assert run_bash(script, {**ok, "ROLLBACK_SCRIPT": "", "ROLLBACK_TO": ""}).returncode == 0
    print("PASS  journey smoke configuration is validated before anything installs")


def main() -> None:
    test_new_inputs_default_off()
    test_defaults_leave_every_lane_as_before()
    test_required_gate_reuse_and_smoke_branches()
    test_required_fast_path_step()
    test_fast_naming_never_skips_a_fast_check()
    test_fast_escalated_reuses_the_required_gate()
    test_fast_scripts_step()
    test_escalation_fails_closed_on_unknown_plan()
    test_required_reuse_lookup()
    test_e2e_proof_key_ignores_default_fast_path_inputs()
    test_only_changed()
    test_journey_smoke_verdicts()
    test_journey_smoke_rollback_hook()
    test_journey_smoke_validation()
    print("fast-path contract passed")


if __name__ == "__main__":
    main()
