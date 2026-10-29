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
    subprocess.run(["bash", "-c", step(doc, "required", name)["run"]], check=True,
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

  await test('concurrency: cgroup limits win over host cores and memory', () => {
    const limits = tg.cgroupLimits(f => (f.endsWith('cpu.max') ? '400000 100000\n' : `${8 * 1024 ** 3}\n`));
    assert.deepEqual(limits, { cpus: 4, memory: 8 * 1024 ** 3 });
    assert.equal(tg.defaultConcurrency({ cpus: 4, memory: 8 * 1024 ** 3 }) <= 4, true);
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
  const run = (g, names, opts = {}) => tg.runTasks({ planTasks: g.tasks, names, command: n => ['npm', 'run', '--silent', n], cwd: repo, concurrency: opts.concurrency || 4, isolate: opts.isolate === false ? null : path.join(tmp, `iso-${results.length}`), env: process.env, onEvent: opts.onEvent, timeoutMs: opts.timeoutMs });

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
      listJobsForWorkflowRunAttempt: async () => ({ data: [{ name: over.job || 'ci / Required', steps: [{ name: over.step || `Publish task proofs ${digest}`, conclusion: over.conclusion || 'success' }] }] }),
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
      ['not the Required job', fakeGithub({ job: 'ci / Something' })],
      ['another workflow', fakeGithub({ path: '.github/workflows/e2e.yml' })],
      ['a fork run', fakeGithub({ headRepo: 1 })],
    ]) assert.equal((await lookup(gh)).found, false, why);
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
      getContent: async ({ path: p }) => { if (!(p in files)) { const e = new Error('404'); e.status = 404; throw e; } return { data: files[p] }; },
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
        (STORE_STEP, "reuse-plan"),
        ("Configure package registry auth", "build"),
        ("Point pnpm at a workspace-local store (self-hosted)", "build"),
        ("Install dependencies (pnpm)", "build"),
        ("Install dependencies (npm)", "build"),
        ("Ensure PyYAML is available", "build"),
        ("Install actionlint", "build"),
        ("actionlint (caller's own workflows)", "build"),
        ("Caller workflow hygiene audit (concurrency, timeouts, SHA pins, permissions)", "build"),
    ]
    for name, job in pairs:
        assert run_text(tg, "required", name) == run_text(nc, job, name), f"{name}: drifted from nuxt-cloudflare.yml {job}"
    assert step(tg, "required", "Install actionlint")["env"] == step(nc, "build", "Install actionlint")["env"]
    for name in ("Install dependencies (pnpm)", "Install dependencies (npm)"):
        assert step(tg, "required", name)["env"] == step(nc, "build", name)["env"], f"{name}: env drifted"
    print(f"ok  {len(pairs)} copied steps match nuxt-cloudflare.yml byte for byte")


def check_shape() -> None:
    doc = yaml.safe_load(WORKFLOW.read_text())
    jobs = doc["jobs"]
    assert list(jobs) == ["required"] and jobs["required"]["name"] == "Required", "one job, named Required"
    assert "if" not in jobs["required"], "a self-contained Required carries no if:"
    names = [s.get("name") or s.get("uses") for s in steps(doc, "required")]
    assert names.index(STORE_STEP) < names.index(ENGINE_STEP) < names.index("Plan tasks") < names.index("Run tasks")
    publish = next(s for s in steps(doc, "required") if str(s.get("name", "")).startswith("Publish task proofs"))
    # The engine's verifier looks for exactly this name.
    assert publish["name"] == "Publish task proofs ${{ steps.run.outputs.digest }}"
    assert publish["if"].startswith("always() && steps.run.outputs.digest != ''")
    assert "head.repo.full_name == github.repository" in publish["if"], "a fork pull request records nothing"
    checkouts = [s for s in steps(doc, "required") if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert [c["if"] for c in checkouts] == ["steps.plan.outputs.run == 'true'", "steps.plan.outputs.run != 'true'"]
    for s in steps(doc, "required"):
        name = s.get("name") or s.get("uses")
        if name in ("Install dependencies (pnpm)", "Install dependencies (npm)", "Install dependencies (caller script)", "Run tasks",
                    "Configure package registry auth", "Resolve dependency cache directory", "Restore dependency cache") or \
                str(name).startswith(("pnpm/action-setup", "actions/setup-node")):
            assert "steps.plan.outputs.run == 'true'" in s["if"], f"{name} must not run when nothing executes"
    lint_steps = [s for s in steps(doc, "required") if s.get("name") in (
        "Ensure PyYAML is available", "Install actionlint", "actionlint (caller's own workflows)",
        "Caller workflow hygiene audit (concurrency, timeouts, SHA pins, permissions)")]
    assert len(lint_steps) == 4 and all(s["if"] == "!cancelled()" for s in lint_steps), "Caller lint runs after a task failure too"
    on = doc[True]["workflow_call"]
    assert len(on["inputs"]) == 8, f"eight inputs, not a flag per gate (got {len(on['inputs'])})"
    assert all(not v.get("required") for v in on["secrets"].values())
    print("ok  one Required job: plan before checkout, publish names the digest, Caller lint after failures")


def main() -> int:
    check_shape()
    check_copies()
    check_engine()
    print("test_taskgraph: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
