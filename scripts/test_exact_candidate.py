#!/usr/bin/env python3
"""Execute the shipped candidate guard against real Git checkouts."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


class ExactCandidate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.safe_load(Path('.github/workflows/nuxt-cloudflare.yml').read_text())

    def test_every_source_checkout_is_pinned_and_verified(self):
        inputs = self.workflow[True]['workflow_call']['inputs']
        self.assertEqual(inputs['expected-candidate-sha']['default'], '')
        self.assertFalse(inputs['expected-candidate-sha']['required'])
        checked = 0
        for name, job in self.workflow['jobs'].items():
            steps = job.get('steps', [])
            for index, step in enumerate(steps):
                if str(step.get('uses', '')).startswith('actions/checkout@'):
                    checked += 1
                    self.assertEqual(step['with']['ref'], '${{ inputs.expected-candidate-sha }}', name)
                    guard = steps[index + 1]
                    self.assertEqual(guard['name'], 'Verify exact validation candidate', name)
                    self.assertEqual(guard['if'], "inputs.expected-candidate-sha != ''", name)
                    self.assertEqual(guard['working-directory'], '${{ github.workspace }}')
        self.assertGreaterEqual(checked, 8)

    def test_shipped_guards_refuse_wrong_event_or_source(self):
        guards = {step['run'] for job in self.workflow['jobs'].values()
                  for step in job.get('steps', [])
                  if step.get('name') == 'Verify exact validation candidate'}
        self.assertEqual(len(guards), 1, 'Candidate guards must not drift between jobs')
        script = guards.pop()
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=directory, text=True).strip()
            git('init', '-q')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                'commit', '--allow-empty', '-qm', 'candidate')
            sha = git('rev-parse', 'HEAD')
            base = dict(os.environ, EXPECTED_CANDIDATE=sha, EVENT_SHA=sha,
                        EVENT_NAME='workflow_dispatch', EVENT_REF='refs/heads/candidate')
            scenarios = [({}, True), ({'EXPECTED_CANDIDATE': sha[:12]}, False),
                         ({'EVENT_SHA': 'a' * 40}, False), ({'EVENT_NAME': 'push'}, False),
                         ({'EVENT_REF': 'refs/tags/candidate'}, False),
                         ({'EXPECTED_CANDIDATE': 'a' * 40, 'EVENT_SHA': 'a' * 40}, False),
                         ({'EXPECTED_CANDIDATE': '$(touch injected)'}, False)]
            for overrides, expected in scenarios:
                with self.subTest(overrides=overrides):
                    result = subprocess.run(['bash', '-c', script], cwd=directory,
                                            env=base | overrides, capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, expected, result.stdout + result.stderr)
                    self.assertFalse(Path(directory, 'injected').exists())

    def test_required_does_not_waive_failed_candidate_jobs(self):
        required = self.workflow['jobs']['required']
        for name in ['build', 'checks', 'caller-lint']:
            self.assertIn(name, required['needs'])
        self.assertEqual(required['if'], 'always()')
        gate = next(step for step in required['steps'] if step.get('name') ==
                    'Require enabled gates to succeed and disabled gates to skip')
        script = gate['run'].replace('${{ inputs.extra-gate-scripts }}', '')
        base = dict(os.environ, BUILD_RESULT='success', CHECKS_RESULT='success',
                    CALLER_LINT_RESULT='success', EXTRA_GATE_RESULT='skipped',
                    E2E_PLAN_RESULT='skipped', E2E_RESULT='skipped', E2E_REPORT_RESULT='skipped',
                    DEPLOY_DRY_RUN_RESULT='skipped', PREVIEW_RESULT='skipped',
                    PREVIEW_CHECKS='none', EVENT_NAME='workflow_dispatch', RUN_E2E='false',
                    E2E_SHARDS='1', E2E_PLAN_SKIPPED='', RUN_DEPLOY_DRY_RUN='false')
        for job in ['BUILD_RESULT', 'CHECKS_RESULT', 'CALLER_LINT_RESULT']:
            for outcome in ['failure', 'cancelled', 'skipped', '']:
                result = subprocess.run(['bash', '-c', script], env=base | {job: outcome},
                                        capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, (job, outcome))

    def test_node_route_guards_follow_node_setup(self):
        # A fresh manifest-routed Linux guest need not have node on PATH.
        for name in ['e2e', 'e2e-quarantine', 'preview']:
            steps = self.workflow['jobs'][name]['steps']
            setup = next(i for i, step in enumerate(steps)
                         if str(step.get('uses', '')).startswith('actions/setup-node@'))
            guard = next(i for i, step in enumerate(steps)
                         if step.get('name') == 'Guard isolated Playwright route')
            self.assertLess(setup, guard, name)


if __name__ == '__main__':
    unittest.main()
