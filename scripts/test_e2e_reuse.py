#!/usr/bin/env python3
"""Execute the shipped proof lookup; failed, partial or different runs never skip.

Reuse proof is a pointer object in the CI artifact store (R2), not a GitHub
artifact. The harness below runs the workflow's own github-script text with
the workflow's own store client (written by the `Install CI artifact store
client` step's own bash), an in-memory stand-in for the bucket, and a stand-in
for the GitHub API. A pointer is honoured only when GitHub confirms the run it
names; every other shape of pointer, run or job list must run E2E.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

import yaml

WORKFLOW = Path('.github/workflows/nuxt-cloudflare.yml')
INSTALL_STEP = 'Install CI artifact store client'
STORE_SECRETS = ('CI_ARTIFACTS_R2_ACCOUNT_ID', 'CI_ARTIFACTS_R2_ACCESS_KEY_ID', 'CI_ARTIFACTS_R2_SECRET_ACCESS_KEY')
OTHER_KEY = {'e2e': 'e2e-proof-v1-' + '0' * 64, 'required': 'required-proof-v1-' + '0' * 64}

# scenario keys:
#   event            context.eventName (default push)
#   tree, commitError                     repos.getCommit
#   noCreds, env                          store credentials in process.env
#   missing, storedKey, r2Status, r2Throw, pointerRaw, pointer, proofKey
#                                         the bucket stand-in
#   run, runError                         actions.getWorkflowRun
#   jobs, jobsError, jobName, jobConclusion, stepName, stepConclusion
#                                         actions.listJobsForWorkflowRunAttempt
HARNESS = r'''
const fs = require('node:fs');
const scenario = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const script = fs.readFileSync(process.argv[2], 'utf8');
const outputs = {}, calls = [], log = [];
// Production retries back off for seconds; the harness takes them at once.
const realSetTimeout = setTimeout;
globalThis.setTimeout = (fn, ms, ...args) => realSetTimeout(fn, 0, ...args);
const ACCOUNT = '0123456789abcdef0123456789abcdef';
const ACCESS = 'fedcba9876543210fedcba9876543210';
if (!scenario.noCreds) {
  Object.assign(process.env, {
    CI_ARTIFACTS_R2_ACCOUNT_ID: ACCOUNT, CI_ARTIFACTS_R2_ACCESS_KEY_ID: ACCESS,
    CI_ARTIFACTS_R2_SECRET_ACCESS_KEY: 'ab'.repeat(32),
  });
}
Object.assign(process.env, scenario.env || {});
const context = {
  repo: {owner: 'example', repo: 'app'}, sha: 'a'.repeat(40),
  eventName: scenario.event || 'push', payload: {repository: {id: 42}},
};
const core = {
  setOutput(k, v) { outputs[k] = v; },
  info(m) { log.push(['info', m]); }, notice(m) { log.push(['notice', m]); },
  warning(m) { log.push(['warning', m]); }, setFailed(m) { log.push(['failed', m]); },
  summary: {addRaw() { return this; }, async write() {}},
};
// The bucket: one pointer per proof key, as a pull request's Required wrote it.
const PREFIX = `https://${ACCOUNT}.r2.cloudflarestorage.com/narduk-ci-artifacts/`;
let served = null;
globalThis.fetch = async (url, init = {}) => {
  const method = init.method || 'GET';
  if (!String(url).startsWith(PREFIX)) throw new Error(`unexpected fetch ${url}`);
  const objectKey = decodeURIComponent(String(url).slice(PREFIX.length).split('?')[0]);
  calls.push({r2: `${method} ${objectKey}`});
  const auth = (init.headers || {}).authorization || '';
  if (!auth.startsWith(`AWS4-HMAC-SHA256 Credential=${ACCESS}/`)
      || !/\/auto\/s3\/aws4_request, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=[0-9a-f]{64}$/.test(auth)) {
    return new Response('<Error><Code>AccessDenied</Code></Error>', {status: 403});
  }
  if (scenario.r2Throw) throw new TypeError('fetch failed');
  if (scenario.r2Status) return new Response('<Error><Code>InternalError</Code></Error>', {status: scenario.r2Status});
  const match = /^proof\/example\/app\/((required|e2e)-proof-v1-[0-9a-f]{64})\.json$/.exec(objectKey);
  if (!match || scenario.missing || (scenario.storedKey && scenario.storedKey !== match[1])) {
    return new Response('<Error><Code>NoSuchKey</Code></Error>', {status: 404});
  }
  served = {key: match[1], kind: match[2]};
  if ('pointerRaw' in scenario) return new Response(scenario.pointerRaw, {status: 200});
  const pointer = {
    schema: 'narduk-ci-proof-pointer-v1', kind: served.kind, key: scenario.proofKey || served.key,
    repository: 'example/app', repository_id: 42, run_id: 123, run_attempt: 1,
    sha: 'f'.repeat(40), written_at: '2026-09-20T12:05:00.000Z', ...scenario.pointer,
  };
  return new Response(`${JSON.stringify(pointer)}\n`, {status: 200});
};
const github = {
  rest: {
    repos: {async getCommit() {
      if (scenario.commitError) throw Error('unavailable');
      return {data: {commit: {tree: {sha: scenario.tree || 'b'.repeat(40)}}}};
    }},
    actions: {
      async getWorkflowRun(args) {
        calls.push({getWorkflowRun: args.run_id});
        if (scenario.runError) throw Error('unavailable');
        return {data: {
          id: args.run_id, run_attempt: 1, event: 'pull_request', status: 'completed', conclusion: 'success',
          path: '.github/workflows/ci.yml', repository: {id: 42}, head_repository: {id: 42},
          html_url: `https://example.test/run/${args.run_id}`, ...scenario.run,
        }};
      },
      async listJobsForWorkflowRunAttempt(args) {
        calls.push({listJobs: [args.run_id, args.attempt_number]});
        if (scenario.jobsError) throw Error('unavailable');
        const prefix = served.kind === 'required' ? 'Publish Required proof' : 'Publish full E2E proof';
        const stepName = (scenario.stepName || '{PREFIX} {KEY}').replace('{PREFIX}', prefix).replace('{KEY}', served.key);
        const jobs = scenario.jobs || [
          {name: 'ci / Build', conclusion: 'success', steps: [{name: 'Build', conclusion: 'success'}]},
          {name: scenario.jobName || 'ci / Required', conclusion: scenario.jobConclusion || 'success', steps: [
            {name: 'Set up job', conclusion: 'success'},
            {name: stepName, conclusion: scenario.stepConclusion || 'success'},
          ]},
        ];
        return {data: {total_count: jobs.length, jobs}};
      },
    },
  },
  // Octokit's paginate unwraps a list endpoint to its items.
  async paginate(method, args) { return (await method(args)).data.jobs; },
};
(async () => {
  await new (Object.getPrototypeOf(async function() {}).constructor)('require', 'core', 'context', 'github', script)(require, core, context, github);
  console.log(JSON.stringify({outputs, calls, log}));
})().catch(error => {console.error(error); process.exit(1);});
'''


def install_script(document: dict | None = None) -> str:
    """The shipped install step: every copy is one YAML alias of this text."""
    document = document or yaml.safe_load(WORKFLOW.read_text())
    return next(s for s in document['jobs']['reuse-plan']['steps'] if s.get('name') == INSTALL_STEP)['run']


def run_github_script(script: str, scenario: dict, env: dict, install: bool = True) -> dict:
    """Run a github-script body with the store client installed by the
    workflow's own install step into a private RUNNER_TEMP."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        runner_temp = root / 'runner-temp'
        runner_temp.mkdir()
        base = {k: v for k, v in os.environ.items() if k not in STORE_SECRETS}
        base['RUNNER_TEMP'] = str(runner_temp)
        if install:
            subprocess.run(['bash', '-c', install_script()], env=base, check=True, timeout=30)
        (root / 'scenario.json').write_text(json.dumps(scenario))
        (root / 'script.js').write_text(script)
        result = subprocess.run(
            ['node', '-e', HARNESS, str(root / 'scenario.json'), str(root / 'script.js')],
            env={**base, **env}, capture_output=True, text=True, check=True, timeout=60,
        )
        return json.loads(result.stdout)


def run_lookup(script: str, scenario: dict, inputs: dict | None = None,
               workflow: str = 'example/app/.github/workflows/ci.yml@refs/heads/main', install: bool = True) -> dict:
    return run_github_script(script, scenario, {
        'CALLER_WORKFLOW_REF': workflow,
        'E2E_INPUTS': json.dumps(inputs or {'e2e-args': '--project=web', 'e2e-shards': 3}),
    }, install)


def main() -> None:
    doc = yaml.safe_load(WORKFLOW.read_text())
    plan = doc['jobs']['e2e-plan']
    lookup = next(step for step in plan['steps'] if step.get('id') == 'proof')
    assert 'inputs.e2e-reuse-pr-results' in lookup['if']
    assert "github.event_name == 'push'" in lookup['if']
    assert 'github.event.repository.default_branch' in lookup['if']
    assert 'github.event_name == \'pull_request\'' in lookup['if']
    assert 'pull_request_target' not in lookup['if']
    for name in STORE_SECRETS:
        assert lookup['env'][name] == '${{ secrets.%s }}' % name, name
    # Only the push reads the store, so only the push installs the client, and
    # it installs it before the lookup that requires it.
    install = next(step for step in plan['steps'] if step.get('name') == INSTALL_STEP)
    assert plan['steps'].index(install) < plan['steps'].index(lookup)
    assert install['if'] == (
        "inputs.e2e-reuse-pr-results && github.event_name == 'push' && "
        "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)")
    script = lookup['with']['script']
    assert script.index("if (context.eventName !== 'push') return;") < script.index('ci-artifact-store.cjs')
    assert plan['permissions']['actions'] == 'read', 'the lookup reads runs and jobs'
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
    # The lookup honours a pointer only when this exact step NAME, carrying
    # the key, succeeded in the run it names: the name is the attestation.
    assert publish['name'] == 'Publish full E2E proof ${{ needs.e2e-plan.outputs.proof-key }}'
    assert publish['env']['PROOF_KEY'] == '${{ needs.e2e-plan.outputs.proof-key }}'
    for name in STORE_SECRETS:
        assert publish['env'][name] == '${{ secrets.%s }}' % name, name
    assert "store.publishProofStep({ core, context, kind: 'e2e' })" in publish['with']['script']
    required_install = next(step for step in required if step.get('name') == INSTALL_STEP)
    assert required.index(required_install) < required.index(publish)
    assert required_install['if'] == "success() && github.event_name == 'pull_request'"
    print('PASS  only an actually successful full PR gate can publish proof, under a name that carries its key')

    baseline = run_lookup(script, {})
    assert baseline['outputs']['reused'] == 'true', baseline
    key = baseline['outputs']['key']
    assert baseline['calls'] == [
        {'r2': f'GET proof/example/app/{key}.json'}, {'getWorkflowRun': 123}, {'listJobs': [123, 1]},
    ], baseline['calls']
    pr = run_lookup(script, {'event': 'pull_request'}, workflow='example/app/.github/workflows/ci.yml@refs/pull/1/merge',
                    install=False)
    assert pr['outputs'] == {'reused': 'false', 'key': key}
    assert pr['calls'] == []
    print('PASS  PR computes matching tree/input key without reading the store or reusing any proof')
    # Each case names the reason it must give: a case that stopped reusing
    # for some other reason (a harness crash, say) proves nothing.
    NOT_ATTESTED = 'shows no successful "Publish full E2E proof'
    NOT_A_PR_RUN = 'is not a successful same-repository pull-request run'
    FOREIGN = 'does not describe this key and repository'
    cases = {
        'missing proof': ({'missing': True}, 'no pointer at proof/example/app/e2e-proof-v1-'),
        'client not installed': ({'__install': False}, 'Cannot find module'),
        'no store credentials (fork, Dependabot, public caller)': ({'noCreds': True}, 'no CI artifact store credentials'),
        'malformed store credentials': ({'env': {'CI_ARTIFACTS_R2_SECRET_ACCESS_KEY': 'not-hex'}}, 'credentials are malformed'),
        'store unavailable': ({'r2Status': 500}, 'HTTP 500 InternalError after 2 attempt(s)'),
        'store unreachable': ({'r2Throw': True}, 'fetch failed after 2 attempt(s)'),
        'store refuses the credentials': ({'r2Status': 403}, 'HTTP 403'),
        'unreadable source tree': ({'commitError': True}, 'unavailable'),
        'invalid source tree': ({'tree': 'unknown'}, 'missing tested Git tree'),
        'pointer is not JSON': ({'pointerRaw': 'not json'}, 'is not JSON'),
        'pointer with another schema': ({'pointer': {'schema': 'narduk-ci-proof-pointer-v0'}}, FOREIGN),
        'pointer of the other proof kind': ({'pointer': {'kind': 'required'}}, FOREIGN),
        'pointer for a different key': ({'proofKey': OTHER_KEY['e2e']}, FOREIGN),
        'pointer from another repository': ({'pointer': {'repository_id': 43}}, FOREIGN),
        'pointer without a run': ({'pointer': {'run_id': None}}, FOREIGN),
        'pointer with a string run id': ({'pointer': {'run_id': '123'}}, FOREIGN),
        'pointer without an attempt': ({'pointer': {'run_attempt': 0}}, FOREIGN),
        'unreadable source run': ({'runError': True}, 'unavailable'),
        'API returns another run': ({'run': {'id': 999}}, NOT_A_PR_RUN),
        'failed source run': ({'run': {'conclusion': 'failure'}}, NOT_A_PR_RUN),
        'cancelled source run': ({'run': {'conclusion': 'cancelled'}}, NOT_A_PR_RUN),
        'pending source run': ({'run': {'status': 'in_progress'}}, NOT_A_PR_RUN),
        'push source run': ({'run': {'event': 'push'}}, NOT_A_PR_RUN),
        'target source run': ({'run': {'event': 'pull_request_target'}}, NOT_A_PR_RUN),
        'different workflow': ({'run': {'path': '.github/workflows/other.yml'}}, NOT_A_PR_RUN),
        'foreign source repository': ({'run': {'repository': {'id': 43}}}, NOT_A_PR_RUN),
        'fork source repository': ({'run': {'head_repository': {'id': 43}}}, NOT_A_PR_RUN),
        'source repository deleted': ({'run': {'head_repository': None}}, NOT_A_PR_RUN),
        'rerun after the pointer was written': ({'run': {'run_attempt': 2}}, 'is on attempt 2, but attempt 1 published'),
        'unreadable job list': ({'jobsError': True}, 'unavailable'),
        'no Required job': ({'jobName': 'ci / Build and test'}, NOT_ATTESTED),
        'failed Required job': ({'jobConclusion': 'failure'}, NOT_ATTESTED),
        'publish step skipped': ({'stepConclusion': 'skipped'}, NOT_ATTESTED),
        'publish step failed': ({'stepConclusion': 'failure'}, NOT_ATTESTED),
        'publish step name never evaluated': ({'stepName': '{PREFIX} ${{ needs.e2e-plan.outputs.proof-key }}'}, NOT_ATTESTED),
        'publish step for another key': ({'stepName': '{PREFIX} ' + OTHER_KEY['e2e']}, NOT_ATTESTED),
        'Required proof step, not the E2E one': ({'stepName': 'Publish Required proof {KEY}'}, NOT_ATTESTED),
        'merged tree changed': ({'tree': 'c' * 40, 'storedKey': key}, 'no pointer at'),
    }
    for name, (scenario, reason) in cases.items():
        install_client = scenario.pop('__install', True)
        result = run_lookup(script, scenario, install=install_client)
        assert result['outputs']['reused'] == 'false', (name, result)
        assert not any(kind == 'failed' for kind, _ in result['log']), (name, result['log'])
        assert any(reason in message for _, message in result['log']), (name, reason, result['log'])
        print(f'PASS  {name} runs E2E')
    for name, value in [('e2e-args', '--project=other'), ('e2e-shards', 4), ('extra-env', 'DIFFERENT=1')]:
        inputs = {'e2e-args': '--project=web', 'e2e-shards': 3, name: value}
        assert run_lookup(script, {'storedKey': key}, inputs)['outputs']['reused'] == 'false'
    reordered = {'e2e-shards': 3, 'e2e-args': '--project=web'}
    assert run_lookup(script, {}, reordered)['outputs']['key'] == key
    print('PASS  changed inputs invalidate proof; key ordering does not')
    print('e2e reuse contract passed')


if __name__ == '__main__':
    main()
