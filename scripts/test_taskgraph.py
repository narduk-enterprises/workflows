#!/usr/bin/env python3
"""Execute the taskgraph engine that taskgraph.yml ships, and hold its copies.

taskgraph.yml writes its engine from its own text (the `Install taskgraph
engine` step). This test runs that exact step under bash, then drives the
module it wrote with Node:

  - keys: what changes a key and what must not; diff-scoped scripts refused;
  - the graph: "ci" tags pull dependencies in, cycles and missing scripts
    refused, outputs pull a dependency into a miss's run;
  - execution: dependency order, the CPU budget, a failure blocking only its
    dependents, one worktree per task with node_modules linked, install leftovers
    copied and dependency outputs carried in;
  - proofs: a pointer is honoured only when GitHub confirms the named attempt
    ran `Publish task proofs <digest>` successfully in its Required job, for
    this repository, this caller workflow, and a manifest holding the key;
  - the steps: `Plan tasks` against a fake GitHub API and store (all hit -> no
    run; no credentials -> notice and all run), `Run tasks` writing the digest
    the publish step names, `Publish task proofs` writing only cacheable passes.

It also holds taskgraph.yml to the steps it copies from nuxt-cloudflare.yml
(store client, registry auth, install and caller lint) byte for byte, so a fix
there cannot silently miss here.

Run: python3 scripts/test_taskgraph.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

import yaml

WORKFLOW = Path(".github/workflows/taskgraph.yml")
NUXT = Path(".github/workflows/nuxt-cloudflare.yml")
ENGINE_STEP = "Install taskgraph engine"
STORE_STEP = "Install CI artifact store client"


def steps(doc: dict, job: str) -> list[dict]:
    return doc["jobs"][job]["steps"]


def step(doc: dict, job: str, name: str) -> dict:
    return next(s for s in steps(doc, job) if s.get("name") == name)


def install(doc: dict, name: str, runner_temp: Path) -> None:
    subprocess.run(["bash", "-c", step(doc, "plan", name)["run"]], check=True,
                   env={**os.environ, "RUNNER_TEMP": str(runner_temp)}, cwd=runner_temp)


HARNESS = r"""
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const tg = require(process.argv[2]);
const store = require(process.argv[3]);
const tmp = process.argv[4];
const results = [];
const test = async (name, fn) => { await fn(); results.push(name); console.log(`ok  ${name}`); };

const entries = (files) => Object.entries(files).map(([p, sha]) => ({ path: p, mode: '100644', sha })).sort((a, b) => (a.path < b.path ? -1 : 1));
const base = { 'package.json': 'a1', 'pnpm-lock.yaml': 'b1', 'src/app.ts': 'c1', 'docs/guide.md': 'd1', 'tests/a.test.ts': 'e1' };
const pkg = s => JSON.stringify({ packageManager: 'pnpm@10.0.0', scripts: s });
const scripts = { lint: 'eslint .', typecheck: 'tsc', test: 'vitest run', build: 'nuxt build', prepare: 'nuxt prepare', format: 'prettier --check src' };
const turbo = JSON.stringify({ tasks: {
  prepare: { outputs: ['.nuxt/**'] },
  lint: { tags: ['ci'], dependsOn: ['prepare'], inputs: ['src/**', '!docs/**'] },
  typecheck: { tags: ['ci'], dependsOn: ['prepare'] },
  test: { tags: ['ci'], dependsOn: ['build'] },
  build: { outputs: ['.output/**'], dependsOn: ['prepare'] },
  format: { tags: ['ci'], inputs: ['src/**', '!src/**/*.md'] },
} });
const planOf = (over = {}) => tg.plan({ entries: entries(over.files || base), turboText: over.turbo || turbo, packageText: over.pkg || pkg(over.scripts || scripts), nodeVersionText: over.node ?? '24', context: over.context || { callerWorkflow: '.github/workflows/ci.yml:f1' } });
const keyOf = (p, t) => p.tasks.find(x => x.task === t).key;

(async () => {
  await test('jsonc: comments and trailing commas parse', () => {
    assert.deepEqual(tg.parseJsonc('{\n // c\n "a": "//not", /* b */ "b": [1,2,],\n}'), { a: '//not', b: [1, 2] });
  });

  await test('globs: ** crosses directories, * does not, a directory selects its contents', () => {
    assert.ok(tg.globToRegExp('src/**').test('src/a/b.ts'));
    assert.ok(tg.globToRegExp('src').test('src/a.ts'));
    assert.ok(tg.globToRegExp('**/*.md').test('docs/x/y.md'));
    assert.ok(tg.globToRegExp('**/*.md').test('y.md'));
    assert.ok(!tg.globToRegExp('src/*.ts').test('src/a/b.ts'));
    assert.ok(tg.globToRegExp('*.{ts,vue}').test('a.vue'));
  });

  await test('selection: ci tags pull dependencies in, dependencies first', () => {
    const p = planOf();
    assert.deepEqual(p.tasks.map(t => t.task), ['prepare', 'lint', 'typecheck', 'build', 'test', 'format']);
    assert.deepEqual(p.tasks.filter(t => t.tagged).map(t => t.task), ['lint', 'typecheck', 'test', 'format']);
  });

  await test('selection: !ci wins, ^deps are ignored, pkg#task and cycles are refused', () => {
    const p = planOf({ turbo: JSON.stringify({ tasks: { lint: { tags: ['ci', '!ci'] }, test: { tags: ['ci'], dependsOn: ['^build'] } } }) });
    assert.deepEqual(p.tasks.map(t => t.task), ['test']);
    assert.throws(() => planOf({ turbo: JSON.stringify({ tasks: { 'web#test': { tags: ['ci'] } } }) }), /turbo workspace/);
    assert.throws(() => planOf({ turbo: JSON.stringify({ tasks: { lint: { tags: ['ci'], dependsOn: ['test'] }, test: { dependsOn: ['lint'] } } }) }), /cycle/);
    assert.throws(() => planOf({ turbo: JSON.stringify({ tasks: { lint: {} } }) }), /tags no task/);
    assert.throws(() => tg.plan({ entries: entries(base), turboText: null, packageText: pkg(scripts), nodeVersionText: '24', context: {} }), /no turbo.json/);
  });

  await test('selection: a task with no script, a diff-scoped script, an output with no directory are refused', () => {
    assert.throws(() => planOf({ scripts: { ...scripts, typecheck: undefined } }), /no package.json script/);
    assert.throws(() => planOf({ scripts: { ...scripts, test: 'vitest run --changed=$FAST_BASE_REF' } }), /diff-scoped/);
    assert.throws(() => planOf({ turbo: JSON.stringify({ tasks: { 'test:changed': { tags: ['ci'] } } }), scripts: { 'test:changed': 'vitest' } }), /diff-scoped/);
    assert.ok(planOf({ turbo: JSON.stringify({ tasks: { 'test:changed': { tags: ['ci'], cache: false } } }), scripts: { 'test:changed': 'vitest' } }));
    assert.throws(() => planOf({ turbo: JSON.stringify({ tasks: { lint: { tags: ['ci'], outputs: ['**/*.d.ts'] } } }) }), /names no directory/);
  });

  await test('selection: inputs that select no file, unknown turbo tokens and diff-scoped tools are refused', () => {
    const only = inputs => planOf({ turbo: JSON.stringify({ tasks: { lint: { tags: ['ci'], inputs } } }) });
    assert.throws(() => only(['scr/**']), /select no tracked file/);
    assert.throws(() => only(['!README.md']), /select no tracked file/);
    assert.throws(() => only(['$TURBO_ROOT$/tsconfig.json']), /only \$TURBO_DEFAULT\$/);
    assert.ok(only(['src/**', '!src/**/*.md']));
    const scoped = script => planOf({ scripts: { ...scripts, test: script } });
    for (const script of ['jest --changedSince=origin/main', 'jest --onlyChanged', 'eslint $(git diff --name-only origin/main)',
      'node x.mjs $(git merge-base HEAD origin/main)', 'nx affected -t test', 'vitest --changed', 'pnpm run test:changed', 'echo $GITHUB_BASE_REF']) {
      assert.throws(() => scoped(script), /diff-scoped/, script);
    }
    assert.ok(scoped('node tools:changed-files-report.mjs && vitest run'), 'a name merely containing :changed is fine');
  });

  await test('keys: stable for the same content', () => {
    assert.deepEqual(planOf(), planOf());
    assert.match(keyOf(planOf(), 'lint'), /^[0-9a-f]{64}$/);
  });

  await test('keys: a file outside a task\'s inputs leaves it alone; a dependency\'s change reaches it', () => {
    const before = planOf();
    const docs = planOf({ files: { ...base, 'docs/guide.md': 'd2' } });
    assert.equal(keyOf(docs, 'format'), keyOf(before, 'format'));
    assert.notEqual(keyOf(docs, 'typecheck'), keyOf(before, 'typecheck'));
    // lint's own inputs exclude docs, but it depends on prepare, which reads every file
    assert.notEqual(keyOf(docs, 'lint'), keyOf(before, 'lint'));
    assert.notEqual(keyOf(planOf({ files: { ...base, 'src/app.ts': 'c2' } }), 'format'), keyOf(before, 'format'));
    assert.equal(keyOf(planOf({ files: { ...base, 'src/notes.md': 'n1' } }), 'format'), keyOf(before, 'format'));
  });

  await test('keys: script text, a pre hook, the lockfile, Node, the caller workflow and a dependency each change a key', () => {
    const k = keyOf(planOf(), 'test');
    assert.notEqual(keyOf(planOf({ scripts: { ...scripts, test: 'vitest run --coverage' } }), 'test'), k);
    assert.notEqual(keyOf(planOf({ scripts: { ...scripts, pretest: 'echo hi' } }), 'test'), k);
    assert.notEqual(keyOf(planOf({ files: { ...base, 'pnpm-lock.yaml': 'b2' } }), 'test'), k);
    assert.notEqual(keyOf(planOf({ node: '22' }), 'test'), k);
    assert.notEqual(keyOf(planOf({ context: { callerWorkflow: '.github/workflows/ci.yml:f2' } }), 'test'), k);
    // build's script feeds test through the dependency key
    assert.notEqual(keyOf(planOf({ scripts: { ...scripts, build: 'nuxt build --prerender' } }), 'test'), k);
    // lint's own script does not
    assert.equal(keyOf(planOf({ scripts: { ...scripts, lint: 'eslint --fix .' } }), 'test'), k);
  });

  await test('execution set: a miss pulls in dependencies with outputs, not passed ones without', () => {
    const p = planOf();
    assert.deepEqual(tg.executionSet(p.tasks, new Set(['test'])), ['prepare', 'build', 'test']);
    assert.deepEqual(tg.executionSet(p.tasks, new Set(['lint'])), ['prepare', 'lint']);
    const noOut = tg.plan({ entries: entries(base), turboText: JSON.stringify({ tasks: { a: {}, b: { tags: ['ci'], dependsOn: ['a'] } } }), packageText: pkg({ a: 'x', b: 'y' }), nodeVersionText: '24', context: {} });
    assert.deepEqual(tg.executionSet(noOut.tasks, new Set(['b'])), ['b']);
    // x misses, y passed and has no outputs, z behind it has outputs: z is rebuilt, y is not re-run.
    const chain = tg.plan({ entries: entries(base), turboText: JSON.stringify({ tasks: { z: { outputs: ['dist/**'] }, y: { dependsOn: ['z'] }, x: { tags: ['ci'], dependsOn: ['y'] } } }), packageText: pkg({ x: 'a', y: 'b', z: 'c' }), nodeVersionText: '24', context: {} });
    assert.deepEqual(tg.executionSet(chain.tasks, new Set(['x'])), ['z', 'x']);
  });

  await test('legs: untagged misses share one leg; a ci:machine task takes its readers to its own', () => {
    const t = JSON.stringify({ tasks: {
      prepare: { outputs: ['.nuxt/**'] },
      build: { tags: ['ci', 'ci:machine'], outputs: ['.output/**'], dependsOn: ['prepare'] },
      wrangler: { tags: ['ci'], dependsOn: ['build'] },
      lint: { tags: ['ci', 'ci:machine'], dependsOn: ['prepare'] },
      format: { tags: ['ci'] },
      guard: { tags: ['ci'] },
    } });
    const p = tg.plan({ entries: entries(base), turboText: t, packageText: pkg({ prepare: 'p', build: 'b', wrangler: 'w', lint: 'l', format: 'f', guard: 'g' }), nodeVersionText: '24', context: {} });
    const all = tg.executionSet(p.tasks, new Set(p.tasks.map(x => x.task)));
    assert.deepEqual(tg.legsOf(p.tasks, all), [
      { id: 'build', names: 'prepare,build,wrangler' },
      { id: 'lint', names: 'prepare,lint' },
      { id: 'shared', names: 'format,guard' },
    ]);
    // No machine tags: one leg for everything, as one seat.
    const flat = planOf();
    assert.deepEqual(tg.legsOf(flat.tasks, tg.executionSet(flat.tasks, new Set(['lint', 'format']))), [{ id: 'all', names: 'prepare,lint,format' }]);
    // Only the misses: a passed build's leg does not exist.
    assert.deepEqual(tg.legsOf(p.tasks, tg.executionSet(p.tasks, new Set(['guard']))), [{ id: 'all', names: 'guard' }]);
  });

  await test('legs: a task behind two machine tasks, or a machine task behind another, is refused', () => {
    const two = JSON.stringify({ tasks: { a: { tags: ['ci', 'ci:machine'] }, b: { tags: ['ci', 'ci:machine'] }, c: { tags: ['ci'], dependsOn: ['a', 'b'] } } });
    const byHand = [{ task: 'a', dependsOn: [], machine: true }, { task: 'b', dependsOn: [], machine: true }, { task: 'c', dependsOn: ['a', 'b'], machine: false }];
    assert.throws(() => tg.legsOf(byHand, ['a', 'b', 'c']), /only one machine/);
    const chain = JSON.stringify({ tasks: { a: { tags: ['ci', 'ci:machine'] }, b: { tags: ['ci', 'ci:machine'], dependsOn: ['a'] } } });
    assert.throws(() => tg.plan({ entries: entries(base), turboText: chain, packageText: pkg({ a: 'a', b: 'b' }), nodeVersionText: '24', context: {} }), /depends on ci:machine task a/);
    // At plan time, whichever of them would hit: a cached machine task must not hide the conflict.
    assert.throws(() => tg.plan({ entries: entries(base), turboText: two, packageText: pkg({ a: 'a', b: 'b', c: 'c' }), nodeVersionText: '24', context: {} }), /needs ci:machine tasks a and b/);
    const named = JSON.stringify({ tasks: { shared: { tags: ['ci', 'ci:machine'] }, x: { tags: ['ci'] } } });
    assert.throws(() => tg.plan({ entries: entries(base), turboText: named, packageText: pkg({ shared: 's', x: 'x' }), nodeVersionText: '24', context: {} }), /share its leg's name/);
  });

  await test('concurrency: cgroup limits win over host cores and memory', () => {
    const limits = tg.cgroupLimits(f => (f.endsWith('cpu.max') ? '400000 100000\n' : f.endsWith('memory.high') ? `${6 * 1024 ** 3}\n` : `${8 * 1024 ** 3}\n`));
    assert.deepEqual(limits, { cpus: 4, memory: 6 * 1024 ** 3 }, 'memory.high, where it throttles, is the limit');
    assert.equal(tg.defaultConcurrency({ cpus: 4, memory: 6 * 1024 ** 3 }), 2, '3 GiB a slot');
    assert.equal(tg.defaultConcurrency({ cpus: 64, memory: 2 * 1024 ** 3 }), 1);
    assert.deepEqual(tg.cgroupLimits(() => 'max\n'), { cpus: Infinity, memory: Infinity });
  });

  // A real repository for execution: tasks are shell scripts run by `npm run`.
  const repo = path.join(tmp, 'repo');
  fs.mkdirSync(path.join(repo, 'scripts'), { recursive: true });
  const git = (...args) => execFileSync('git', ['-C', repo, ...args], { encoding: 'utf8' });
  git('init', '-q');
  const sh = (name, body) => fs.writeFileSync(path.join(repo, 'scripts', name), `set -eu\n${body}\n`);
  sh('prepare.sh', 'mkdir -p .nuxt && echo prepared > .nuxt/marker');
  sh('slow.sh', 'mkdir -p "$1" && node -p "Date.now()" > "$1/start" && sleep 1 && node -p "Date.now()" > "$1/end" && echo "$TASKGRAPH_TASK ok"');
  sh('needs-prepare.sh', 'test "$(cat .nuxt/marker)" = prepared && test -f node_modules/dep/index.js && test -f generated.txt && echo shared > shared.txt');
  sh('fail.sh', 'echo boom >&2; exit 3');
  sh('linger.sh', 'sleep 60 & echo started');
  sh('hang.sh', 'echo waiting; sleep 60');
  fs.writeFileSync(path.join(repo, 'package.json'), pkg({
    prepare: 'sh scripts/prepare.sh', a: 'sh scripts/slow.sh out-a', b: 'sh scripts/slow.sh out-b', c: 'sh scripts/slow.sh out-c',
    uses: 'sh scripts/needs-prepare.sh', fails: 'sh scripts/fail.sh', after: 'echo never', linger: 'sh scripts/linger.sh', hang: 'sh scripts/hang.sh',
  }));
  fs.writeFileSync(path.join(repo, '.gitignore'), 'node_modules/\n.nuxt/\ngenerated.txt\nout-*/\nshared.txt\n');
  git('add', '-A');
  git('-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'init');
  // What an install leaves: node_modules and a generated file.
  fs.mkdirSync(path.join(repo, 'node_modules', 'dep'), { recursive: true });
  fs.writeFileSync(path.join(repo, 'node_modules', 'dep', 'index.js'), '');
  fs.writeFileSync(path.join(repo, 'generated.txt'), 'gen');
  const graph = tasks => ({ tasks: Object.entries(tasks).map(([task, t]) => ({ task, key: tg.sha256(task), dependsOn: t.dependsOn || [], outputs: t.outputs || [], cache: t.cache !== false, cpus: t.cpus || 1, tagged: true, inputFiles: 0 })) });
  const run = (g, names, opts = {}) => tg.runTasks({ planTasks: g.tasks, names, command: n => ['npm', 'run', '--silent', n], cwd: repo, concurrency: opts.concurrency || 4, isolate: opts.isolate === false ? null : path.join(tmp, `iso-${results.length}`), restored: opts.restored, env: process.env, onEvent: opts.onEvent, timeoutMs: opts.timeoutMs });

  await test('run: independent tasks overlap within the CPU budget, each in its own worktree', async () => {
    const g = graph({ a: {}, b: {}, c: { cpus: 2 } });
    const cwds = {};
    const r = await run(g, ['a', 'b', 'c'], { concurrency: 2, onEvent: e => { if (e.type === 'start') cwds[e.task] = e.cwd; } });
    assert.deepEqual(Object.values(r).map(x => x.status), ['passed', 'passed', 'passed']);
    const t = n => ({ s: BigInt(fs.readFileSync(path.join(cwds[n], `out-${n}`, 'start'), 'utf8')), e: BigInt(fs.readFileSync(path.join(cwds[n], `out-${n}`, 'end'), 'utf8')) });
    assert.ok(t('a').s < t('b').e && t('b').s < t('a').e, 'a and b should overlap');
    assert.ok(t('c').s >= t('a').e && t('c').s >= t('b').e, 'c (2 CPUs) waits for both slots');
    assert.equal(new Set(Object.values(cwds)).size, 3);
    assert.ok(!fs.existsSync(path.join(repo, 'out-a')), 'the base checkout is untouched');
  });

  await test('run: dependency outputs, node_modules and install leftovers reach a dependent\'s worktree', async () => {
    const g = graph({ prepare: { outputs: ['.nuxt/**'] }, uses: { dependsOn: ['prepare'] } });
    const cwds = {};
    const r = await run(g, ['prepare', 'uses'], { onEvent: e => { if (e.type === 'start') cwds[e.task] = e.cwd; } });
    assert.equal(r.uses.status, 'passed', JSON.stringify(r));
    assert.ok(fs.lstatSync(path.join(cwds.uses, 'node_modules')).isSymbolicLink());
    assert.ok(!fs.existsSync(path.join(repo, 'shared.txt')));
  });

  await test('run: a dependent waits for a running task behind a passed one, and gets its outputs', async () => {
    // uses -> middle (passed, not run) -> prepare (outputs, runs): uses must start after prepare ends.
    const g = graph({ prepare: { outputs: ['.nuxt/**'] }, middle: { dependsOn: ['prepare'] }, uses: { dependsOn: ['middle'] } });
    const r = await run(g, ['prepare', 'uses']);
    assert.equal(r.prepare.status, 'passed');
    assert.equal(r.uses.status, 'passed', JSON.stringify(r));
  });

  await test('run: a failure blocks its dependents and nothing else', async () => {
    const g = graph({ fails: {}, after: { dependsOn: ['fails'] }, a: {} });
    const outputs = {};
    const r = await run(g, ['fails', 'after', 'a'], { onEvent: e => { if (e.type === 'end') outputs[e.task] = e.output; } });
    assert.equal(r.fails.status, 'failed');
    assert.equal(r.fails.exitCode, 3);
    assert.match(outputs.fails, /boom/);
    assert.equal(r.after.status, 'blocked');
    assert.equal(r.a.status, 'passed');
  });

  await test('run: a ci:shared task runs in the checkout itself; one reading outputs is refused', async () => {
    const g = graph({ a: {} });
    g.tasks[0].shared = true;
    const cwds = {};
    const r = await run(g, ['a'], { onEvent: e => { if (e.type === 'start') cwds[e.task] = e.cwd; } });
    assert.equal(r.a.status, 'passed');
    assert.equal(cwds.a, repo);
    fs.rmSync(path.join(repo, 'out-a'), { recursive: true });
    assert.throws(() => planOf({ turbo: JSON.stringify({ tasks: { build: { outputs: ['.output/**'] }, test: { tags: ['ci', 'ci:shared'], dependsOn: ['build'] } } }) }), /ci:shared but depends on build/);
    // A producer behind a no-output task is still a producer.
    assert.throws(() => planOf({ turbo: JSON.stringify({ tasks: { build: { outputs: ['.output/**'] }, prepare: { dependsOn: ['build'] }, test: { tags: ['ci', 'ci:shared'], dependsOn: ['prepare'] } } }) }), /ci:shared but depends on build/);
    assert.equal(planOf({ turbo: JSON.stringify({ tasks: { lint: { tags: ['ci', 'ci:shared'] } } }) }).tasks[0].shared, true);
  });

  await test('run: a task is over when its process exits, even if a leftover holds its output open', async () => {
    const g = graph({ linger: {} });
    const t0 = Date.now();
    const outputs = {};
    const r = await run(g, ['linger'], { onEvent: e => { if (e.type === 'end') outputs[e.task] = e.output; } });
    assert.equal(r.linger.status, 'passed');
    assert.ok(Date.now() - t0 < 20000, `took ${Date.now() - t0} ms`);
    assert.match(outputs.linger, /started/);
  });

  await test('run: a task past its time budget is stopped and fails; the rest still report', async () => {
    const g = graph({ hang: {}, after: {} });
    const t0 = Date.now();
    const outputs = {};
    const r = await run(g, ['hang', 'after'], { timeoutMs: 1500, onEvent: e => { if (e.type === 'end') outputs[e.task] = e.output; } });
    assert.equal(r.hang.status, 'failed');
    assert.equal(r.hang.signal, 'timeout');
    assert.match(outputs.hang, /was stopped/);
    assert.equal(r.after.status, 'passed', outputs.after);
    assert.ok(Date.now() - t0 < 20000, `took ${Date.now() - t0} ms`);
  });

  await test('run: task names that sanitise alike still get their own worktrees', async () => {
    const root = path.join(tmp, 'collide');
    const one = tg.isolatedWorkspace(repo, root, 'a:b');
    fs.writeFileSync(path.join(one, 'MARK'), 'x');
    const two = tg.isolatedWorkspace(repo, root, 'a_b');
    assert.notEqual(one, two);
    assert.ok(fs.existsSync(path.join(one, 'MARK')));
    fs.rmSync(root, { recursive: true, force: true });
    git('worktree', 'prune');
  });

  await test('run: without isolation tasks share the checkout', async () => {
    const g = graph({ a: {} });
    const r = await run(g, ['a'], { isolate: false });
    assert.equal(r.a.status, 'passed');
    assert.ok(fs.existsSync(path.join(repo, 'out-a', 'end')));
    fs.rmSync(path.join(repo, 'out-a'), { recursive: true });
  });

  await test('main: digest names exactly the cacheable passes, exit code reflects failures', async () => {
    const g = graph({ a: {}, b: { cache: false }, fails: {} });
    const out = path.join(tmp, 'gh-output');
    const summary = path.join(tmp, 'gh-summary');
    fs.writeFileSync(out, '');
    const env = { ...process.env, TASKGRAPH_PLAN: JSON.stringify(g), TASKGRAPH_MISSES: 'a,b,fails', TASKGRAPH_PM: 'npm', TASKGRAPH_CONCURRENCY: '3', RUNNER_TEMP: path.join(tmp, 'rt'), GITHUB_OUTPUT: out, GITHUB_STEP_SUMMARY: summary };
    const cwd = process.cwd();
    process.chdir(repo);
    const log = console.log;
    console.log = () => {};
    let code;
    const write = process.stdout.write;
    process.stdout.write = () => true;
    try { code = await tg.main(env); } finally { console.log = log; process.stdout.write = write; process.chdir(cwd); }
    assert.equal(code, 1);
    const lines = fs.readFileSync(out, 'utf8');
    assert.match(lines, new RegExp(`^digest=${tg.manifestDigest([tg.sha256('a')])}$`, 'm'));
    const res = JSON.parse(lines.match(/^results=(.*)$/m)[1]);
    assert.equal(res.b.status, 'passed');
    assert.match(fs.readFileSync(summary, 'utf8'), /taskgraph run/);
    assert.ok(!git('worktree', 'list').includes(path.join(tmp, 'rt')), 'main removes and prunes its worktrees');
    // A leg runs exactly the names it was given.
    fs.writeFileSync(out, '');
    process.chdir(repo);
    console.log = () => {};
    process.stdout.write = () => true;
    try { code = await tg.main({ ...env, TASKGRAPH_MISSES: '', TASKGRAPH_RUN: 'a' }); } finally { console.log = log; process.stdout.write = write; process.chdir(cwd); }
    assert.equal(code, 0);
    assert.deepEqual(Object.keys(JSON.parse(fs.readFileSync(out, 'utf8').match(/^results=(.*)$/m)[1])), ['a']);
    await assert.rejects(tg.main({ ...env, TASKGRAPH_RUN: 'nope' }), /unknown task nope/);
  });

  // ---- proofs against a fake store and a fake GitHub ----
  const repoId = 4242;
  const context = { repo: { owner: 'narduk-enterprises', repo: 'app' }, payload: { repository: { id: repoId } }, sha: 'c'.repeat(40), eventName: 'push' };
  const objects = new Map();
  const fakeStore = { ...store, getObject: async (cfg, key) => (objects.has(key) ? Buffer.from(objects.get(key)) : null), putObject: async (cfg, key, body) => { objects.set(key, body.toString()); } };
  const cfg = { bucket: 'b' };
  const key = 'a'.repeat(64);
  const other = 'b'.repeat(64);
  const digest = tg.manifestDigest([key, other]);
  const fakeGithub = (over = {}) => ({
    calls: 0,
    paginate: async (fn, args) => (await fn(args)).data,
    rest: { actions: {
      getWorkflowRunAttempt: async ({ run_id, attempt_number }) => ({ data: { id: run_id, run_attempt: attempt_number, path: over.path || '.github/workflows/ci.yml', html_url: `https://x/${run_id}`, repository: { id: repoId }, head_repository: { id: over.headRepo || repoId } } }),
      listJobsForWorkflowRunAttempt: async () => ({ data: [{ name: over.job || 'ci / Tasks (all)', steps: [{ name: over.step || `Publish task proofs ${digest}`, conclusion: over.conclusion || 'success' }] }] }),
    }, repos: {
      getContent: async () => ({ data: { sha: over.blob || 'f'.repeat(40) } }),
    } },
  });
  const pointer = (over = {}) => JSON.stringify({ schema: tg.POINTER_SCHEMA, key, repository_id: repoId, run_id: 7, run_attempt: 1, manifest: [key, other], ...over });
  const lookup = async (gh, k = key) => tg.findTaskProof({ github: gh, context, store: fakeStore, cfg, key: k, workflowPath: '.github/workflows/ci.yml', attempts: new Map() });
  const objectKey = tg.proofKey(store, 'narduk-enterprises/app', key);

  await test('proof: the object key is repository-scoped and shape-checked', () => {
    assert.equal(objectKey, `proof/narduk-enterprises/app/task-proof-v1-${key}.json`);
    assert.throws(() => tg.proofKey(store, 'narduk-enterprises/app', 'nope'), /not a task key/);
  });

  await test('proof: honoured only when GitHub confirms the publish step for this manifest', async () => {
    assert.equal((await lookup(fakeGithub())).found, false, 'no object, no proof');
    objects.set(objectKey, pointer());
    assert.equal((await lookup(fakeGithub())).found, true);
    for (const [why, gh] of [
      ['step failed', fakeGithub({ conclusion: 'failure' })],
      ['digest of another manifest', fakeGithub({ step: `Publish task proofs ${tg.manifestDigest([key])}` })],
      ['not a Tasks leg', fakeGithub({ job: 'ci / Required' })],
      ['a leg of another shape', fakeGithub({ job: 'ci / Tasks (a) (b)' })],
      ['another workflow', fakeGithub({ path: '.github/workflows/e2e.yml' })],
      ['a fork run', fakeGithub({ headRepo: 1 })],
    ]) assert.equal((await lookup(gh)).found, false, why);
    // The attested run must have run this caller workflow's text, not only its path.
    const withBlob = (gh, blob) => tg.findTaskProof({ github: gh, context, store: fakeStore, cfg, key, workflowPath: '.github/workflows/ci.yml', callerBlob: blob, attempts: new Map() });
    assert.equal((await withBlob(fakeGithub(), 'f'.repeat(40))).found, true);
    assert.equal((await withBlob(fakeGithub({ blob: 'e'.repeat(40) }), 'f'.repeat(40))).found, false, 'another version of the workflow');
    for (const [why, p] of [
      ['key not in manifest', pointer({ manifest: [other] })],
      ['another repository', pointer({ repository_id: 1 })],
      ['another key', pointer({ key: other })],
      ['bad schema', pointer({ schema: 'x' })],
    ]) {
      objects.set(objectKey, p);
      assert.equal((await lookup(fakeGithub())).found, false, why);
    }
    objects.set(objectKey, 'not json');
    assert.equal((await lookup(fakeGithub())).found, false);
    objects.delete(objectKey);
  });

  await test('publish: one pointer per cacheable pass, manifest digest matches main\'s', async () => {
    const planTasks = [{ task: 'a', key, cache: true }, { task: 'b', key: other, cache: false }, { task: 'c', key: 'c'.repeat(64), cache: true }];
    const written = await tg.publishTaskProofs({ store: fakeStore, cfg, context, planTasks, results: { a: { status: 'passed' }, b: { status: 'passed' }, c: { status: 'failed' } }, env: { GITHUB_RUN_ID: '9', GITHUB_RUN_ATTEMPT: '2' } });
    assert.deepEqual(written, [objectKey]);
    const p = JSON.parse(objects.get(objectKey));
    assert.deepEqual(p.manifest, [key]);
    assert.equal(p.run_id, 9);
    assert.equal(p.run_attempt, 2);
    objects.clear();
  });

  // ---- output cache: a passed producer's outputs, stored and restored ----
  await test('outputs: a stored producer is restored, not re-run, and the digest attests its tarball', () => {
    const g = graph({ build: { outputs: ['.output/**'] }, test: { dependsOn: ['build'] }, mid: { dependsOn: ['build'] }, e2e: { dependsOn: ['mid'] } });
    assert.deepEqual(tg.executionSet(g.tasks, new Set(['test'])), ['build', 'test'], 'without a stored tarball the build runs');
    assert.deepEqual(tg.executionSet(g.tasks, new Set(['test']), new Set(['build'])), ['test']);
    assert.deepEqual(tg.restoresFor(g.tasks, ['test'], new Set(['build'])), ['build']);
    // Looked up through a passed task with no outputs.
    assert.deepEqual(tg.executionSet(g.tasks, new Set(['e2e']), new Set(['build'])), ['e2e']);
    assert.deepEqual(tg.restoresFor(g.tasks, ['e2e'], new Set(['build'])), ['build']);
    assert.deepEqual(tg.restoresFor(g.tasks, ['build', 'test'], new Set(['build'])), [], 'a producer that runs is not restored');
    // A restored producer carries what it was built from.
    const chain = graph({ codegen: { outputs: ['gen/**'] }, build: { outputs: ['dist/**'], dependsOn: ['codegen'] } });
    assert.deepEqual(tg.carriedOutputDirs(chain.tasks, 'build'), ['gen', 'dist']);
    const h = 'd'.repeat(64);
    assert.equal(tg.manifestDigest([key, other]), tg.manifestDigest([key, other], {}), 'no outputs: the digest is unchanged');
    assert.notEqual(tg.manifestDigest([key, other], { [key]: h }), tg.manifestDigest([key, other]));
    assert.equal(tg.outputObjectKey(store, 'narduk-enterprises/app', 7, 1, key), `prebuilt/narduk-enterprises/app/7-1-taskout-${key}.tar.gz`);
  });

  await test('outputs: the proof carries the tarball hash only when the attested digest covers it', async () => {
    const h = 'd'.repeat(64);
    const withOut = tg.manifestDigest([key, other], { [key]: h });
    objects.set(objectKey, pointer({ outputs: { [key]: h } }));
    const ok = await lookup(fakeGithub({ step: `Publish task proofs ${withOut}` }));
    assert.equal(ok.found, true);
    assert.deepEqual(ok.output, { sha256: h, run_id: 7, run_attempt: 1 });
    assert.equal((await lookup(fakeGithub())).found, false, 'a hash the step name does not attest');
    for (const [why, outs] of [['not a hash', { [key]: 'x' }], ['a key outside the manifest', { ['e'.repeat(64)]: h }], ['an array', [h]]]) {
      objects.set(objectKey, pointer({ outputs: outs }));
      assert.equal((await lookup(fakeGithub({ step: `Publish task proofs ${withOut}` }))).found, false, why);
    }
    objects.set(objectKey, pointer());
    assert.equal((await lookup(fakeGithub())).output, null, 'an old proof restores nothing');
    objects.delete(objectKey);
  });

  await test('outputs: archived after a pass, stored, fetched, checked and restored into a dependent', async () => {
    const bins = new Map();
    const binStore = { ...store, storeConfig: () => cfg, getObject: async (c, k) => bins.get(k) || null, putObject: async (c, k, body) => { bins.set(k, Buffer.from(body)); } };
    const g = graph({ prepare: { outputs: ['.nuxt/**'] }, uses: { dependsOn: ['prepare'] } });
    const rt = path.join(tmp, 'rt-out');
    fs.rmSync(rt, { recursive: true, force: true });
    const cwds = new Map();
    const r1 = await run(g, ['prepare'], { onEvent: e => { if (e.type === 'start') cwds.set(e.task, e.cwd); } });
    assert.equal(r1.prepare.status, 'passed');
    const outs = tg.archiveOutputs({ planTasks: g.tasks, results: r1, cwds, dir: path.join(rt, 'taskgraph-outputs') });
    const pk = tg.sha256('prepare');
    assert.match(outs[pk], /^[0-9a-f]{64}$/);
    const warnings = [];
    await tg.publishTaskProofs({ store: binStore, cfg, context, planTasks: g.tasks, results: r1, outputs: outs, outputDir: path.join(rt, 'taskgraph-outputs'), env: { GITHUB_RUN_ID: '9', GITHUB_RUN_ATTEMPT: '2' }, warn: m => warnings.push(m) });
    assert.deepEqual(warnings, []);
    const tarKey = tg.outputObjectKey(store, 'narduk-enterprises/app', 9, 2, pk);
    assert.ok(bins.has(tarKey));
    const proof = JSON.parse(bins.get(tg.proofKey(store, 'narduk-enterprises/app', pk)).toString());
    assert.deepEqual(proof.outputs, { [pk]: outs[pk] });
    // A later leg: `uses` misses, `prepare` passed with its tarball stored.
    const restores = { prepare: { key: pk, sha256: outs[pk], run_id: 9, run_attempt: 2 } };
    const core = { warning: m => warnings.push(m), info: () => {} };
    const env = { RUNNER_TEMP: rt, TASKGRAPH_PLAN: JSON.stringify(g), TASKGRAPH_RUN: 'uses', TASKGRAPH_RESTORES: JSON.stringify(restores), CI_ARTIFACTS_R2_ACCOUNT_ID: 'a', CI_ARTIFACTS_R2_ACCESS_KEY_ID: 'b', CI_ARTIFACTS_R2_SECRET_ACCESS_KEY: 'c' };
    assert.deepEqual(await tg.restoreStep({ context, core, store: binStore, env }), ['prepare']);
    const out = path.join(tmp, 'gh-output-restore');
    fs.writeFileSync(out, '');
    const quiet = async fn => { const log = console.log; const w = process.stdout.write; const c = process.cwd(); console.log = () => {}; process.stdout.write = () => true; process.chdir(repo); try { return await fn(); } finally { console.log = log; process.stdout.write = w; process.chdir(c); } };
    fs.rmSync(path.join(repo, '.nuxt'), { recursive: true, force: true });
    assert.equal(await quiet(() => tg.main({ ...process.env, ...env, TASKGRAPH_PM: 'npm', GITHUB_OUTPUT: out })), 0, 'uses reads the restored .nuxt');
    assert.deepEqual(Object.keys(JSON.parse(fs.readFileSync(out, 'utf8').match(/^results=(.*)$/m)[1])), ['uses'], 'prepare did not run');
    // A tarball that does not match its proof is refused, and the producer runs instead.
    bins.set(tarKey, Buffer.from('tampered'));
    fs.rmSync(path.join(rt, 'taskgraph-restore'), { recursive: true, force: true });
    assert.deepEqual(await tg.restoreStep({ context, core, store: binStore, env }), []);
    assert.match(warnings.join('\n'), /does not match its proof/);
    fs.writeFileSync(out, '');
    assert.equal(await quiet(() => tg.main({ ...process.env, ...env, TASKGRAPH_PM: 'npm', GITHUB_OUTPUT: out })), 0);
    assert.deepEqual(Object.keys(JSON.parse(fs.readFileSync(out, 'utf8').match(/^results=(.*)$/m)[1])), ['prepare', 'uses'], 'no checked tarball: prepare runs here');
    fs.rmSync(rt, { recursive: true, force: true });
  });

  await test('outputs: a restored producer brings the outputs it was built from (two levels)', async () => {
    // prepare (.nuxt) -> a (out-a) -> uses, which reads .nuxt: restoring `a` alone must bring .nuxt.
    const g = graph({ prepare: { outputs: ['.nuxt/**'] }, a: { outputs: ['out-a/**'], dependsOn: ['prepare'] }, uses: { dependsOn: ['a'] } });
    const cwds = new Map();
    const r1 = await run(g, ['prepare', 'a'], { onEvent: e => { if (e.type === 'start') cwds.set(e.task, e.cwd); } });
    assert.equal(r1.a.status, 'passed');
    const dir = path.join(tmp, 'two-level');
    const outs = tg.archiveOutputs({ planTasks: g.tasks, results: r1, cwds, dir });
    const ak = tg.sha256('a');
    const x = path.join(dir, 'x');
    fs.mkdirSync(x, { recursive: true });
    execFileSync('tar', ['-xzf', path.join(dir, `${ak}.tar.gz`), '-C', x]);
    assert.ok(fs.existsSync(path.join(x, '.nuxt', 'marker')) && fs.existsSync(path.join(x, 'out-a', 'end')), 'a\'s tarball holds .nuxt and out-a');
    assert.match(outs[ak], /^[0-9a-f]{64}$/);
    fs.rmSync(path.join(repo, '.nuxt'), { recursive: true, force: true });
    const r2 = await run(g, ['uses'], { restored: new Map([['a', x]]) });
    assert.equal(r2.uses.status, 'passed', JSON.stringify(r2));
    fs.rmSync(dir, { recursive: true, force: true });
  });

  // ---- the github-script steps ----
  const notes = [];
  const core = () => {
    const outputs = {};
    return { outputs, setOutput: (k, v) => { outputs[k] = v; }, info: () => {}, notice: m => notes.push(m), warning: m => notes.push(m), summary: { addRaw() { return this; }, async write() {} } };
  };
  const files = { 'package.json': pkg({ lint: 'eslint .', test: 'vitest run' }), 'turbo.json': JSON.stringify({ tasks: { lint: { tags: ['ci'] }, test: { tags: ['ci'] } } }), '.node-version': '24.1.0\n' };
  const api = (over = {}) => {
    const gh = fakeGithub(over);
    gh.rest.repos = {
      getCommit: async () => ({ data: { commit: { tree: { sha: 't'.repeat(40) } } } }),
      getContent: async ({ path: p }) => { if (p === '.github/workflows/ci.yml') return { data: { sha: '3'.repeat(40) } }; if (!(p in files)) { const e = new Error('404'); e.status = 404; throw e; } return { data: files[p] }; },
    };
    gh.rest.git = { getTree: async () => ({ data: { truncated: false, tree: [{ path: 'package.json', mode: '100644', type: 'blob', sha: '1'.repeat(40) }, { path: 'src/a.ts', mode: '100644', type: 'blob', sha: '2'.repeat(40) }, { path: '.github/workflows/ci.yml', mode: '100644', type: 'blob', sha: '3'.repeat(40) }] } }) };
    return gh;
  };
  const env = (over = {}) => ({ CALLER_WORKFLOW_REF: 'narduk-enterprises/app/.github/workflows/ci.yml@refs/heads/main', WORKING_DIRECTORY: '.', NODE_VERSION: '24', NODE_VERSION_FILE: '.node-version', PACKAGE_MANAGER: 'pnpm', INSTALL_SCRIPT: '', REUSE: 'true', CI_ARTIFACTS_R2_ACCOUNT_ID: '0'.repeat(32), CI_ARTIFACTS_R2_ACCESS_KEY_ID: '1'.repeat(32), CI_ARTIFACTS_R2_SECRET_ACCESS_KEY: '2'.repeat(64), ...over });

  await test('plan step: nothing passed before -> every task runs', async () => {
    const c = core();
    const { result } = await tg.planStep({ github: api(), context, core: c, store: fakeStore, env: env() });
    assert.equal(c.outputs.run, 'true');
    assert.equal(c.outputs.misses, 'lint,test');
    assert.deepEqual(JSON.parse(c.outputs.plan), result);
    assert.deepEqual(JSON.parse(c.outputs.legs), [{ id: 'all', names: 'lint,test' }]);
  });

  await test('plan step: every task passed before -> no run, nothing to install', async () => {
    const c0 = core();
    const { result } = await tg.planStep({ github: api(), context, core: c0, store: fakeStore, env: env() });
    const keys = result.tasks.map(t => t.key);
    await tg.publishTaskProofs({ store: fakeStore, cfg, context, planTasks: result.tasks, results: { lint: { status: 'passed' }, test: { status: 'passed' } }, env: { GITHUB_RUN_ID: '7', GITHUB_RUN_ATTEMPT: '1' } });
    const c = core();
    await tg.planStep({ github: api({ step: `Publish task proofs ${tg.manifestDigest(keys)}` }), context, core: c, store: fakeStore, env: env() });
    assert.equal(c.outputs.run, 'false');
    assert.equal(c.outputs.misses, '');
    assert.equal(c.outputs.legs, '[]');
    const r = core();
    await tg.planStep({ github: api({ step: `Publish task proofs ${tg.manifestDigest(keys)}` }), context, core: r, store: fakeStore, env: env({ REUSE: 'false' }) });
    assert.equal(r.outputs.misses, 'lint,test', 'reuse: false runs everything');
    objects.clear();
  });

  await test('plan step: no store credentials -> a notice and every task runs', async () => {
    const c = core();
    notes.length = 0;
    await tg.planStep({ github: api(), context, core: c, store: fakeStore, env: env({ CI_ARTIFACTS_R2_SECRET_ACCESS_KEY: '' }) });
    assert.equal(c.outputs.misses, 'lint,test');
    assert.ok(notes.some(n => /No CI artifact store credentials/.test(n)));
  });

  await test('plan step: a missing node-version-file and a parent working-directory are refused', async () => {
    await assert.rejects(tg.planStep({ github: api(), context, core: core(), store: fakeStore, env: env({ NODE_VERSION_FILE: '.nvmrc' }) }), /does not exist/);
    assert.throws(() => tg.normalizeDir('../x'), /leaves the repository/);
    await assert.rejects(tg.planStep({ github: api(), context, core: core(), store: fakeStore, env: env({ PACKAGE_MANAGER: 'yarn' }) }), /pnpm or npm/);
    assert.equal(tg.normalizeDir('./apps/web/'), 'apps/web');
    assert.equal(tg.normalizeDir('.'), '');
  });

  console.log(`HARNESS ${results.length} passed`);
})().catch(error => { console.error(error); process.exit(1); });
"""


def check_engine() -> None:
    doc = yaml.safe_load(WORKFLOW.read_text())
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        install(doc, ENGINE_STEP, root)
        install(doc, STORE_STEP, root)
        harness = root / "harness.cjs"
        harness.write_text(HARNESS)
        work = root / "work"
        work.mkdir()
        out = subprocess.run(
            ["node", str(harness), str(root / "taskgraph.cjs"), str(root / "ci-artifact-store.cjs"), str(work)],
            capture_output=True, text=True, timeout=300,
        )
        print(out.stdout, end="")
        if out.returncode != 0 or "HARNESS" not in out.stdout:
            raise SystemExit(f"engine harness failed:\n{out.stderr[-4000:]}")


def run_text(doc: dict, job: str, name: str) -> str:
    return step(doc, job, name)["run"]


def check_copies() -> None:
    """The steps taskgraph.yml copies from nuxt-cloudflare.yml stay identical."""
    tg = yaml.safe_load(WORKFLOW.read_text())
    nc = yaml.safe_load(NUXT.read_text())
    pairs = [
        (STORE_STEP, "plan", "reuse-plan"),
        (STORE_STEP, "tasks", "reuse-plan"),
        ("Configure package registry auth", "tasks", "build"),
        ("Point pnpm at a workspace-local store (self-hosted)", "tasks", "build"),
        ("Install dependencies (pnpm)", "tasks", "build"),
        ("Install dependencies (npm)", "tasks", "build"),
        ("Ensure PyYAML is available", "required", "build"),
        ("Install actionlint", "required", "build"),
        ("actionlint (caller's own workflows)", "required", "build"),
        ("Caller workflow hygiene audit (concurrency, timeouts, SHA pins, permissions)", "required", "build"),
    ]
    for name, ours, job in pairs:
        assert run_text(tg, ours, name) == run_text(nc, job, name), f"{name} ({ours}): drifted from nuxt-cloudflare.yml {job}"
    assert run_text(tg, "plan", ENGINE_STEP) == run_text(tg, "tasks", ENGINE_STEP), "one engine for both jobs"
    assert step(tg, "required", "Install actionlint")["env"] == step(nc, "build", "Install actionlint")["env"]
    for name in ("Install dependencies (pnpm)", "Install dependencies (npm)"):
        assert step(tg, "tasks", name)["env"] == step(nc, "build", name)["env"], f"{name}: env drifted"
    print(f"ok  {len(pairs)} copied steps match nuxt-cloudflare.yml byte for byte")


def check_shape() -> None:
    doc = yaml.safe_load(WORKFLOW.read_text())
    jobs = doc["jobs"]
    assert list(jobs) == ["plan", "tasks", "required"], list(jobs)
    assert jobs["required"]["name"] == "Required" and jobs["required"]["if"] == "!cancelled()"
    assert jobs["required"]["needs"] == ["plan", "tasks"], "Required needs every job"
    # Plan holds no install and runs no caller code.
    plan = [s.get("name") or s.get("uses") for s in steps(doc, "plan")]
    assert plan == [STORE_STEP, ENGINE_STEP, "Plan tasks"], plan
    assert jobs["plan"]["permissions"] == {"contents": "read", "actions": "read"}
    # One leg per machine, only when there is work; an empty matrix is an error.
    tasks = jobs["tasks"]
    assert tasks["name"] == "Tasks (${{ matrix.leg.id }})"
    assert tasks["if"] == "needs.plan.outputs.run == 'true'"
    assert tasks["strategy"] == {"fail-fast": False, "matrix": {"leg": "${{ fromJSON(needs.plan.outputs.legs) }}"}}
    run = step(doc, "tasks", "Run tasks")
    assert run["env"]["TASKGRAPH_RUN"] == "${{ matrix.leg.names }}"
    assert "steps.plan" not in yaml.safe_dump(tasks), "a leg reads the plan through needs, not a step"
    publish = next(s for s in steps(doc, "tasks") if str(s.get("name", "")).startswith("Publish task proofs"))
    # The engine's verifier looks for exactly this name, in a `Tasks (<leg>)` job.
    assert publish["name"] == "Publish task proofs ${{ steps.run.outputs.digest }}"
    assert publish["if"].startswith("always() && steps.run.outputs.digest != ''")
    assert "head.repo.full_name == github.repository" in publish["if"], "a fork pull request records nothing"
    assert "Tasks \\(" in run_text(doc, "plan", ENGINE_STEP), "the verifier matches the leg job name"
    # Required: the verdict, then a sparse checkout and Caller lint, all after a failed leg too.
    req = steps(doc, "required")
    assert req[0]["name"] == "Require the plan and every leg to pass"
    lint_steps = [s for s in req if s.get("name") in (
        "Ensure PyYAML is available", "Install actionlint", "actionlint (caller's own workflows)",
        "Caller workflow hygiene audit (concurrency, timeouts, SHA pins, permissions)")]
    assert len(lint_steps) == 4 and all(s["if"] == "!cancelled()" for s in lint_steps), "Caller lint runs after a task failure too"
    on = doc[True]["workflow_call"]
    assert len(on["inputs"]) == 9, f"nine inputs, not a flag per gate (got {len(on['inputs'])})"
    assert all(not v.get("required") for v in on["secrets"].values())
    print("ok  Plan, one Tasks leg per machine, Required: publish names the digest, Caller lint after failures")


def check_verdict() -> None:
    """Required: green only for a green plan and, when it found work, green legs."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    script = step(doc, "required", "Require the plan and every leg to pass")["run"]

    def verdict(plan, run, tasks):
        env = {**os.environ, "PLAN_RESULT": plan, "RUN": run, "TASKS_RESULT": tasks}
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True).returncode

    assert verdict("success", "false", "skipped") == 0, "nothing to run"
    assert verdict("success", "true", "success") == 0
    for plan, run, tasks in (("failure", "", "skipped"), ("success", "true", "failure"), ("success", "true", "cancelled"),
                             ("success", "true", "skipped"), ("success", "false", "success"), ("cancelled", "", "skipped")):
        assert verdict(plan, run, tasks) != 0, (plan, run, tasks)
    print("ok  Required fails unless the plan passed and every leg it asked for passed")


def main() -> int:
    check_shape()
    check_verdict()
    check_copies()
    check_engine()
    print("test_taskgraph: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
