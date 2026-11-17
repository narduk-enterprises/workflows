# v3: composite actions

Four steps a repository's own workflow calls. They are composite actions, not reusable workflows, on purpose:

- **The caller keeps its jobs.** A reusable workflow brings its own jobs, runner choice and a `Required` job that takes a
  runner to add up the others. A composite action is steps inside the caller's job: the caller owns `runs-on`, `if`,
  `needs`, `permissions`, `concurrency` and the check's name, and no job exists only to report on other jobs.
- **Nothing to route.** No `runner` input, no organization variable, no visibility test. The job that uses the action
  already says where it runs.
- **Small enough to read.** Each is one file under 80 lines, or one script; `scripts/test_actions.py` runs the shipped
  text of every step.

They come from PACC·TRAC's CI after it was cut from six jobs per pull request to one (2026-11-17). v1 and v2 (the
reusable workflows in `.github/workflows/`) are unchanged and keep their callers.

| Action | What it does | Inputs |
| --- | --- | --- |
| [`setup`](./setup/action.yml) | Node from `.node-version`, pnpm from `packageManager`, the pnpm store where the runner can keep it, `pnpm install --frozen-lockfile` | `node-version-file`, `install-args` |
| [`verify`](./verify/action.yml) | Runs the checks `Config/remote-gate.json` names: what the diff selects on a pull request, the whole list otherwise; side by side, one timed line each | `scope`, `alone`, `config`, `base` |
| [`version-proof`](./version-proof/action.yml) | Polls a URL until it answers this commit's SHA; red when it never does | `url`, `expected`, `attempts`, `interval` |
| [`health`](./health/action.yml) | Polls a health URL until it answers 200; red, with the body, when it never does | `url`, `attempts`, `interval`, `means` |

## A caller

```yaml
jobs:
  verify:                       # the one required check: on a pull request and on main
    runs-on: <the repository's own route>
    timeout-minutes: 20
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@<sha> # v7
        with:
          fetch-depth: 0        # a pull request is compared with HEAD^1, so the base commit must be present
          persist-credentials: false
      - uses: narduk-enterprises/workflows/actions/setup@<sha> # v3
      - uses: narduk-enterprises/workflows/actions/verify@<sha> # v3
        with:
          alone: typecheck-demo build

  deploy-staging:
    needs: [verify, build]
    steps:
      # ... the repository's own deploy ...
      - uses: narduk-enterprises/workflows/actions/version-proof@<sha> # v3
        with:
          url: https://staging.example.com/version
      - uses: narduk-enterprises/workflows/actions/health@<sha> # v3
        with:
          url: https://staging.example.com/healthz?deep=1
          means: the database binding or its migrations are wrong
```

Pin the full commit SHA `v3` points to, with a `# v3` comment, as for v2. Never `@main`.

## The gate file

`verify` reads the same `Config/remote-gate.json` that `remote-gate --changed` reads on a developer's machine
(agent-infrastructure `scripts/remote_gate.py`), so a change that is green there runs the same checks in CI:

- `checks`: name to shell command. `{files}` becomes the changed files a rule matched; `{base}` the base commit.
- `default`: the names every event but a pull request runs.
- `changed.rules`: `paths` (globs from the repository root) to `checks`; `changed.always`: names every pull request
  runs; `changed.full`: paths that swap one check for another (the changed-files lint for the whole lint).

`alone` is the action's own input, not part of the file: the checks that must not share memory with the rest.

## What is not here yet

The build with its byte-exact handoff to the deploy, the deploy itself (migrate, read the ledger back, `wrangler deploy`)
and the browser sweep are still PACC·TRAC's own steps. They move here when a second repository needs them.
