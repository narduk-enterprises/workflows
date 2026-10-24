#!/usr/bin/env python3
"""Execute shipped admission scripts and evaluate deferred job graphs offline."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml

from test_fast_path import StatusEvaluator, truthy

FILES = ("apple", "node-library", "python-data", "docs-governance",
         "nuxt-cloudflare", "reusable-browser-tests")
DOCS = {name: yaml.safe_load(Path(f".github/workflows/{name}.yml").read_text()) for name in FILES}
SHA, BASE, REV = "a" * 40, "b" * 40, "c" * 40
CONTRACT = hashlib.sha256(b'{}\n{}').hexdigest()
PR = {"number": 7, "draft": False, "head": {"sha": SHA, "repo": {"id": 1}}, "base": {"sha": BASE}}
RUN = {"id": 10, "run_attempt": 1, "workflow_id": 2, "repository": {"id": 1},
       "head_repository": {"id": 1}, "head_sha": SHA, "path": ".github/workflows/ci.yml",
       "event": "pull_request", "status": "completed", "conclusion": "success",
       "pull_requests": [PR], "referenced_workflows": [{"path": "workflows/ci.yml@v1", "sha": REV}]}
HARNESS = r"""
const fs = require('node:fs');
const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const script = fs.readFileSync(process.argv[3], 'utf8');
process.env.GITHUB_RUN_ATTEMPT = String(fixture.attempt || 1);
process.env.CI_INPUTS_JSON = fixture.inputs || '{}';
process.env.CI_VARS_JSON = fixture.vars || '{}';
const outputs = {}, calls = [];
const core = {setOutput(k,v) {outputs[k] = v}, info() {}, notice() {}};
const context = {repo: {owner:'example', repo:'app'}, runId: 20,
  eventName: fixture.event || 'pull_request', payload: fixture.payload};
const github = {rest: {actions: {
  async getWorkflowRun(args) {calls.push(['current', args]);
    if (fixture.denied) throw {status:403};
    return {data: fixture.current};},
  async listWorkflowRuns(args) {calls.push(['runs', args]); return {data: {workflow_runs: fixture.runs}}},
  async listJobsForWorkflowRunAttempt(args) {calls.push(['jobs', args]); return {data: fixture.jobs}},
}}};
(async () => {
  await new Function('github','context','core','require',`return (async () => {${script}})()`)(github,context,core,require);
  process.stdout.write(JSON.stringify({outputs,calls}));
})().catch(error => {console.error(error); process.exit(1)});
"""


def fixture():
    data = copy.deepcopy({"payload": {"action": "edited", "changes": {"body": {}}, "pull_request": PR},
                          "current": {**RUN, "id": 20}, "runs": [RUN],
                          "jobs": {"total_count": 1, "jobs": [{"name": "ci / Required", "conclusion": "success",
                          "steps": [{"name": f"Validated CI contract {CONTRACT}", "conclusion": "success"}]}]}})
    data["runs"] = copy.deepcopy(data["runs"])
    return data


class Expr(StatusEvaluator):
    def call(self, name, args):
        if name == "toJSON":
            return json.dumps(args[0], separators=(",", ":"))
        return super().call(name, args)


def ev(value, ctx):
    if isinstance(value, bool):
        return value
    value = str(value).strip()
    if value.startswith("${{"):
        value = value[3:-2]
    return Expr(ctx).run(value)


def render(value, ctx):
    return re.sub(r"\$\{\{(.*?)\}\}", lambda m: str(ev(m.group(1), ctx)), value)


def context(doc, draft=False, event="pull_request", **inputs):
    defaults = {key: spec.get("default", "") for key, spec in doc[True]["workflow_call"]["inputs"].items()}
    defaults.update(inputs)
    return {"inputs": defaults, "github": {"event_name": event, "ref": "refs/pull/7/merge",
            "workflow": "CI", "run_id": 20, "run_attempt": 1,
            "event": {"action": "edited", "pull_request": {**PR, "draft": draft},
                      "repository": {"default_branch": "main", "private": False}}},
            "vars": {}, "env": {}, "matrix": {}, "__status": "success"}


def graph(doc, ctx, reason, admission="success", results=None, admission_outputs=None):
    state = {}

    def settle(jid):
        if jid in state:
            return
        job = doc["jobs"][jid]
        deps = job.get("needs", [])
        deps = [deps] if isinstance(deps, str) else deps
        for dep in deps:
            settle(dep)
        local = {**ctx, "needs": {dep: state[dep] for dep in deps}}
        cond = job.get("if", "true")
        enabled = truthy(ev(cond, local))
        if not re.search(r"\b(always|cancelled|failure|success)\(", str(cond)):
            enabled &= all(state[dep]["result"] == "success" for dep in deps)
        outputs = (admission_outputs or {"skip": "true", "reason": reason}) if jid == "ci-admission" and admission == "success" else {}
        outcome = admission if jid == "ci-admission" else (results or {}).get(jid, "success")
        state[jid] = {"result": outcome if enabled else "skipped",
                      "outputs": outputs if enabled else {}, "ctx": local}

    for jid in doc["jobs"]:
        settle(jid)
    return state


class AdmissionTests(unittest.TestCase):
    def execute(self, data, slug="nuxt-cloudflare", script=None):
        script = script or DOCS[slug]["jobs"]["ci-admission"]["steps"][0]["with"]["script"]
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / name for name in ("fixture.json", "script.js", "harness.js")]
            for path, content in zip(paths, (json.dumps(data), script, HARNESS)):
                path.write_text(content)
            result = subprocess.run(["node", str(paths[2]), str(paths[0]), str(paths[1])],
                                    text=True, capture_output=True, check=True)
            return json.loads(result.stdout)

    def test_all_shipped_scripts_agree_and_edits_validate_without_api(self):
        scripts = {doc["jobs"]["ci-admission"]["steps"][0]["with"]["script"] for doc in DOCS.values()}
        self.assertEqual(len(scripts), 1)
        for slug, doc in DOCS.items():
            self.assertEqual(doc["jobs"]["ci-admission"]["permissions"],
                             {"contents": "read", "actions": "read"}
                             if slug in ("nuxt-cloudflare", "reusable-browser-tests") else {"contents": "read"})
            for changes in ({"title": {}}, {"body": {}}, {"title": {}, "body": {}},
                            {}, {"base": {}}, {"body": {}, "base": {}}):
                with self.subTest(slug=slug, changes=changes):
                    data = fixture()
                    data["payload"]["changes"] = changes
                    result = self.execute(data, slug)
                    self.assertEqual(result["outputs"], {"skip": "false", "reason": "validate"})
                    self.assertEqual(result["calls"], [])
            self.assertFalse(ev(doc["jobs"]["ci-admission"]["if"], context(doc)))

    def test_browser_contract_failure_still_blocks_unvalidated_routes(self):
        doc = DOCS["reusable-browser-tests"]
        self.assertEqual(doc["jobs"]["ci-admission"]["runs-on"], doc["jobs"]["contract"]["runs-on"])
        for failure in ("contract", "validate", "chromium"):
            ctx = context(doc, **{"run-webkit": True})
            ctx["github"]["event"]["action"] = "synchronize"
            state = graph(doc, ctx, "validate", results={failure: "failure"})
            order = ["contract", "validate", "chromium", "webkit"]
            for downstream in order[order.index(failure) + 1:]:
                self.assertEqual(state[downstream]["result"], "skipped", (failure, downstream))

    def test_drafts_need_no_api_and_non_pr_events_never_defer(self):
        data = fixture()
        data["payload"]["pull_request"]["draft"] = True
        result = self.execute(data)
        self.assertEqual(result["outputs"]["reason"], "draft")
        self.assertEqual(result["calls"], [])
        data["attempt"] = 2
        self.assertEqual(self.execute(data)["outputs"]["skip"], "false")
        for event in ("push", "workflow_dispatch", "schedule", "pull_request_target"):
            data["event"] = event
            result = self.execute(data)
            self.assertEqual(result["outputs"]["skip"], "false")
            self.assertEqual(result["calls"], [])

    def test_ready_and_full_rerun_recover_from_draft_outputs(self):
        for slug, doc in DOCS.items():
            for action in ("opened", "synchronize", "ready_for_review", "reopened"):
                data = fixture()
                data["payload"]["action"] = action
                self.assertEqual(self.execute(data, slug)["outputs"]["skip"], "false")
            ctx = context(doc)
            ctx["github"]["event"]["action"] = "ready_for_review"
            ready = graph(doc, ctx, "validate")
            self.assertEqual(ready["ci-admission"]["result"], "skipped")
            self.assertEqual(ready["required"]["result"], "success")
            # A full rerun executes admission again, but retains the draft event payload.
            data = fixture()
            data["payload"]["pull_request"]["draft"] = True
            data["attempt"] = 2
            result = self.execute(data, slug)
            self.assertEqual(result["outputs"], {"skip": "false", "reason": "validate"})
            self.assertEqual(result["calls"], [])
            ctx = context(doc, draft=True)
            ctx["github"]["run_attempt"] = 2
            rerun = graph(doc, ctx, "validate", admission_outputs=result["outputs"])
            self.assertEqual(rerun["ci-admission"]["result"], "success")
            self.assertEqual(rerun["required"]["result"], "success")
            for jid in doc["jobs"]:
                if jid != "ci-admission":
                    self.assertEqual(rerun[jid]["result"], ready[jid]["result"], (slug, jid))

    def test_retargeted_pr_current_api_state_cannot_reuse_old_base_success(self):
        data = fixture()
        # The successful run validated BASE. After retargeting, GitHub returns
        # the PR's CURRENT base on that old run too, not the validated base.
        current_pr = copy.deepcopy(PR)
        current_pr["base"] = {"sha": "d" * 40, "ref": "release"}
        data["payload"]["pull_request"] = current_pr
        data["runs"][0]["pull_requests"] = [copy.deepcopy(current_pr)]
        data["current"]["pull_requests"] = [copy.deepcopy(current_pr)]
        self.assertNotEqual(BASE, data["runs"][0]["pull_requests"][0]["base"]["sha"])
        # The retarget run failed, then a body edit arrives on the same head.
        data["runs"].append({**copy.deepcopy(data["runs"][0]), "id": 11, "conclusion": "failure"})
        for slug in FILES:
            result = self.execute(data, slug)
            self.assertEqual(result["outputs"], {"skip": "false", "reason": "validate"})
            self.assertEqual(result["calls"], [])

    def test_two_open_prs_from_one_head_to_different_bases_each_validate(self):
        first = copy.deepcopy(PR)
        first["base"]["ref"] = "main"
        second = copy.deepcopy(PR)
        second.update(number=8, base={"sha": "d" * 40, "ref": "release"})
        self.assertEqual(first["head"], second["head"])
        for pr in (first, second):
            data = fixture()
            data["payload"]["pull_request"] = pr
            # Run associations contain both current PRs; they do not identify
            # which base a historical run actually validated.
            data["runs"][0]["pull_requests"] = copy.deepcopy([first, second])
            for slug in FILES:
                with self.subTest(pr=pr["number"], slug=slug):
                    result = self.execute(data, slug)
                    self.assertEqual(result["outputs"], {"skip": "false", "reason": "validate"})
                    self.assertEqual(result["calls"], [])

    def test_admission_report_rejects_missing_or_inconsistent_outputs(self):
        cases = [
            ("skipped", "", "", 0), ("success", "false", "validate", 0),
            ("success", "true", "successful-head", 1),
            ("success", "true", "draft", 1), ("success", "true", "unknown", 1),
            ("success", "", "", 1), ("failure", "false", "validate", 1),
            ("cancelled", "true", "draft", 1),
        ]
        for slug, doc in DOCS.items():
            for job in doc["jobs"].values():
                for step in job.get("steps", []):
                    if step.get("name") != "Check CI admission":
                        continue
                    for result, skip, reason, expected in cases:
                        env = {**os.environ, "ADMISSION_RESULT": result, "ADMISSION_SKIP": skip,
                               "ADMISSION_REASON": reason}
                        actual = subprocess.run(["bash", "-c", step["run"]], env=env,
                                                text=True, capture_output=True)
                        self.assertEqual(actual.returncode, expected, (slug, result, skip, reason))
                        if reason == "draft" and result == "success":
                            self.assertIn("push a commit", actual.stdout)
                            self.assertIn("Re-run all jobs", actual.stdout)
                            self.assertIn("Re-run failed jobs keeps draft admission outputs", actual.stdout)
                            self.assertIn("ready_for_review in the caller pull_request.types", actual.stdout)

    def test_deferred_graph_always_reports_required_without_heavy_work(self):
        configs = [({}, "Required"), ({"checks-in-build": True, "preview-checks": "none"}, "Required"),
                   ({"fast-scripts": "lint", "pr-fast-only": True}, "Fast"),
                   ({"fast-scripts": "lint", "pr-fast-only": True, "e2e-full-paths": "src/**"}, "Fast")]
        for slug, doc in DOCS.items():
            for overrides, wanted in configs if slug == "nuxt-cloudflare" else [({}, "Required")]:
                with self.subTest(slug=slug, inputs=overrides, reason="draft"):
                    ctx = context(doc, draft=True, **overrides)
                    state = graph(doc, ctx, "draft")
                    gates = [(jid, render(doc["jobs"][jid].get("name", jid), s["ctx"]))
                             for jid, s in state.items() if s["result"] == "success"]
                    self.assertEqual(sum(name == "Required" for _, name in gates), 1, gates)
                    self.assertTrue(any(name == wanted for _, name in gates), gates)
                    self.assertTrue(all(jid in ("ci-admission", "required", "fast", "fast-escalable") for jid, _ in gates), gates)
                    for jid, _ in gates:
                        if jid == "ci-admission":
                            continue
                        job = doc["jobs"][jid]
                        local = {**state[jid]["ctx"], "env": {}}
                        local["env"] = {key: str(ev(value, local)).lower()
                                        for key, value in job.get("env", {}).items()
                                        if isinstance(value, str) and value.startswith("${{")}
                        steps = [step for step in job["steps"] if truthy(ev(step.get("if", "true"), local))]
                        self.assertEqual([step.get("name") for step in steps], ["Check CI admission"])
                        env = {**os.environ, **{k: render(v, local) for k, v in steps[0]["env"].items()}}
                        result = subprocess.run(["bash", "-c", steps[0]["run"]], env=env, capture_output=True)
                        self.assertEqual(result.returncode, 1)

    def test_concurrency_separates_non_prs_invocations_and_matrix_lanes(self):
        for slug, doc in DOCS.items():
            for jid, job in doc["jobs"].items():
                if jid == "ci-admission":
                    continue
                policy = job["concurrency"]
                ctx = context(doc)
                group = render(policy["group"], ctx)
                self.assertTrue(group.startswith(f"workflows-{slug}-{jid}-"))
                self.assertTrue(ev(policy["cancel-in-progress"], ctx))
                ctx["github"]["run_id"] += 1
                self.assertEqual(render(policy["group"], ctx), group)
                ctx["inputs"]["ci-concurrency-key"] = "second"
                self.assertNotEqual(render(policy["group"], ctx), group)
                for event in ("push", "workflow_dispatch", "schedule", "pull_request_target"):
                    ctx = context(doc, event=event)
                    ctx["github"]["ref"] = "refs/heads/main"
                    self.assertFalse(ev(policy["cancel-in-progress"], ctx))
                    before = render(policy["group"], ctx)
                    ctx["github"]["run_id"] += 1
                    self.assertNotEqual(before, render(policy["group"], ctx))
                if "strategy" in job:
                    ctx = context(doc)
                    ctx["matrix"] = {"shard": 1}
                    before = render(policy["group"], ctx)
                    ctx["matrix"] = {"shard": 2}
                    self.assertNotEqual(before, render(policy["group"], ctx))
                if slug == "nuxt-cloudflare":
                    for override in ({"mode": "e2e"}, {"journey-smoke-url": "https://example.test"},
                                     {"expected-candidate-sha": SHA}):
                        ctx = context(doc, **override)
                        self.assertFalse(ev(policy["cancel-in-progress"], ctx))
                        self.assertFalse(ev(doc["jobs"]["ci-admission"]["if"], ctx))


if __name__ == "__main__":
    unittest.main()
