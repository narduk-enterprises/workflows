#!/usr/bin/env python3
"""Contract checks for the opt-in node-library package batching surface."""
from pathlib import Path
import yaml

WF = Path('.github/workflows/node-library.yml')

def main() -> int:
    doc = yaml.safe_load(WF.read_text())
    trigger = doc.get('on', doc.get(True))
    inputs = trigger['workflow_call']['inputs']
    jobs = doc['jobs']
    checks = [
        ('package-batch-size' in inputs, 'batch size input'),
        (inputs['package-batch-size']['default'] > 0, 'positive batch default'),
        (inputs['package-batches']['default'] == '', 'opt-in default'),
        (jobs['package'].get('if') == "inputs.package-batches == ''", 'legacy path gated'),
        (jobs['package-batch'].get('if') == "inputs.package-batches != ''", 'batch path gated'),
        (jobs['validate-package-batches'].get('if') == "inputs.package-batches != ''", 'batch input validation gated'),
        (any(s.get('name') == 'Reject an empty or malformed batch set' for s in jobs['validate-package-batches']['steps']), 'empty batch guard'),
        ('validate-package-batches' in jobs['package-batch']['needs'], 'batch waits for validation'),
        ('package-batch' in jobs['required']['needs'], 'required observes batches'),
        ('validate-package-batches' in jobs['required']['needs'], 'required observes validation'),
        (any(s.get('name') == 'Install dependencies (pnpm)' for s in jobs['package-batch']['steps']), 'one batch install'),
        (any(s.get('name') == 'Run package batch' for s in jobs['package-batch']['steps']), 'attributable package runner'),
    ]
    failed = [name for ok, name in checks if not ok]
    for ok, name in checks:
        print(('PASS  ' if ok else 'FAIL  ') + name)
    return 1 if failed else 0

if __name__ == '__main__':
    raise SystemExit(main())
