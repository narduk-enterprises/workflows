# workflows

Shared reusable GitHub Actions workflows for the narduk-enterprises estate (CI-5).

> **This repository is public on purpose.** GitHub only serves reusable
> workflows across owners when the host repo is public (short of GitHub
> Enterprise, which the estate rejected). Public visibility lets every owner —
> `narduk-enterprises`, incubator, clients, and personal `loganrenz/*` repos —
> call these workflows directly. The workflows hold no secrets (all
> `workflow_call` secrets are optional and skip cleanly).
>
> **Callers must pin `@v1` or a full commit SHA — never `@main` — and must
> never pass a self-hosted runner label.** A fork PR on a public caller can
> run attacker-controlled code, so estate self-hosted runners are off-limits here;
> reusable jobs default to GitHub-hosted `ubuntu-latest`.

Fix CI in one place, not 100. Application repos call these workflows via
`workflow_call` instead of blob-copying YAML. This repo replaces the broken
pattern where ~20 repos carried copies of `weekly-drift-check.yml` pointing at
`narduk-enterprises/narduk-nuxt-template/.github/workflows/reusable-quality.yml@main`
— a repo name that no longer exists (renamed to `narduk-template`), so every
scheduled run 404'd silently.

CI-5 phase 2 (`company-hq strategy/workflow-consistency-proposal.md`) adds the
three app-shaped CI gates below, each producing the identical
`ci / Required` check context so branch protection and org rulesets can
require the same string across every repo of that type. See
[§2 "Proposed standard"](https://github.com/narduk-enterprises/company-hq/blob/main/strategy/workflow-consistency-proposal.md#2-proposed-standard)
in that proposal for the full rationale. **Not built in this pass** (flagged
there as not evidence-based, or explicitly out of scope): `apple.yml`,
`python-data.yml`, a `nuxt-cloudflare-deploy.yml` sibling for push-to-main
deploys with real Cloudflare secrets, actual consumer adoption, and any
branch-protection/ruleset change.

## Catalog

| Workflow | Purpose |
|----------|---------|
| `docs-governance.yml` | Thin generic gate for docs/handbook-shaped repos: checkout, optionally provision Python/Node, run one repo-provided check command. Generalizes company-hq's `handbook-spine-check.yml` / `untangle-project-sync.yml` shape |
| `node-library.yml` | CI gate for `library` / `cli` project-lifecycle surfaces: lint/typecheck/test/build, each `--if-present`, with an optional per-package matrix generalizing narduk-libs' `package-gates` + `verify` pattern. See [relationship to `reusable-node-ci.yml`](#relationship-between-node-libraryyml-and-reusable-node-ciyml) below |
| `nuxt-cloudflare.yml` | CI gate for `nuxt-web` / `cloudflare-worker` surfaces: typecheck (worker + Nuxt split, matching hydrogen), build, optional Playwright e2e, optional `wrangler deploy --dry-run` validation. CI only — no deploy job (see below) |
| `reusable-node-ci.yml` | Generic Node CI: lint, typecheck, test, build (pnpm or npm). Zero live callers as of 2026-07-24 — kept for compatibility; `node-library.yml` is the richer, preferred surface for new adoption |
| `reusable-weekly-drift-check.yml` | Weekly template-drift + quality check for fleet apps: typecheck, unit tests, and `narduk-fleet check-drift` |

All five are `on: workflow_call` only — none of them declare their own
triggers, and none declare `concurrency:` (see "How to consume" below for why).

## The `ci / Required` convention

This is the actual point of the repo, not an implementation detail.

`docs-governance.yml`, `node-library.yml`, and `nuxt-cloudflare.yml` each end
with a job named **exactly** `Required`. That job `needs:` every other job the
workflow defines, runs with `if: always()`, and explicitly checks each
`needs.<job>.result` — a job that's mandatory must report `success`; a job
that's gated behind an opt-in input (Playwright e2e, the wrangler dry-run gate,
a package-matrix lane) may report `success` **or** `skipped`, but never
`failure` or `cancelled`. `if: always()` jobs succeed by default if you don't
check anything explicitly — these don't skip that check.

This generalizes a pattern narduk-libs already proved in production: its `ci.yml`
runs a 12-lane package matrix, then a `verify` job that `needs: package-gates`
and fails unless `needs.package-gates.result == 'success'` — one stable
pass/fail signal regardless of how many matrix lanes ran underneath.

The subtlety worth restating: for a called reusable workflow, **GitHub
composes the check-context string as `<caller's job id> / <job's name>`**. So
even with an identical reusable workflow, a caller whose calling job is named
`quality:` or `verify:` instead of `ci:` produces a *different* context string.
The convention has to cover both ends:

- Every reusable workflow here ends with a job named `Required`.
- **Every caller names its calling job id `ci`.**

That composes to one identical string on every adopting repo, regardless of
app type or what runs underneath:

```text
ci / Required
```

That string is what a branch-protection rule or org ruleset requires. This is
what reopens `untangle/TRACKER.yaml` D-PKG-3 Part B: once a publisher repo's
caller conforms, the org ruleset can require `ci / Required` without a
per-repo verification pass, because the string is enforced by convention, not
discovered by inspection.

## Runner routing (`runner` input)

`docs-governance.yml`, `node-library.yml`, and `nuxt-cloudflare.yml` each
accept a `runner` input: a **JSON-encoded string**, decoded with `fromJSON()`
at every job's `runs-on:`. It accepts three shapes:

```yaml
runner: '"ubuntu-latest"'                                              # plain string (the default)
runner: '["self-hosted","Linux","X64","proxmox","linux-ci"]'           # JSON array
runner: '{"group":"linux-ci","labels":["self-hosted","Linux","X64","proxmox","linux-ci"]}'  # JSON object
```

The object form matches `Config/github-runner-fleet.json`'s `runsOn` shape
exactly, so a private manifest-routed caller can paste that value verbatim.
The value must be **valid JSON** — a bare string still needs its own quotes,
which is why the default is the four-character JSON string `"ubuntu-latest"`,
not the bare word.

**PUBLIC CALLERS MUST NEVER PASS A SELF-HOSTED LABEL.** This repo is public;
a fork PR on a public caller can run attacker-controlled code, so a
self-hosted `runner` value on a public repo hands that PR estate
infrastructure. Only private, manifest-routed callers may pass a self-hosted
value — resolve it first with
`python3 scripts/github_runner_fleet.py route` (or
`scripts/onboard-proxmox-runner.sh --route-only`) in the `agent-infrastructure`
checkout, then copy the returned `runsOn` object verbatim. Every workflow file
in this repo repeats this rule in a loud top-of-file comment; don't rely on
this README alone when adding a fourth.

`reusable-node-ci.yml`'s existing `runner` input is a **plain string only**
(`runs-on: ${{ inputs.runner }}`, no `fromJSON`) — it predates this
convention and is left as-is. Don't pass a JSON-encoded value to it; it isn't
decoded.

## How to consume

### Caller template

Every caller is a thin workflow with **one job named `ci`** (this is what
makes the `ci / Required` convention work — see above) and triggers on
`pull_request`, `push` to the default branch, and `workflow_dispatch`.
**Never trigger PR-only** — a PR-only trigger means the default branch's tip
carries no CI status after merge, which is exactly what happened to
`narduk-skills` and (pre-merge) `narduk-eslint-config` per the CI-5 phase 2
audit.

```yaml
name: CI

on:
  pull_request:
  push:
    branches: [main] # match the repo's actual default branch
  workflow_dispatch:

concurrency:
  group: ci-${{ github.repository }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true

jobs:
  ci: # <-- must be named `ci`; GitHub composes "ci / Required" from this + the reusable workflow's job name
    uses: narduk-enterprises/workflows/.github/workflows/<workflow>.yml@v1
    with:
      # ...per-workflow inputs, see below
    secrets:
      NARDUK_PLATFORM_GH_PACKAGES_READ: ${{ secrets.NARDUK_PLATFORM_GH_PACKAGES_READ }}
```

Set `concurrency` in the **caller** — workflow-level concurrency does not
propagate from called reusable workflows, and none of the five workflows in
this repo declare their own.

### `node-library.yml`

```yaml
jobs:
  ci:
    uses: narduk-enterprises/workflows/.github/workflows/node-library.yml@v1
    with:
      node-version: "22"
      package-manager: pnpm
    secrets:
      NARDUK_PLATFORM_GH_PACKAGES_READ: ${{ secrets.NARDUK_PLATFORM_GH_PACKAGES_READ }}
```

With a per-package matrix (generalizes narduk-libs' `package-gates`):

```yaml
jobs:
  ci:
    uses: narduk-enterprises/workflows/.github/workflows/node-library.yml@v1
    with:
      package-matrix: |
        [
          {"label": "narduk-core", "filter": "@narduk-enterprises/narduk-core"},
          {"label": "narduk-auth", "filter": "@narduk-enterprises/narduk-auth"}
        ]
    secrets:
      NARDUK_PLATFORM_GH_PACKAGES_READ: ${{ secrets.NARDUK_PLATFORM_GH_PACKAGES_READ }}
```

### `nuxt-cloudflare.yml`

```yaml
jobs:
  ci:
    uses: narduk-enterprises/workflows/.github/workflows/nuxt-cloudflare.yml@v1
    with:
      node-version: "24"
      package-manager: npm # hydrogen's current package manager; pnpm is the default
      typecheck-worker-script: typecheck
      typecheck-web-script: web:typecheck
      run-e2e: true
      wrangler-dry-run: true
    secrets:
      NARDUK_PLATFORM_GH_PACKAGES_READ: ${{ secrets.NARDUK_PLATFORM_GH_PACKAGES_READ }}
```

Deploying with real Cloudflare credentials on push-to-main is **not** this
workflow's job — that stays a separate `nuxt-cloudflare-deploy.yml` sibling
(deferred, not built in this pass), matching hydrogen's existing two-job
`ci` / `deploy` split rather than folding deploy secrets into the CI gate.

### `docs-governance.yml`

Path-filtering is the **caller's** job — this workflow doesn't know the
caller's repo layout:

```yaml
name: handbook-spine-check

on:
  pull_request:
    paths: [docs/**, scripts/check-handbook-spine.py]
  push:
    branches: [main]
    paths: [docs/**, scripts/check-handbook-spine.py]
  workflow_dispatch:

concurrency:
  group: handbook-spine-check-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true

jobs:
  ci:
    uses: narduk-enterprises/workflows/.github/workflows/docs-governance.yml@v1
    with:
      command: python3 scripts/check-handbook-spine.py
      fetch-depth: 1
```

`command` can be multi-line to install its own light dependencies first — for
example, a PyYAML-consuming script (like company-hq's
`untangle-project-sync.yml`, which is not itself a `docs-governance.yml`
caller today, but shares its shape):

```yaml
    with:
      command: |
        python3 -m pip install --disable-pip-version-check --no-cache-dir "pyyaml==6.*"
        python3 untangle/sync-to-project.py --project-number 1 --dry-run
```

### Weekly drift check (unchanged)

```yaml
jobs:
  drift:
    uses: narduk-enterprises/workflows/.github/workflows/reusable-weekly-drift-check.yml@v1
    with:
      run-typecheck: true
      run-tests: true
      run-drift-check: true
    secrets:
      NARDUK_PLATFORM_GH_PACKAGES_READ: ${{ secrets.NARDUK_PLATFORM_GH_PACKAGES_READ }}
```

### Generic Node CI (unchanged)

```yaml
jobs:
  ci:
    uses: narduk-enterprises/workflows/.github/workflows/reusable-node-ci.yml@v1
    with:
      node-version: "22"
      run-lint: true
      run-tests: true
    secrets:
      NARDUK_PLATFORM_GH_PACKAGES_READ: ${{ secrets.NARDUK_PLATFORM_GH_PACKAGES_READ }}
```

Notes:

- All secrets are optional; package-registry auth is skipped cleanly when no
  token is passed, so public/forked callers still run.
- **Public repos must never pass a self-hosted `runner`/label** (fork PRs
  would run attacker code on estate infrastructure) — see "Runner routing"
  above.

## Relationship between `node-library.yml` and `reusable-node-ci.yml`

`reusable-node-ci.yml` already does the core of this — lint/typecheck/test/build,
each `--if-present`, pnpm or npm — and a GitHub code search across
`narduk-enterprises` (2026-07-24, `"uses: narduk-enterprises/workflows"`)
found **zero** repos currently calling it: the only real cross-repo caller in
the org is `narduk-template-smoke-app`, and it calls
`reusable-weekly-drift-check.yml`, not this one. So `node-library.yml` doesn't
migrate a live caller — it's additive, and `reusable-node-ci.yml` is
unchanged in this PR.

`node-library.yml` is `reusable-node-ci.yml` plus two things `reusable-node-ci.yml`
doesn't have:

1. **The `Required` aggregator job**, generalizing narduk-libs' `package-gates`
   → `verify` pattern (see "The `ci / Required` convention" above).
2. **An optional `package-matrix` input**, generalizing narduk-libs'
   per-package matrix so a pnpm/npm workspace monorepo gets one parallel lane
   per package instead of one whole-repo job. The default is a single
   whole-repo lane, so `narduk-charts` / `narduk-skills` / `narduk-eslint-config`
   (none of which are monorepos) can ignore this input entirely and get the
   same shape `reusable-node-ci.yml` already provides them.

It also upgrades the `runner` input to accept the JSON array/object form (see
"Runner routing" above) — a deliberate divergence from `reusable-node-ci.yml`'s
plain-string-only `runner` input, called out explicitly rather than changing
that file's existing behavior underneath any (currently nonexistent) caller.

**Which one to adopt:** new callers should use `node-library.yml` — it is a
strict superset of what `reusable-node-ci.yml` offers for the `library`/`cli`
surfaces, and it's the workflow that produces the `ci / Required` check
context this proposal exists to standardize. `reusable-node-ci.yml` stays
published for compatibility since removing a public reusable workflow is a
breaking change regardless of live-caller count; it is not deprecated by this
PR, but it is not the recommended surface for new adoption either. Migrating
it fully (or formally deprecating it) is a follow-up decision for Logan, not
executed here.

## Versioning policy

- **Callers pin `@vN` tags or full commit SHAs, never `@main`.** `@main` is how
  the last outage happened; it is not a supported reference.
- Maintainers cut tags. `vN` major tags (`v1`, `v2`, …) move forward only for
  backward-compatible changes within that major; breaking changes (renamed or
  newly-required inputs, removed jobs, changed secret names) get a new major.
- To adopt a fix, callers bump their pinned tag/SHA deliberately — nothing
  changes under them silently unless they chose a moving `vN` major tag.
- **`docs-governance.yml`, `node-library.yml`, and `nuxt-cloudflare.yml` ship
  in this PR without a tag pointing at them yet.** The existing `v1` tag
  predates this PR (it was cut for the original two workflows) and is not
  moved here — moving a tag is a maintainer action taken deliberately, not a
  side effect of merging new files. The caller snippets above show `@v1` to
  match the estate's existing pin convention (`narduk-template-smoke-app`
  pins `@v1`), but until a maintainer advances (or re-cuts) `v1` to include
  this commit — or cuts a new major — the first real adopter of one of these
  three files should pin the exact commit SHA of this PR's merge commit
  instead of `@v1`, then move to the tag once one covers it. This is called
  out explicitly in the PR that adds these files.

## Maintainer conventions

- Third-party and first-party actions are pinned to full commit SHAs with a
  version comment, targeting the current Actions Node runtime.
- Jobs declare minimal `permissions` and explicit `timeout-minutes`.
- This is a private repo shared org-wide: Actions access is set to
  `organization` so other narduk-enterprises repos can call these workflows.
- New reusable workflows follow both estate-wide conventions added by CI-5
  phase 2: the workflow's last job is named exactly `Required` and `needs:`
  everything else (see above), and `runs-on:` is `${{ fromJSON(inputs.runner) }}`
  fed by a JSON-encoded `runner` input defaulting to `'"ubuntu-latest"'` (see
  "Runner routing" above).
- Estate conventions live in the `ci-workflow-author` skill
  (agent-infrastructure repo); consult it before adding workflows here.
