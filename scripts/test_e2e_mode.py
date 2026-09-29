#!/usr/bin/env python3
"""Contract tests for taking E2E off the pull request (fast-CI program, 2026-09-29).

Two additions to nuxt-cloudflare.yml, both proven by EVALUATING the shipped job
conditions and names over the real job graph (test_fast_path.py's simulator) and
by executing the shipped gate text:

  1. The org switch. `vars.CI_E2E_IN_CI == 'false'` turns `E2E plan`, `E2E` and
     `E2E quarantine` off in an ordinary CI run on ANY event, and `Required`
     reads that skip as success. Unset, empty or any other value changes
     nothing. Two things deliberately outlive the switch, because both are
     promises to run browsers that a variable must not be able to break:
     `e2e-full-paths` (a protected-path pull request escalates to the full
     suite, which needs `E2E plan` to decide) and `expected-candidate-sha`
     (explicit release validation "cannot skip configured browser coverage").

  2. `mode: e2e`, the post-merge / nightly run. Only Build -> E2E plan -> E2E
     (and the quarantine lane) run, `Required` is the verdict, every other lane
     is skipped, and the variable is ignored. Asking for the mode without
     `run-e2e`, with journey-smoke-url, or with an unknown mode is a red
     verdict, never a green run that tested nothing. The mode never path-skips
     (a newer push cancels the run, so no diff can prove a skip), and its proof
     key equals the pull-request run's so a reused proof is still honoured.

Run: python3 scripts/test_e2e_mode.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fast_path as tfp  # noqa: E402
import test_e2e_skip_paths as skip  # noqa: E402

JOBS = tfp.JOBS
INPUTS = tfp.INPUTS
EVENTS = tfp.EVENTS
E2E_LANES = ("e2e-plan", "e2e", "e2e-quarantine")
NON_E2E_LANES = ("reuse-plan", "checks", "extra-gate", "preview", "deploy-dry-run", "fast",
                 "fast-escalable", "fast-escalated", "journey-smoke")

_real_context = tfp.context


def context_with_vars(event, ref="refs/heads/main", inputs=None, needs=None, status="success",
                      variables=None):
    ctx = _real_context(event, ref, inputs, needs, status)
    ctx["vars"] = dict(variables or {})
    return ctx


CURRENT_VARS: dict = {}


def simulate(inputs: dict, event: str, ref: str, variables: dict | None = None,
             outputs: dict | None = None, results: dict | None = None) -> dict:
    """The real graph with a repo/org variable set (tfp.context has no `vars`)."""
    global CURRENT_VARS
    CURRENT_VARS = variables or {}
    tfp.context = lambda e, r="refs/heads/main", i=None, n=None, s="success": context_with_vars(
        e, r, i, n, s, CURRENT_VARS)
    try:
        return tfp.simulate(JOBS, inputs, event, ref, outputs, results)
    finally:
        tfp.context = _real_context


def gates(state: dict) -> list:
    global CURRENT_VARS
    tfp.context = _real_context
    return tfp.run_gates("required", state)


def plan(skipped: str = "false", shards: str = "1") -> dict:
    return tfp.plan_outputs(skipped, shards)


PR_EVENTS = [(e, r) for e, r in EVENTS if e in ("pull_request", "pull_request_target")]
OTHER_EVENTS = [(e, r) for e, r in EVENTS if e not in ("pull_request", "pull_request_target")]
BASE = {"run-e2e": True, "e2e-quarantine-args": "--grep=@flaky"}


def ran(state: dict) -> set[str]:
    return {job for job, value in state.items() if value["result"] != "skipped"}


def assert_required_passes(state: dict, label: str) -> None:
    for name, code, out in gates(state):
        assert code == 0, (label, name, out)


def test_mode_input() -> None:
    spec = INPUTS["mode"]
    assert spec["default"] == "ci" and spec["type"] == "string" and spec["required"] is False, spec
    print("PASS  mode input: string, default 'ci', optional")


def test_switch_defaults_change_nothing() -> None:
    """Unset, empty or any value but 'false' is today's behaviour, on every event."""
    for variables in ({}, {"CI_E2E_IN_CI": ""}, {"CI_E2E_IN_CI": "true"}, {"CI_E2E_IN_CI": "0"},
                      {"CI_E2E_IN_CI": "no"}):
        for event, ref in EVENTS:
            state = simulate(BASE, event, ref, variables, plan())
            assert {"build", "e2e-plan", "e2e", "e2e-quarantine", "required"} <= ran(state), (variables, event, ran(state))
            assert_required_passes(state, f"{variables} {event}")
    print("PASS  CI_E2E_IN_CI unset / empty / true / other values: E2E runs on every event, as before")


def test_switch_off_skips_the_e2e_lanes() -> None:
    off = {"CI_E2E_IN_CI": "false"}
    for event, ref in EVENTS:
        state = simulate(BASE, event, ref, off, plan())
        for lane in E2E_LANES:
            assert state[lane]["result"] == "skipped", (event, lane)
        assert state["build"]["result"] == "success" and state["required"]["result"] == "success", event
        assert_required_passes(state, f"off {event}")
        # The skip is not a waiver: an E2E lane that ran anyway fails Required,
        # and so does a failed Build.
        for lane, result in (("e2e", "success"), ("e2e-plan", "success")):
            bad = tfp.with_need(state, "required", lane, result)
            assert gates(bad)[0][1] == 1, (event, lane)
        assert gates(tfp.with_need(state, "required", "build", "failure"))[0][1] == 1, event
        # Case-insensitive like every GitHub string comparison.
        upper = simulate(BASE, event, ref, {"CI_E2E_IN_CI": "False"}, plan())
        assert upper["e2e"]["result"] == "skipped", event
    print("PASS  CI_E2E_IN_CI=false: E2E plan, E2E and quarantine skip on every event; Required passes and still catches a stray lane")


def test_switch_leaves_promises_to_run_browsers() -> None:
    off = {"CI_E2E_IN_CI": "false"}
    promises = {
        "e2e-full-paths": {"e2e-full-paths": "**/*auth*/**"},
        "expected-candidate-sha": {"expected-candidate-sha": "a" * 40},
    }
    for label, extra in promises.items():
        for event, ref in EVENTS:
            state = simulate({**BASE, **extra}, event, ref, off, plan())
            assert {"e2e-plan", "e2e"} <= ran(state), (label, event)
    # And the required gate reads it the same way: the lanes are enabled.
    state = simulate({**BASE, "e2e-full-paths": "**/*auth*/**"}, "pull_request", "refs/pull/1/merge", off, plan())
    assert state["required"]["ctx"]["needs"]["e2e"]["result"] == "success"
    assert_required_passes(state, "full-paths")
    print("PASS  the switch never overrides e2e-full-paths (protected-path escalation) or expected-candidate-sha")


def test_switch_off_without_run_e2e_is_unchanged() -> None:
    for variables in ({}, {"CI_E2E_IN_CI": "false"}):
        state = simulate({}, "pull_request", "refs/pull/1/merge", variables, plan())
        assert not (set(E2E_LANES) & ran(state)), variables
        assert_required_passes(state, f"no e2e {variables}")
    print("PASS  a caller without run-e2e is unaffected by the variable")


def test_fold_reads_the_effective_switch() -> None:
    """With checks-in-build and E2E off, Build is the only lane, so it becomes
    `Required` and the aggregator steps aside (the folded gate). Any E2E lane
    that is really on, or mode e2e, keeps the aggregator."""
    folded = {**tfp.FOLD_INPUTS, "run-e2e": True}
    cases = [
        ({"CI_E2E_IN_CI": "false"}, folded, "Required", "skipped"),
        ({}, folded, "Build", "success"),
        ({"CI_E2E_IN_CI": "true"}, folded, "Build", "success"),
        ({"CI_E2E_IN_CI": "false"}, {**folded, "e2e-full-paths": "**/auth/**"}, "Build", "success"),
        ({"CI_E2E_IN_CI": "false"}, {**folded, "mode": "e2e"}, "Build", "success"),
        ({"CI_E2E_IN_CI": "false"}, {**tfp.FOLD_INPUTS, "mode": "e2e"}, "Build", "success"),
    ]
    for variables, inputs, build_name, required_result in cases:
        state = simulate(inputs, "pull_request", "refs/pull/1/merge", variables, plan())
        ctx = {**state["build"]["ctx"], "vars": variables}
        tfp.context = _real_context
        name = tfp.render(JOBS["build"]["name"], ctx)
        assert name == build_name, (variables, inputs, name)
        assert state["required"]["result"] == required_result, (variables, inputs, state["required"]["result"])
        # Exactly one job reports the check named Required.
        required_name = tfp.render(JOBS["required"]["name"], {**state["required"]["ctx"], "vars": variables})
        reporters = [n for n, jn in (("build", name), ("required", required_name))
                     if jn == "Required" and state[n]["result"] != "skipped"]
        if build_name == "Required":
            assert reporters == ["build"], (variables, inputs, reporters)
        else:
            assert reporters == ["required"], (variables, inputs, reporters)
    print("PASS  the folded Required gate follows the effective switch and never folds in mode e2e")


def test_mode_e2e_runs_only_the_e2e_chain() -> None:
    loud = {**BASE, "mode": "e2e", "extra-gate-scripts": "check:extra", "fast-scripts": "fast",
            "preview-checks": "og", "wrangler-dry-run": True, "required-reuse-pr-results": True,
            "checks-in-build": False}
    for variables in ({}, {"CI_E2E_IN_CI": "false"}, {"CI_E2E_IN_CI": "true"}):
        for event, ref in EVENTS:
            state = simulate(loud, event, ref, variables, plan())
            assert ran(state) == {"build", "e2e-plan", "e2e", "e2e-quarantine", "required"}, (variables, event, ran(state))
            assert_required_passes(state, f"mode e2e {variables} {event}")
    # Escalation inputs cannot wake a fast lane in this mode either.
    escalating = {**loud, "e2e-full-paths": "**/auth/**"}
    for event, ref in EVENTS:
        assert ran(simulate(escalating, event, ref, {}, plan())) == \
            {"build", "e2e-plan", "e2e", "e2e-quarantine", "required"}, event
    print("PASS  mode e2e: only Build, E2E plan, E2E (+quarantine) and Required run, on every event, whatever the variable says")


def test_mode_e2e_verdict() -> None:
    inputs = {**BASE, "mode": "e2e"}
    state = simulate(inputs, "push", "refs/heads/main", {}, plan())
    for lane, result in (("build", "failure"), ("e2e", "failure"), ("e2e-plan", "failure"),
                         ("e2e", "skipped"), ("e2e-plan", "skipped"), ("e2e", "cancelled")):
        assert gates(tfp.with_need(state, "required", lane, result))[0][1] == 1, (lane, result)
    # A lane that must be skipped but ran fails the verdict.
    for lane in ("checks", "extra-gate", "preview", "deploy-dry-run"):
        assert gates(tfp.with_need(state, "required", lane, "success"))[0][1] == 1, lane
    for lane in ("reuse-plan", "fast", "fast-escalable", "fast-escalated", "journey-smoke"):
        results = [r for r in (gates(tfp.with_need(state, "required", lane, "success"))) if r[1] == 1]
        assert results, lane
    # A reused PR proof skips E2E: still a pass, and E2E must then be skipped.
    proven = simulate(inputs, "push", "refs/heads/main", {}, plan("true"))
    assert proven["e2e"]["result"] == "skipped"
    assert_required_passes(proven, "proof reuse")
    assert gates(tfp.with_need(proven, "required", "e2e", "success"))[0][1] == 1
    # The verdict cannot pass a run that tested nothing.
    for bad, expect in (({"run-e2e": False, "mode": "e2e"}, "requires run-e2e"),
                        ({**inputs, "journey-smoke-url": "https://example.test"}, "journey-smoke-url"),
                        ({**inputs, "mode": "nightly"}, "mode must be")):
        broken = simulate(bad, "push", "refs/heads/main", {}, plan())
        out = " ".join(o for _, code, o in gates(broken) if code)
        assert any(code == 1 for _, code, _ in gates(broken)), bad
        assert expect in out, (bad, out)
    zero = simulate(inputs, "push", "refs/heads/main", {}, plan(shards="0"))
    assert gates(zero)[0][1] == 1
    print("PASS  mode e2e verdict: red for any failed/skipped/cancelled E2E lane, a stray lane, no run-e2e, a bad mode or zero shards")


def test_mode_e2e_skips_caller_lint_and_lint_steps() -> None:
    required = {s.get("name"): s for s in JOBS["required"]["steps"]}
    lint = ["Check out the caller for Caller lint", "Ensure PyYAML is available", "Install actionlint",
            "actionlint (caller's own workflows)",
            "Caller workflow hygiene audit (concurrency, timeouts, SHA pins, permissions)",
            "Verify exact validation candidate"]
    for mode, want in (("ci", True), ("e2e", False)):
        ctx = tfp.context("push", inputs={"mode": mode, "expected-candidate-sha": "a" * 40})
        for name in lint:
            assert tfp.ev(tfp.job_condition(required[name]), ctx) is want, (mode, name)
    build = {s.get("name"): s for s in JOBS["build"]["steps"]}
    for name in ("Dependency audit", "Typecheck Worker", "Typecheck Nuxt", "Unit tests", "Extra scripts",
                 "Start concurrent scripts"):
        for mode, want in (("ci", True), ("e2e", False)):
            ctx = tfp.context("push", inputs={"mode": mode, "dependency-audit": True, "checks-in-build": True,
                                              "run-tests": True, "extra-scripts": "x", "concurrent-scripts": "y"})
            assert tfp.ev(tfp.job_condition(build[name]), ctx) is want, (mode, name)
    print("PASS  mode e2e: Build runs no audit, typecheck, unit test or extra script; Required runs no Caller lint")


def test_prebuilt_handoff_follows_the_switch() -> None:
    """Build packs and uploads the app for the E2E jobs only when they will run."""
    step = next(s for s in JOBS["build"]["steps"] if s.get("id") == "e2e-build-path")
    for variables, inputs, want in (({"CI_E2E_IN_CI": "false"}, BASE, "false"), ({}, BASE, "true"),
                                    ({"CI_E2E_IN_CI": "false"}, {**BASE, "mode": "e2e"}, "true")):
        ctx = context_with_vars("pull_request", "refs/pull/1/merge", inputs, variables=variables)
        assert tfp.gh_str(tfp.render(step["env"]["RUN_E2E"], ctx)) == want, (variables, inputs)
    print("PASS  the prebuilt E2E application is packed and published only when E2E will run")


def run_skip(mode: str, event: str, files: list[str], *, skip_patterns="**/*.md", full_patterns="") -> tuple[str, str]:
    """Execute the shipped `Decide whether E2E can be skipped` text with MODE."""
    base, head = "a" * 40, "b" * 40
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        skip.make_stub_gh(bin_dir, files)
        output = root / "out"
        output.write_text("")
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", GH_TOKEN="x", REPO="o/r",
                   EVENT_NAME=event, REUSED="false", BASE_SHA=base, HEAD_SHA=head,
                   SKIP_PATTERNS=skip_patterns, FULL_PATTERNS=full_patterns,
                   GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=str(root / "summary"))
        if mode:
            env["MODE"] = mode
        (root / "workspace").mkdir()
        result = subprocess.run(["bash", "-c", skip.skip_step_script()], capture_output=True, text=True,
                                env=env, cwd=root / "workspace")
        assert result.returncode == 0, result.stdout + result.stderr
        values = dict(line.split("=", 1) for line in output.read_text().split())
        return values["skipped"], values["full"]


def test_mode_e2e_never_path_skips() -> None:
    docs_only = ["README.md", "docs/a.md"]
    assert run_skip("", "push", docs_only)[0] == "true", "control: a docs-only push path-skips in ci mode"
    assert run_skip("ci", "push", docs_only)[0] == "true"
    for event in ("push", "schedule", "workflow_dispatch", "pull_request"):
        skipped, full = run_skip("e2e", event, docs_only)
        assert (skipped, full) == ("false", "true"), (event, skipped, full)
    print("PASS  mode e2e never path-skips: a docs-only push after a cancelled code push still runs the full suite")


def test_proof_key_ignores_mode() -> None:
    script = next(s for s in JOBS["e2e-plan"]["steps"] if s.get("id") == "proof")["with"]["script"]
    legacy = {"e2e-args": "--project=web", "e2e-shards": 3}
    ci = tfp.run_script(script, {}, {**legacy, "mode": "ci"}, "E2E_INPUTS")["outputs"]["key"]
    post_merge = tfp.run_script(script, {}, {**legacy, "mode": "e2e"}, "E2E_INPUTS")["outputs"]["key"]
    absent = tfp.run_script(script, {}, legacy, "E2E_INPUTS")["outputs"]["key"]
    assert ci == post_merge == absent, "the mode input changed the E2E proof key"
    required = JOBS["reuse-plan"]["steps"]
    lookup = next(s for s in required if s.get("id") == "proof")["with"]["script"]
    with_ci = tfp.run_script(lookup, {}, {**tfp.defaults(), "required-reuse-pr-results": True}, "PROOF_INPUTS")
    without = {k: v for k, v in {**tfp.defaults(), "required-reuse-pr-results": True}.items() if k != "mode"}
    assert with_ci["outputs"]["key"] == tfp.run_script(lookup, {}, without, "PROOF_INPUTS")["outputs"]["key"], \
        "the default mode changed the Required proof key"
    print("PASS  the mode input never changes a proof key (E2E: always dropped; Required: dropped at its default)")


def main() -> None:
    for test in (test_mode_input, test_switch_defaults_change_nothing, test_switch_off_skips_the_e2e_lanes,
                 test_switch_leaves_promises_to_run_browsers, test_switch_off_without_run_e2e_is_unchanged,
                 test_fold_reads_the_effective_switch, test_mode_e2e_runs_only_the_e2e_chain,
                 test_mode_e2e_verdict, test_mode_e2e_skips_caller_lint_and_lint_steps,
                 test_prebuilt_handoff_follows_the_switch, test_mode_e2e_never_path_skips,
                 test_proof_key_ignores_mode):
        test()
    print("e2e mode contract passed")


if __name__ == "__main__":
    main()
