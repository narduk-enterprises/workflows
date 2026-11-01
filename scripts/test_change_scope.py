#!/usr/bin/env python3
"""Execute the shipped classifier and prove same-context Apple fallback routing."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from test_runner_default import evaluate, runner_context

DOC = yaml.safe_load(Path('.github/workflows/change-scope.yml').read_text())
APPLE = yaml.safe_load(Path('.github/workflows/apple.yml').read_text())
SCRIPT = DOC['jobs']['required']['steps'][0]['with']['script']
HARNESS = r'''
const fs = require('node:fs');
const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const source = fs.readFileSync(process.argv[3], 'utf8');
const outputs = {}, calls = [];
const core = {setOutput(k,v) {outputs[k]=v}, info() {}, warning() {}};
const context = fixture.context;
const github = {rest: {repos: {async compareCommitsWithBasehead(args) {
  calls.push(args); if (fixture.denied) throw {status:403}; return {data:fixture.compare};
}}}};
(async () => {
 await new Function('github','context','core',`return (async () => {${source}})()`)(github,context,core);
 process.stdout.write(JSON.stringify({outputs,calls}));
})().catch(error => {console.error(error); process.exit(1)});
'''


def execute(files, event='push', patterns=None, **extra):
    fixture = {'context': {'eventName': event, 'repo': {'owner':'example','repo':'app'},
                'sha':'b'*40, 'payload': {'before':'a'*40, 'pull_request': {
                    'base':{'sha':'a'*40}, 'head':{'sha':'b'*40}}}},
               'compare': {'status':'ahead', 'files':files}, **extra}
    with tempfile.TemporaryDirectory() as tmp:
        paths = [Path(tmp)/name for name in ('fixture.json','source.js','harness.js')]
        for path, value in zip(paths, (json.dumps(fixture), SCRIPT, HARNESS)):
            path.write_text(value)
        return subprocess.run(['node',str(paths[2]),str(paths[0]),str(paths[1])],
            env={**os.environ,'IGNORE_PATHS':json.dumps(patterns if patterns is not None else
                ['README.md','AGENTS.md','docs/*.md'])},text=True,capture_output=True)


class ScopeTests(unittest.TestCase):
    def run_scope(self, files, expected, **kwargs):
        result = execute(files, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['outputs']['run'], expected, data)
        return data

    def test_docs_push_and_pr_skip(self):
        for event in ('push','pull_request'):
            data = self.run_scope([{'filename':'README.md','status':'modified'},
                                   {'filename':'docs/design/architecture.md','status':'added'}], 'false',event=event)
            self.assertEqual(data['calls'][0]['basehead'], 'a'*40+'...'+'b'*40)

    def test_code_unknown_empty_large_and_denied_run_full(self):
        for files in ([], None, [{'filename':'Sources/App.swift','status':'modified'}],
                      [{'filename':'README.md','status':'unknown'}],
                      [{'filename':'README.md','status':'modified'}]*300,
                      [{'filename':'README.md'}], [{}]):
            self.run_scope(files,'true')
        self.run_scope([{'filename':'README.md','status':'modified'}],'true',denied=True)
        self.run_scope([{'filename':'README.md','status':'modified'}],'true',patterns=[])
        for status in ('behind','diverged','identical',None):
            self.run_scope([], 'true', compare={'status':status,'files':[{'filename':'README.md','status':'modified'}]})

    def test_rename_considers_both_names_and_globs_are_literal(self):
        self.run_scope([{'filename':'docs/old.md','previous_filename':'Sources/App.swift','status':'renamed'}],'true')
        self.run_scope([{'filename':'Sources/App.swift','previous_filename':'docs/old.md','status':'renamed'}],'true')
        self.run_scope([{'filename':'docs/new.md','previous_filename':'docs/old.md','status':'renamed'}],'false')
        self.run_scope([{'filename':'READMEZmd','status':'modified'}],'true')
        self.run_scope([{'filename':'docs/check.py','status':'modified'}],'true')
        self.run_scope([{'filename':'docs/[x].md','status':'modified'}],'false',patterns=['docs/[x].md'])

    def test_non_diff_events_run_full_without_api(self):
        for event in ('schedule','workflow_dispatch','merge_group','pull_request_target'):
            self.assertEqual(self.run_scope([], 'true', event=event)['calls'], [])
        for value in (None, '', '0'*40, 'invalid'):
            context = {'eventName':'push','sha':'b'*40,'repo':{},'payload':{'before':value}}
            self.assertEqual(self.run_scope([], 'true',context=context)['calls'],[])
        for flag in ('forced','deleted'):
            context = {'eventName':'push','sha':'b'*40,'repo':{},'payload':{'before':'a'*40,flag:True}}
            self.assertEqual(self.run_scope([], 'true',context=context)['calls'],[])

    def test_invalid_configuration_fails(self):
        for patterns in ('*.md',[None],['']):
            self.assertNotEqual(execute([],patterns=patterns).returncode,0)

    def test_same_xcode_context_runs_on_linux_for_docs(self):
        job = APPLE['jobs']['xcode']
        for private in (True,False,None):
            for run in ('false','true',''):
                ctx = runner_context(private,{'lint-runner':'','apple-runner':'"macos-latest"'}, {})
                ctx['needs']={'changes':{'outputs':{'run':run}}}
                route = evaluate(job['runs-on'], ctx)
                if private is None:
                    self.assertEqual(route,'missing-repository-visibility-do-not-run')
                elif run == 'false':
                    self.assertEqual(route.get('group') if isinstance(route,dict) else route,
                                     'linux-ci' if private else 'ubuntu-latest')
                elif not private:
                    self.assertEqual(route,'macos-latest')
        self.assertEqual(job['name'],'xcode')
        self.assertIn('changes',APPLE['jobs']['required']['needs'])
        self.assertNotIn('if', DOC['jobs']['required'])
        self.assertEqual(APPLE[True]['workflow_call']['inputs']['apple-ignore-paths']['default'],'[]')
        for step in job['steps'][1:]:
            self.assertIn("needs.changes.outputs.run != 'false'",step['if'])
        proof = job['steps'][0]
        for result, run, reason, good in [('success','false','irrelevant-only',True),
                                          ('success','true','full-lane',True),
                                          ('failure','false','irrelevant-only',False),
                                          ('success','','',False),
                                          ('success','false','full-lane',False)]:
            outcome = subprocess.run(['bash','-c',proof['run']],capture_output=True,
                env={**os.environ,'SCOPE_RESULT':result,'RUN_APPLE':run,'SCOPE_REASON':reason})
            self.assertEqual(outcome.returncode==0,good)


if __name__ == '__main__':
    unittest.main()
