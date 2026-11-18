#!/usr/bin/env python3
"""One job that checks the source: choose the checks, run them side by side, print one timed line each.

  plan         A pull request. Read the changed files and the repository's gate file, choose the checks, write the
               plan to $RUNNER_TEMP/verify-plan.json.
  plan --all   Any other event. The whole list (`default` in the gate file).
  run          Run the planned checks side by side, keep going past a red one, print one timed line each.
               Exit 1 when any check failed.
  select       Read changed file names on stdin; print the checks `plan` would choose for them.

The gate file is `Config/remote-gate.json`, the file `remote-gate --changed` reads on a developer's machine
(agent-infrastructure scripts/remote_gate.py: `glob_match`, `select_changed`). CI and the desk read one mapping, so a
change that is green locally runs the same checks here. The file is the source of truth; this is its second reader,
and the two readings must stay identical.

Environment: VERIFY_CONFIG (the gate file, relative to the workspace), VERIFY_ALONE (names of checks that run one at
a time after the others, in this order: a build or a second typecheck whose memory must not stack on the rest),
VERIFY_BASE (the commit a pull request is compared with; HEAD^1 is the base branch on a merge commit).
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(os.environ.get('GITHUB_WORKSPACE') or os.getcwd())
CONFIG = ROOT / os.environ.get('VERIFY_CONFIG', 'Config/remote-gate.json')
PLAN = Path(os.environ.get('RUNNER_TEMP', tempfile.gettempdir())) / 'verify-plan.json'
BASE = os.environ.get('VERIFY_BASE') or 'HEAD^1'
ALONE = tuple(os.environ.get('VERIFY_ALONE', '').split())


def glob_match(pattern: str, path: str, fold: bool = False) -> bool:
    """Repo-root-anchored glob: `*` and `?` stay inside a directory, `**/` spans directories."""
    out, i = '', 0
    while i < len(pattern):
        if pattern.startswith('**/', i):
            out, i = out + '(?:.*/)?', i + 3
        elif pattern.startswith('**', i):
            out, i = out + '.*', i + 2
        elif pattern[i] == '*':
            out, i = out + '[^/]*', i + 1
        elif pattern[i] == '?':
            out, i = out + '[^/]', i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.fullmatch(out, path, re.IGNORECASE if fold else 0) is not None


def changed_files() -> list[str]:
    """What this pull request proposes: parent 1 of the merge commit is the base branch.
    `-z`: without it git quotes and escapes a name that has a non-ASCII character, a tab or a quote in it
    (`"src/caf\\303\\251.ts"`), no rule matches the quoted text, and that file's checks are silently not chosen."""
    out = subprocess.run(
        ['git', 'diff', '--no-renames', '--name-only', '-z', BASE, 'HEAD'],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout
    return [f for f in out.split('\0') if f]


def select_changed(spec: dict, checks: dict, files: list[str]) -> list[tuple[str, str]]:
    picked: dict[str, list[str]] = {}
    for rule in spec['rules']:
        hit = [f for f in files if any(glob_match(g, f) for g in rule['paths'])]
        for name in rule['checks'] if hit else []:
            picked.setdefault(name, [])
            picked[name] += [f for f in hit if f not in picked[name]]
    full = spec.get('full')
    if full and any(glob_match(g, f) for f in files for g in full['paths']):
        picked = {full['swap'].get(n, n): fs for n, fs in picked.items()}
    for name in spec.get('always', []):
        picked.setdefault(name, [])
    plan = []
    for name, hit in picked.items():
        command = checks[name]
        if '{files}' in command:
            # A deleted file is a change, but there is nothing for a linter to open. `./` so a file named `--something` is never read as a flag.
            present = [f for f in (hit or files) if (ROOT / f).exists()]
            if not present:
                continue
            command = command.replace('{files}', ' '.join(shlex.quote('./' + f) for f in present))
        plan.append((name, command.replace('{base}', BASE)))
    return plan


def summary(text: str) -> None:
    print(text)
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a') as fh:
            fh.write(text + '\n')


def plan(*flags: str) -> int:
    config = json.loads(CONFIG.read_text())
    unknown = [name for name in ALONE if name not in config['checks']]
    if unknown:
        sys.exit(f'verify: `alone` names no check in {CONFIG.name}: {", ".join(unknown)}')
    if '--all' in flags:
        names = config['default']
        chosen = [(name, config['checks'][name]) for name in names]
        summary('Every check (not a pull request); verify runs: ' + ', '.join(names))
    else:
        files = changed_files()
        chosen = select_changed(config['changed'], config['checks'], files)
        summary(f'{len(files)} changed file(s) vs {BASE}; verify runs: ' + ', '.join(n for n, _ in chosen))
    PLAN.write_text(json.dumps({'checks': chosen}))
    return 0


def stages(chosen: list) -> list:
    """Checks that share nothing run side by side, so the job is as long as its slowest check, not their sum.
    The ALONE ones follow, one at a time, in ALONE's order."""
    together = [c for c in chosen if c[0] not in ALONE]
    alone = sorted((c for c in chosen if c[0] in ALONE), key=lambda c: ALONE.index(c[0]))
    return ([together] if together else []) + [[c] for c in alone]


def run_stage(stage: list) -> list:
    """[(name, code, seconds)] for one stage, in the plan's order. Each check's log is printed as one group when the
    stage ends, and each check's seconds are its own (start of the stage to its own exit), not the slowest one's."""
    started = time.monotonic()
    procs = []
    for name, command in stage:
        log = tempfile.TemporaryFile(mode='w+')
        procs.append((name, command, log, subprocess.Popen(command, cwd=ROOT, shell=True, stdout=log, stderr=subprocess.STDOUT)))
    took: dict[str, float] = {}
    while len(took) < len(procs):
        for name, _command, _log, proc in procs:
            if name not in took and proc.poll() is not None:
                took[name] = time.monotonic() - started
        time.sleep(0.2)
    results = []
    for name, command, log, proc in procs:
        log.seek(0)
        print(f'::group::{name}: {command}', flush=True)
        shutil.copyfileobj(log, sys.stdout)
        print('::endgroup::', flush=True)
        results.append((name, proc.returncode, took[name]))
    return results


def run() -> int:
    chosen = json.loads(PLAN.read_text())['checks']
    began = time.monotonic()
    rows, failed = [], []
    for stage in stages(chosen):
        for name, code, took in run_stage(stage):
            rows.append((name, 'pass' if code == 0 else f'FAIL({code})', took))
            if code != 0:
                failed.append(name)
    summary('\n'.join(['| check | result | wall |', '| --- | --- | --- |'] + [f'| {n} | {r} | {t:.1f} s |' for n, r, t in rows]))
    summary(f'verify checks total: {time.monotonic() - began:.1f} s')
    if failed:
        print(f'::error::verify failed: {", ".join(failed)}')
        return 1
    return 0


def select() -> int:
    config = json.loads(CONFIG.read_text())
    files = [f for f in sys.stdin.read().splitlines() if f]
    for name, _ in select_changed(config['changed'], config['checks'], files):
        print(name)
    return 0


COMMANDS = {'plan': plan, 'run': run, 'select': select}

if __name__ == '__main__':
    command, flags = (sys.argv[1] if len(sys.argv) > 1 else ''), sys.argv[2:]
    if command not in COMMANDS or (flags and (command != 'plan' or flags != ['--all'])):
        sys.exit(f'usage: verify.py {{{"|".join(COMMANDS)}}}; plan takes --all')
    sys.exit(COMMANDS[command](*flags))
