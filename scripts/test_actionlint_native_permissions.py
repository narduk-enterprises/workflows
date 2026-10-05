"""Exercise the actual compatibility binary; no permission is stripped or ignored."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import sys

import yaml


class ShippedInstaller(unittest.TestCase):
    def setUp(self):
        self.source = Path('scripts/install-actionlint-native-alerts.sh').read_bytes()
        workflow = yaml.safe_load(Path('.github/workflows/nuxt-cloudflare.yml').read_text())
        self.installs = [step for job in workflow['jobs'].values()
                         for step in job.get('steps', [])
                         if step.get('name') == 'Install actionlint']

    def test_all_callers_pin_the_same_installer_as_own_ci(self):
        self.assertEqual(len(self.installs), 3)
        for step in self.installs:
            self.assertEqual(step['run'], self.installs[0]['run'])
            self.assertEqual(step['env'], self.installs[0]['env'])
            self.assertRegex(step['env']['ACTIONLINT_INSTALLER_SHA'], r'^[a-f0-9]{40}$')
            self.assertEqual(step['env']['ACTIONLINT_INSTALLER_SHA256'],
                             hashlib.sha256(self.source).hexdigest())
            self.assertIn('https://raw.githubusercontent.com/narduk-enterprises/workflows/${ACTIONLINT_INSTALLER_SHA}/scripts/install-actionlint-native-alerts.sh', step['run'])
        own = yaml.safe_load(Path('.github/workflows/ci.yml').read_text())
        steps = {step.get('name'): step for step in own['jobs']['validate']['steps']}
        self.assertEqual(steps['Install actionlint']['run'],
                         'bash scripts/install-actionlint-native-alerts.sh ./actionlint')
        self.assertEqual(steps['Native alert permission validator tests']['run'],
                         'python3 scripts/test_actionlint_native_permissions.py')

    def test_shipped_checksum_gate_refuses_tampered_installer(self):
        step = self.installs[0]
        for tampered in (False, True):
            with self.subTest(tampered=tampered), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                payload = root / 'payload'
                payload.write_bytes(self.source + (b'\n# tampered\n' if tampered else b''))
                curl = root / 'curl'
                curl.write_text(f'#!{sys.executable}\nimport os,shutil,sys\nshutil.copyfile(os.environ["PAYLOAD"], sys.argv[sys.argv.index("-o")+1])\n')
                curl.chmod(0o755)
                bash = root / 'bash'
                bash.write_text(f'#!{sys.executable}\nprint("VERIFIED_INSTALLER_EXECUTED")\n')
                bash.chmod(0o755)
                result = subprocess.run(['/bin/bash', '-c', step['run']], text=True,
                                        capture_output=True, env=os.environ | step['env'] | {
                                            'RUNNER_TEMP': str(root), 'PAYLOAD': str(payload),
                                            'PATH': f'{root}:{os.environ["PATH"]}'})
                self.assertEqual(result.returncode == 0, not tampered, result.stdout + result.stderr)
                self.assertEqual('VERIFIED_INSTALLER_EXECUTED' in result.stdout, not tampered)


class NativeAlertPermissions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = str(Path(os.environ.get("ACTIONLINT_BIN", "./actionlint")).resolve())
        if not Path(cls.binary).is_file():
            raise AssertionError("build the actual compatibility binary before these tests")
        if not shutil.which("shellcheck"):
            raise AssertionError("shellcheck is required; do not silently skip shell validation")

    def lint(self, permission, value, job_scope=False, script="echo ok"):
        scope = "" if job_scope else f"permissions:\n  {permission}: {value}\n"
        job = f"    permissions:\n      {permission}: {value}\n" if job_scope else ""
        workflow = f"name: Fixture\non: push\n{scope}jobs:\n  fixture:\n{job}    runs-on: ubuntu-latest\n    steps:\n      - run: {script}\n"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "fixture.yml")
            path.write_text(workflow)
            return subprocess.run([self.binary, "-no-color", str(path)], capture_output=True, text=True)

    def test_read_and_none_supported_at_both_levels(self):
        for job_scope in (False, True):
            for value in ("read", "none"):
                with self.subTest(job_scope=job_scope, value=value):
                    result = self.lint("vulnerability-alerts", value, job_scope)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_write_is_refused_at_both_levels(self):
        for job_scope in (False, True):
            result = self.lint("vulnerability-alerts", "write", job_scope)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('invalid as permission of scope "vulnerability-alerts"', result.stdout)

    def test_unknown_scope_still_refused(self):
        result = self.lint("invented-alerts", "read")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unknown permission scope "invented-alerts"', result.stdout)

    def test_other_invalid_permission_still_refused(self):
        result = self.lint("id-token", "read")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('invalid as permission of scope "id-token"', result.stdout)

    def test_shellcheck_still_enforced(self):
        result = self.lint("vulnerability-alerts", "read", script="echo $unquoted")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("shellcheck", result.stdout)


if __name__ == "__main__":
    unittest.main()
