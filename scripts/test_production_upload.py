#!/usr/bin/env python3
"""Execute the shipped production handoff scripts against disposable builds."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from test_runner_default import evaluate, runner_context, LINUX_CI

CI = yaml.safe_load(Path('.github/workflows/nuxt-cloudflare.yml').read_text())
UPLOAD = yaml.safe_load(Path('.github/workflows/cloudflare-version-upload.yml').read_text())


def step(document, job, name):
    return next(s for s in document['jobs'][job]['steps'] if s.get('name') == name)


PREPARE = step(CI, 'build', 'Prepare production artifact')['run']
VERIFY = step(UPLOAD, 'upload', 'Verify exact build identity and generated upload configuration')['run']
ARM = step(UPLOAD, 'upload', 'Validate artifact identity and deployment credential availability')['run']
COMMAND = step(UPLOAD, 'upload', 'Upload already-built version tagged with the exact commit SHA')['run']


class ProductionUpload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / '.output'
        (self.output / 'server').mkdir(parents=True)
        (self.output / 'public').mkdir()
        (self.output / 'server/index.mjs').write_text('export default {}')
        self.config = self.output / 'server/wrangler.json'
        self.config.write_text(json.dumps({'main': 'index.mjs', 'assets': {'directory': '../public'}}))
        self.env = {**os.environ, 'OUTPUT_DIR': '.output', 'BUILD_SCRIPT': 'cf:build',
                    'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': 'a' * 40,
                    'GITHUB_REPOSITORY': 'narduk-enterprises/example', 'GITHUB_RUN_ID': '42',
                    'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_OUTPUT': str(self.root / 'outputs'),
                    'GITHUB_EVENT_PATH': str(self.root / 'event.json')}
        (self.root / 'event.json').write_text('{}')

    def execute(self, script, cwd=None, **env):
        return subprocess.run(['bash', '-euo', 'pipefail', '-c', script], cwd=cwd or self.root,
                              env={**self.env, **env}, text=True, capture_output=True)

    def prepare(self):
        result = self.execute(PREPARE)
        self.assertEqual(result.returncode, 0, result.stderr)
        return (self.root / 'outputs').read_text().strip().split('=', 1)[1]

    def test_production_build_uses_only_fleet_ci_or_public_hosted(self):
        expression = CI['jobs']['build']['runs-on']
        definitions = CI[True]['workflow_call']['inputs']
        inputs = {key: value.get('default', '') for key, value in definitions.items()}
        inputs.update({'production-output-directory': '.output', 'runner': '\"ubuntu-latest\"'})
        overrides = {'BLACKSMITH_RUNNERS_ENABLED': 'true', 'CI_LINUX_RUNNER': '\"blacksmith-x\"'}
        for visibility, expected in ((True, json.loads(LINUX_CI)), (False, 'ubuntu-latest'),
                                     (None, 'missing-repository-visibility-do-not-run')):
            self.assertEqual(evaluate(expression, runner_context(visibility, inputs, overrides)), expected)

    def test_exact_identity_and_generated_config_survive_handoff(self):
        artifact = self.prepare()
        self.assertRegex(artifact, r'^production-a{40}-42-2-[a-f0-9]{12}$')
        result = self.execute(VERIFY, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        for key in ('GITHUB_SHA', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'):
            result = self.execute(VERIFY, self.output, **{key: 'wrong'})
            self.assertNotEqual(result.returncode, 0, key)

    def test_custom_build_or_external_paths_fail_closed(self):
        for config in ({'main': 'index.mjs', 'build': {'command': 'false'}},
                       {'main': '../../event.json'}, {'main': 'index.mjs', 'env': {'production': {}}}):
            self.config.write_text(json.dumps(config))
            self.assertNotEqual(self.execute(PREPARE).returncode, 0)
        self.config.write_text('{"main":"index.mjs"}')
        (self.output / 'external').symlink_to(self.root / 'event.json')
        self.assertNotEqual(self.execute(PREPARE).returncode, 0)

    def test_handoff_requires_real_build(self):
        self.assertNotEqual(self.execute(PREPARE, BUILD_SCRIPT='').returncode, 0)
        self.assertNotEqual(self.execute(PREPARE, OUTPUT_DIR='..').returncode, 0)

    def test_path_skip_covers_whole_push_and_defaults_unknown_paths_to_upload(self):
        def git(*args):
            return subprocess.check_output(['git', *args], cwd=self.root, text=True).strip()
        git('init', '-q')
        (self.root / 'app.js').write_text('baseline')
        git('add', 'app.js')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com', 'commit', '-qm', 'baseline')
        before = git('rev-parse', 'HEAD')
        for name in ('docs/readme.md', 'tests/a.py', 'e2e/a.ts', 'a.test.ts', 'a.spec.ts', '.github/ISSUE_TEMPLATE/a.yml'):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('test')
            git('add', name)
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com', 'commit', '-qm', 'docs and tests')
        (self.root / 'event.json').write_text(json.dumps({'before': before}))
        result = self.execute(PREPARE, GITHUB_EVENT_NAME='push', GITHUB_SHA=git('rev-parse', 'HEAD'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('path-skipped', result.stdout)
        self.assertFalse((self.root / 'outputs').exists())
        # A later docs commit must not hide an earlier source change in one push.
        (self.root / 'new.extension').write_text('source')
        git('add', 'new.extension')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com', 'commit', '-qm', 'source')
        result = self.execute(PREPARE, GITHUB_EVENT_NAME='push', GITHUB_SHA=git('rev-parse', 'HEAD'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'outputs').exists())
        self.assertNotEqual(self.execute(PREPARE, GITHUB_EVENT_NAME='push', GITHUB_SHA='b' * 40).returncode, 0)

    def test_missing_credential_warns_and_malformed_artifact_fails(self):
        artifact = self.prepare()
        result = self.execute(ARM, ARTIFACT=artifact, CLOUDFLARE_API_TOKEN='', CLOUDFLARE_ACCOUNT_ID='')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('::warning::', result.stdout)
        self.assertIn('armed=false', (self.root / 'outputs').read_text())
        for name in ('../../escape', artifact.replace('-42-', '-43-'), artifact + 'bad'):
            self.assertNotEqual(self.execute(ARM, ARTIFACT=name).returncode, 0)

    def test_upload_only_tags_exact_sha_and_never_builds(self):
        binary = self.root / 'wrangler'
        binary.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGUMENTS"\n')
        binary.chmod(0o755)
        result = self.execute(COMMAND, PATH=f'{self.root}:{os.environ["PATH"]}', ARGUMENTS=str(self.root / 'args'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'args').read_text().splitlines(),
                         ['versions', 'upload', '--config', 'server/wrangler.json', '--no-bundle',
                          '--tag', 'a' * 40, '--message', 'Runner upload ' + 'a' * 40])
        upload = UPLOAD['jobs']['upload']
        self.assertNotIn('checkout', json.dumps(upload))
        self.assertIn('proxmox-deploy', upload['runs-on'])
        self.assertIn('ubuntu-latest', upload['runs-on'])
        self.assertIn("github.event.repository.full_name == github.repository", upload['if'])
        self.assertIs(upload['concurrency']['cancel-in-progress'], True)
        self.assertEqual(CI[True]['workflow_call']['inputs']['production-output-directory']['default'], '')


if __name__ == '__main__':
    unittest.main()
