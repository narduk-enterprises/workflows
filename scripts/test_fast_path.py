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

import functools
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
from test_runner_default import Evaluator, loose_eq, to_num, truthy  # noqa: E402

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


# ---------------------------------------------------------------------------
# Job-graph simulation with GitHub's skip propagation.
#
# A job whose `if:` names no status function gets an implicit `success()`,
# and at job level that is false when ANY ancestor in the needs chain was
# skipped or failed, not only a direct `needs:` entry: "a failure or skip
# applies to all jobs in the dependency chain from the point of failure or
# skip onwards" (the workflow-syntax docs for jobs.<job_id>.needs, reproduced
# in actions/runner#2205). Evaluating each `if:` against hand-fed `needs`
# results cannot see that, and it once let a skipped `Reuse plan` silently skip
# `E2E`, `Preview` and `Deploy dry run` for every default caller, turning
# `Required` red. These helpers propagate results through the real graph.
# ---------------------------------------------------------------------------
GRAPH_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<op>\|\||&&|==|!=|>=|<=|>|<|!|\(|\)|,)"
    r"|(?P<num>-?\d+(?:\.\d+)?)|(?P<id>[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*))")
STATUS_CALL = re.compile(r"\b(?:always|success|failure|cancelled)\s*\(")
TEMPLATE = re.compile(r"\$\{\{(.*?)\}\}", re.S)


@functools.lru_cache(maxsize=None)
def graph_tokens(src: str) -> tuple:
    out, pos, src = [], 0, src.strip()
    while pos < len(src):
        m = GRAPH_TOKEN.match(src, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot tokenize at {src[pos:pos + 30]!r}")
        out.append((m.lastgroup, m.group(m.lastgroup)))
        pos = m.end()
    return tuple(out)


class GraphEvaluator(Evaluator):
    """Job-level evaluation: status functions over the job's ANCESTORS
    (ctx["__job_status"]) plus the numeric comparison `E2E report` uses."""

    def run(self, src: str):
        self.toks, self.i = list(graph_tokens(src)), 0
        value = self.or_()
        assert self.i == len(self.toks), f"trailing tokens in {src!r}"
        return value

    def cmp(self):
        left = self.unary()
        op = self.peek()[1]
        if op not in ("==", "!=", ">", "<", ">=", "<="):
            return left
        self.i += 1
        right = self.unary()
        if op in ("==", "!="):
            return loose_eq(left, right) == (op == "==")
        a, b = to_num(left), to_num(right)
        return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]

    def call(self, name, args):  # type: ignore[override]
        if name in ("always", "success", "failure", "cancelled"):
            return self.ctx["__job_status"][name]
        return Evaluator.call(name, args)


def gh_str(value) -> str:
    """How GitHub renders an expression value into env or a script."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def render(value, ctx: dict):
    """Evaluate a whole `${{ }}` value, or interpolate templates in a string."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("${{") and text.endswith("}}") and text.count("${{") == 1:
        return GraphEvaluator(ctx).run(text[3:-2])
    return TEMPLATE.sub(lambda m: gh_str(GraphEvaluator(ctx).run(m.group(1))), value)


def job_condition(job: dict) -> str:
    cond = job.get("if")
    if cond is None:
        return "success()"
    text = str(cond).strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    return text if STATUS_CALL.search(text) else f"success() && ({text})"


def needs_of(job: dict) -> list:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def simulate(jobs: dict, inputs: dict, event: str, ref: str = "refs/heads/main",
             outputs: dict | None = None, results: dict | None = None,
             cancelled: bool = False) -> dict:
    """{job: {"result", "outputs", "ctx"}} for one run of `jobs`. A job whose
    `if:` holds gets results.get(job, "success") and outputs.get(job, {}); any
    other job is "skipped" with no outputs, exactly as GitHub reports it."""
    outputs, results = outputs or {}, results or {}
    base = context(event, ref, inputs)
    state: dict = {}
    ancestors: dict = {}

    def settle(job_id: str) -> None:
        if job_id in state:
            return
        job = jobs[job_id]
        direct = needs_of(job)
        for dep in direct:
            settle(dep)
        ancestors[job_id] = set(direct).union(*(ancestors[dep] for dep in direct))
        upstream = [state[a]["result"] for a in ancestors[job_id]]
        ctx = {**base, "needs": {dep: {"result": state[dep]["result"], "outputs": state[dep]["outputs"]}
                                 for dep in direct}}
        ctx["__job_status"] = {"always": True, "cancelled": cancelled,
                               "success": not cancelled and all(r == "success" for r in upstream),
                               "failure": any(r == "failure" for r in upstream)}
        runs = truthy(GraphEvaluator(ctx).run(job_condition(job)))
        state[job_id] = {"result": results.get(job_id, "success") if runs else "skipped",
                         "outputs": dict(outputs.get(job_id, {})) if runs else {}, "ctx": ctx}

    for job_id in jobs:
        settle(job_id)
    return state


def run_gates(job_id: str, state: dict) -> list:
    """Run every unconditional `run:` step of `job_id` (the Required gate
    steps) with env rendered from the simulated `needs`; [(name, rc, out)]."""
    ctx = state[job_id]["ctx"]
    ran = []
    for s in JOBS[job_id]["steps"]:
        if "run" not in s or "if" in s:
            continue
        env = {key: gh_str(render(value, ctx)) for key, value in (s.get("env") or {}).items()}
        result = run_bash(render(s["run"], ctx), env)
        ran.append((s["name"], result.returncode, result.stdout + result.stderr))
    return ran


# origin/main (67968e3) job graph, verbatim: the baseline every default
# caller must keep.
OLD_GRAPH = {
    "build": {},
    "checks": {},
    "extra-gate": {"if": "inputs.extra-gate-scripts != ''"},
    "e2e-plan": {"if": "inputs.run-e2e"},
    "e2e": {"needs": ["build", "e2e-plan"],
            "if": "inputs.run-e2e && needs.e2e-plan.outputs.skipped != 'true'"},
    "e2e-quarantine": {"needs": ["build", "e2e-plan"],
                       "if": "inputs.run-e2e && inputs.e2e-quarantine-args != '' && needs.e2e-plan.outputs.skipped != 'true'"},
    "e2e-report": {"needs": ["e2e-plan", "e2e"],
                   "if": "!cancelled() && inputs.run-e2e && needs.e2e-plan.outputs.shard-total > 1 && needs.e2e-plan.result == 'success' && needs.e2e-plan.outputs.skipped != 'true'"},
    "preview": {"needs": "build",
                "if": "inputs.preview-checks != 'none' && (github.event_name == 'pull_request' || github.event_name == 'pull_request_target')"},
    "deploy-dry-run": {"needs": "build", "if": "inputs.wrangler-dry-run"},
    "caller-lint": {},
    "required": {"needs": ["build", "checks", "extra-gate", "e2e-plan", "e2e", "e2e-report", "preview",
                           "deploy-dry-run", "caller-lint"], "if": "always()"},
}
NEW_JOBS = {"reuse-plan", "fast", "fast-escalated", "journey-smoke"}
CI_LANES = ["build", "checks", "extra-gate", "e2e-plan", "e2e", "e2e-quarantine", "e2e-report",
            "preview", "deploy-dry-run"]


def plan_outputs(skipped: str = "false", shards: str = "1", full: str = "false") -> dict:
    return {"e2e-plan": {"skipped": skipped, "shard-total": shards, "full": full, "e2e-args": ""}}


def test_simulator_models_skip_propagation() -> None:
    # actions/runner#2205: a skipped job, then an always() job that runs, then
    # a default-`if` job, which GitHub skips; an explicit guard runs.
    graph = {"a": {"if": "false"}, "b": {"needs": ["a"], "if": "always()"}, "c": {"needs": ["b"]},
             "d": {"needs": ["b"], "if": "!cancelled() && needs.b.result == 'success'"}}
    state = simulate(graph, {}, "push")
    assert [state[j]["result"] for j in "abcd"] == ["skipped", "success", "skipped", "success"], state
    print("PASS  simulator: a skip anywhere upstream skips every default-`if` job below it")


def test_default_graph_matches_origin_main() -> None:
    """Every fast-path input at its default: each pre-existing job must run,
    skip or fail exactly as on origin/main, for every event, lane input, one
    failing lane and a cancelled run; every new job must be skipped."""
    assert set(JOBS) == set(OLD_GRAPH) | NEW_JOBS, set(JOBS) ^ (set(OLD_GRAPH) | NEW_JOBS)
    faults = [(None, False), (None, True)] + [
        (job, False) for job in ("build", "checks", "extra-gate", "e2e-plan", "e2e", "preview", "caller-lint")]
    cases = 0
    for (event, ref), run_e2e, dry_run, preview, extra, quarantine, skipped, shards in itertools.product(
            EVENTS, [True, False], [True, False], ["og", "none"], ["", "lint"], ["", "--grep=@quarantine"],
            ["false", "true"], ["1", "2"]):
        inputs = {"run-e2e": run_e2e, "wrangler-dry-run": dry_run, "preview-checks": preview,
                  "extra-gate-scripts": extra, "e2e-quarantine-args": quarantine}
        outs = plan_outputs(skipped, shards)
        for failing, cancelled in faults:
            results = {failing: "failure"} if failing else {}
            new = simulate(JOBS, inputs, event, ref, outs, results, cancelled)
            old = simulate(OLD_GRAPH, inputs, event, ref, outs, results, cancelled)
            for job in OLD_GRAPH:
                assert new[job]["result"] == old[job]["result"], (
                    job, new[job]["result"], old[job]["result"], event, inputs, skipped, shards, failing, cancelled)
            for job in NEW_JOBS:
                assert new[job]["result"] == "skipped", (job, event, inputs)
            cases += 1
    print(f"PASS  defaults: every pre-existing job runs/skips as on origin/main with skip propagation ({cases} runs)")


def test_default_callers_required_passes_on_the_real_graph() -> None:
    """End to end for a default caller: simulate the graph, then run
    `Required`'s own gate steps on the results. A green run must be green.
    `Required` tells events apart only as pull request or not, so one push
    stands for schedule and manual runs (each gate step is a bash process)."""
    runs = 0
    events = [("pull_request", "refs/pull/1/merge"), ("pull_request_target", "refs/heads/main"),
              ("push", "refs/heads/main")]
    for (event, ref), run_e2e, dry_run, preview, extra, skipped, shards in itertools.product(
            events, [True, False], [True, False], ["og", "none"], ["", "lint"], ["false", "true"], ["1", "2"]):
        inputs = {"run-e2e": run_e2e, "wrangler-dry-run": dry_run, "preview-checks": preview,
                  "extra-gate-scripts": extra}
        state = simulate(JOBS, inputs, event, ref, plan_outputs(skipped, shards))
        gates = run_gates("required", state)
        assert [name for name, _, _ in gates] == [GATE_STEP, FAST_STEP], gates
        for name, code, out in gates:
            assert code == 0, (name, event, inputs, skipped, shards, out)
        runs += 1
    # A real failure still fails.
    state = simulate(JOBS, {"run-e2e": True}, "pull_request", "refs/pull/1/merge", plan_outputs(),
                     {"e2e": "failure"})
    assert run_gates("required", state)[0][1] == 1
    print(f"PASS  default callers: Required passes on the simulated graph ({runs} runs); a failed E2E fails it")


def test_enabled_modes_on_the_real_graph() -> None:
    full = {"run-e2e": True, "wrangler-dry-run": True, "extra-gate-scripts": "lint",
            "e2e-quarantine-args": "--grep=@quarantine", "e2e-shards": 2, "preview-checks": "og",
            "fast-scripts": "lint"}

    def green(job_id: str, state: dict) -> None:
        for name, code, out in run_gates(job_id, state):
            assert code == 0, (job_id, name, out)

    # Required reuse on a default-branch push: a proven tree skips every lane.
    reuse = {**full, "required-reuse-pr-results": True}
    state = simulate(JOBS, reuse, "push", "refs/heads/main", {"reuse-plan": {"reused": "true"}, **plan_outputs("false", "2")})
    assert {j: state[j]["result"] for j in CI_LANES + ["fast", "fast-escalated", "journey-smoke"]} == dict.fromkeys(
        CI_LANES + ["fast", "fast-escalated", "journey-smoke"], "skipped")
    assert state["reuse-plan"]["result"] == state["caller-lint"]["result"] == "success"
    green("required", state)
    # No proof: the full gate runs, as before.
    state = simulate(JOBS, reuse, "push", "refs/heads/main", {"reuse-plan": {"reused": "false"}, **plan_outputs("false", "2")})
    ran = {j for j in JOBS if state[j]["result"] == "success"}
    assert ran == set(CI_LANES) - {"preview"} | {"reuse-plan", "caller-lint", "fast", "required"}, ran
    green("required", state)
    # Journey-smoke mode: every CI lane skipped, the smoke runs.
    smoke = {**full, "journey-smoke-url": "https://app.example"}
    state = simulate(JOBS, smoke, "workflow_dispatch", "refs/heads/main", plan_outputs())
    ran = {j for j in JOBS if state[j]["result"] == "success"}
    assert ran == {"journey-smoke", "caller-lint", "required"}, ran
    green("required", state)
    # ci / Fast on a pull request without a protected path.
    state = simulate(JOBS, full, "pull_request", "refs/pull/1/merge", plan_outputs("false", "2", "false"))
    assert state["fast"]["result"] == "success" and state["fast-escalated"]["result"] == "skipped"
    assert render(JOBS["fast"]["name"], state["fast"]["ctx"]) == "Fast"
    assert all(state[j]["result"] == "success" for j in CI_LANES), state
    green("required", state)
    # A protected path: `fast-escalated` becomes `Fast` and runs the full gate.
    state = simulate(JOBS, full, "pull_request", "refs/pull/1/merge", plan_outputs("false", "2", "true"))
    assert state["fast"]["result"] == state["fast-escalated"]["result"] == "success"
    assert render(JOBS["fast"]["name"], state["fast"]["ctx"]) == "Fast lanes (escalated)"
    assert render(JOBS["fast-escalated"]["name"], state["fast-escalated"]["ctx"]) == "Fast"
    green("fast-escalated", state)
    green("required", state)
    state = simulate(JOBS, full, "pull_request", "refs/pull/1/merge", plan_outputs("false", "2", "true"),
                     {"e2e": "failure"})
    assert any(code == 1 for _, code, _ in run_gates("fast-escalated", state))
    print("PASS  enabled modes on the simulated graph: reuse, journey smoke, Fast and escalated Fast")


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
        # GitHub shows a skipped job's raw name template today; this is the
        # name if it ever evaluates it (test_skipped_fast_jobs_never_show_fast).
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
    # The gate runs before the escalation publish, so a check-run update
    # failure cannot skip the full-suite proof. The gate step itself is the
    # Required step, not a copy.
    gate = job["steps"][-2]
    assert gate == step("required", GATE_STEP), "escalated Fast must run the Required gate step"
    assert job["steps"][-1].get("name") == "Publish Fast escalation"
    referenced = set(re.findall(r"needs\.([a-z0-9-]+)\.", json.dumps(gate)))
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


def test_fast_escalation_is_published_on_the_check_named_fast() -> None:
    """The check name stays Fast. Escalation is a job output, a summary line,
    and the check run's output title and summary. Both jobs that can hold the
    name run the same script and request checks: write."""
    for job in ("fast", "fast-escalated"):
        assert JOBS[job]["outputs"]["escalated"] == "${{ steps.escalation.outputs.escalated }}"
        assert JOBS[job]["permissions"]["checks"] == "write"
        publish = step(job, "Publish Fast escalation")
        assert publish["id"] == "escalation"
        assert publish["if"] == "success()"
        assert "name" not in publish["run"] or "name` is omitted" in publish["run"]
    assert step("fast", "Publish Fast escalation")["run"] == step("fast-escalated", "Publish Fast escalation")["run"]
    # The name expressions are unchanged: escalation moves the name, it does not add one.
    assert "|| 'Fast'" in JOBS["fast"]["name"]
    assert "&& 'Fast' ||" in JOBS["fast-escalated"]["name"]
    script = step("fast", "Publish Fast escalation")["run"]
    assert '"name"' not in script  # the patch body must not rename the check
    cases = [("true", "Fast escalated", "escalated: true"), ("false", "Fast", "escalated: false")]
    for escalated, title, first_line in cases:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            args = root / "args"
            body = root / "body"
            stub = bindir / "gh"
            stub.write_text(
                "#!/bin/bash\nset -euo pipefail\n"
                f'printf "%s\\n" "$@" > {json.dumps(str(args))}\n'
                f'cat > {json.dumps(str(body))}\n'
            )
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
            output = root / "out"
            summary = root / "summary"
            env = {
                "PATH": f"{bindir}:/usr/bin:/bin",
                "ESCALATED": escalated,
                "CHECK_RUN_ID": "107725045234",
                "GITHUB_REPOSITORY": "narduk-enterprises/example",
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(summary),
                "GH_TOKEN": "fake",
            }
            result = run_bash(script, env)
            assert result.returncode == 0, (escalated, result.stdout, result.stderr)
            assert output.read_text().strip() == f"escalated={escalated}"
            assert summary.read_text().splitlines()[2] == first_line
            recorded = args.read_text().splitlines()
            assert "--method" in recorded and "PATCH" in recorded
            assert "repos/narduk-enterprises/example/check-runs/107725045234" in recorded
            payload = json.loads(body.read_text())
            assert set(payload) == {"output"}
            assert payload["output"]["title"] == title
            assert payload["output"]["summary"].splitlines()[0] == first_line
            assert "name" not in payload
    refused = run_bash(script, {"ESCALATED": "true", "CHECK_RUN_ID": "not-a-number",
                                "GITHUB_REPOSITORY": "narduk-enterprises/example", "GH_TOKEN": "fake"})
    assert refused.returncode == 1 and "not numeric" in refused.stdout
    print("PASS  Fast publishes escalated on the job output, the step summary and the check-run title/summary")


def test_escalation_fails_closed_on_unknown_plan() -> None:
    script = step("fast", "Resolve protected-path escalation")["run"]
    ok = {"RUN_E2E": "true", "FULL_PATHS": "src/auth/**", "E2E_PLAN_RESULT": "success", "ESCALATED": "false"}
    assert run_bash(script, ok).returncode == 0
    for outcome in ("failure", "cancelled", "skipped", ""):
        assert run_bash(script, {**ok, "E2E_PLAN_RESULT": outcome}).returncode == 1, outcome
    assert run_bash(script, {**ok, "FULL_PATHS": "", "E2E_PLAN_RESULT": "failure"}).returncode == 0
    # run-e2e false: `E2E plan` never runs, so a protected path could never
    # escalate and `Fast` would pass on lint/unit alone. Fail closed instead.
    for outcome in ("skipped", "success"):
        result = run_bash(script, {**ok, "RUN_E2E": "false", "E2E_PLAN_RESULT": outcome})
        assert result.returncode == 1 and "run-e2e is false" in result.stdout, (outcome, result.stdout)
    assert run_bash(script, {**ok, "RUN_E2E": "false", "FULL_PATHS": "", "E2E_PLAN_RESULT": "skipped"}).returncode == 0
    print("PASS  escalation fails closed when e2e-full-paths is set and the plan did not succeed or cannot run")


def test_skipped_fast_jobs_never_show_fast() -> None:
    """GitHub does not evaluate the `name:` of a job it skips (community
    discussions #13261 and #152293): the check shows the raw template. So the
    two computed names must never BE `Fast` as raw text, and every other job
    name must be static and not `Fast`. test_fast_naming_never_skips_a_fast_check
    covers the other case, a GitHub that does evaluate skipped names."""
    for job in ("fast", "fast-escalated"):
        raw = JOBS[job]["name"].strip()
        assert raw.startswith("${{") and raw.endswith("}}") and raw != "Fast", (job, raw)
    for job_id, job in JOBS.items():
        if job_id in ("fast", "fast-escalated"):
            continue
        assert "${{" not in job["name"] and job["name"] != "Fast", (job_id, job["name"])
    print("PASS  a skipped fast job shows its raw template, never `Fast`; no other job can be named Fast")


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
    test_simulator_models_skip_propagation()
    test_defaults_leave_every_lane_as_before()
    test_default_graph_matches_origin_main()
    test_default_callers_required_passes_on_the_real_graph()
    test_enabled_modes_on_the_real_graph()
    test_skipped_fast_jobs_never_show_fast()
    test_required_gate_reuse_and_smoke_branches()
    test_required_fast_path_step()
    test_fast_naming_never_skips_a_fast_check()
    test_fast_escalated_reuses_the_required_gate()
    test_fast_scripts_step()
    test_fast_escalation_is_published_on_the_check_named_fast()
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
