#!/usr/bin/env python3
"""Execute the shipped proof lookup; failed, partial or different runs never skip."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

import yaml

WORKFLOW = Path('.github/workflows/nuxt-cloudflare.yml')
HARNESS = r'''
const fs = require('node:fs');
const scenario = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const script = fs.readFileSync(process.argv[2], 'utf8');
const outputs = {}, calls = [];
const context = {
  repo: {owner: 'example', repo: 'app'}, sha: 'a'.repeat(40),
  eventName: scenario.event || 'push', payload: {repository: {id: 42}},
};
const core = {
  setOutput(k, v) { outputs[k] = v; }, info() {}, warning() {}, notice() {},
  summary: {addRaw() {return this;}, async write() {}},
};
const github = {rest: {
  repos: {async getCommit() {
    if (scenario.commitError) throw Error('unavailable');
    return {data: {commit: {tree: {sha: scenario.tree || 'b'.repeat(40)}}}};
  }},
  actions: {
    async listArtifactsForRepo(args) {
      calls.push(args);
      if (scenario.apiError) throw Error('forbidden');
      const artifact = {
        name: scenario.proofKey || args.name, expired: false,
        created_at: '2026-09-20T12:05:00Z',
        workflow_run: {id: 123, repository_id: 42, head_repository_id: 42},
        ...scenario.artifact,
      };
      return {data: {artifacts: scenario.missing ? [] : [artifact]}};
    },
    async getWorkflowRun(args) {
      calls.push(args);
      if (scenario.runError) throw Error('unavailable');
      return {data: {
        event: 'pull_request', status: 'completed', conclusion: 'success',
        path: '.github/workflows/ci.yml', repository: {id: 42}, head_repository: {id: 42},
        run_started_at: '2026-09-20T12:00:00Z', html_url: 'https://example.test/run/123',
        ...scenario.run,
      }};
    },
  },
}};
(async () => {
  await new (Object.getPrototypeOf(async function() {}).constructor)('require', 'core', 'context', 'github', script)(require, core, context, github);
  console.log(JSON.stringify({outputs, calls}));
})().catch(error => {console.error(error); process.exit(1);});
'''


def run_lookup(script: str, scenario: dict, inputs: dict | None = None, workflow: str = 'example/app/.github/workflows/ci.yml@refs/heads/main') -> dict:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'scenario.json').write_text(json.dumps(scenario))
        (root / 'script.js').write_text(script)
        result = subprocess.run(
            ['node', '-e', HARNESS, str(root / 'scenario.json'), str(root / 'script.js')],
            env={**os.environ, 'CALLER_WORKFLOW_REF': workflow,
                 'E2E_INPUTS': json.dumps(inputs or {'e2e-args': '--project=web', 'e2e-shards': 3})},
            capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)


def main() -> None:
    doc = yaml.safe_load(WORKFLOW.read_text())
    plan = doc['jobs']['e2e-plan']
    lookup = next(step for step in plan['steps'] if step.get('id') == 'proof')
    assert 'inputs.e2e-reuse-pr-results' in lookup['if']
    assert "github.event_name == 'push'" in lookup['if']
    assert 'github.event.repository.default_branch' in lookup['if']
    assert 'github.event_name == \'pull_request\'' in lookup['if']
    assert 'pull_request_target' not in lookup['if']
    assert plan['outputs']['proof-key'] == '${{ steps.proof.outputs.key }}'
    skip = next(step for step in plan['steps'] if step.get('id') == 'skip')
    assert skip['env']['REUSED'] == '${{ steps.proof.outputs.reused }}'
    required = doc['jobs']['required']['steps']
    publish = next(step for step in required if step.get('id') == 'e2e-proof')
    for guard in ('success()', "github.event_name == 'pull_request'",
                  'github.event.pull_request.head.repo.full_name == github.repository',
                  "needs.e2e.result == 'success'", 'needs.e2e-plan.outputs.proof-key',
                  'needs.e2e-plan.outputs.e2e-args == inputs.e2e-args',
                  "fromJSON(needs.e2e-plan.outputs.shard-total || '0') == inputs.e2e-shards"):
        assert guard in publish['if'], guard
    assert required.index(publish) > 0, 'proof must follow the Required gate'
    upload = next(step for step in required if step.get('name') == 'Publish full E2E proof')
    assert upload['if'] == "success() && steps.e2e-proof.outputs.path != ''"
    assert upload['with']['name'] == '${{ needs.e2e-plan.outputs.proof-key }}'
    assert upload['with']['overwrite'] is True
    print('PASS  only an actually successful full PR gate can publish proof')

    script = lookup['with']['script']
    baseline = run_lookup(script, {})
    assert baseline['outputs']['reused'] == 'true'
    assert baseline['calls'][0]['per_page'] == 5
    key = baseline['outputs']['key']
    pr = run_lookup(script, {'event': 'pull_request'}, workflow='example/app/.github/workflows/ci.yml@refs/pull/1/merge')
    assert pr['outputs'] == {'reused': 'false', 'key': key}
    assert pr['calls'] == []
    print('PASS  PR computes matching tree/input key without reusing any proof')
    cases = {
        'missing proof': {'missing': True},
        'unreadable artifact API': {'apiError': True},
        'unreadable source tree': {'commitError': True},
        'invalid source tree': {'tree': 'unknown'},
        'unreadable source run': {'runError': True},
        'expired proof': {'artifact': {'expired': True}},
        'different proof key': {'proofKey': 'e2e-proof-v1-other'},
        'fork proof': {'artifact': {'workflow_run': {'id': 123, 'repository_id': 42, 'head_repository_id': 43}}},
        'missing provenance': {'artifact': {'workflow_run': None}},
        'failed source run': {'run': {'conclusion': 'failure'}},
        'cancelled source run': {'run': {'conclusion': 'cancelled'}},
        'pending source run': {'run': {'status': 'in_progress'}},
        'push source run': {'run': {'event': 'push'}},
        'target source run': {'run': {'event': 'pull_request_target'}},
        'different workflow': {'run': {'path': '.github/workflows/other.yml'}},
        'foreign source repository': {'run': {'repository': {'id': 43}}},
        'fork source repository': {'run': {'head_repository': {'id': 43}}},
        'old run-attempt proof': {'run': {'run_started_at': '2026-09-20T12:10:00Z'}},
        'missing attempt timestamp': {'run': {'run_started_at': None}},
        'merged tree changed': {'tree': 'c' * 40, 'proofKey': key},
    }
    for name, scenario in cases.items():
        result = run_lookup(script, scenario)
        assert result['outputs']['reused'] == 'false', (name, result)
        print(f'PASS  {name} runs E2E')
    for name, value in [('e2e-args', '--project=other'), ('e2e-shards', 4), ('extra-env', 'DIFFERENT=1')]:
        inputs = {'e2e-args': '--project=web', 'e2e-shards': 3, name: value}
        assert run_lookup(script, {'proofKey': key}, inputs)['outputs']['reused'] == 'false'
    reordered = {'e2e-shards': 3, 'e2e-args': '--project=web'}
    assert run_lookup(script, {}, reordered)['outputs']['key'] == key
    print('PASS  changed inputs invalidate proof; key ordering does not')
    print('e2e reuse contract passed')


if __name__ == '__main__':
    main()
