# workflows

Shared reusable GitHub Actions workflows for the narduk-enterprises estate (CI-5).

> **This repository is private, with Actions access set to `organization`.**
> That is what makes the private→private cross-repo call resolve for every
> `narduk-enterprises` repo, and it is the current state of the world:
> **D-VIS-1** (company-hq `DECISIONS.md`, 2026-07-24) removed public repos
> from the company orgs and reversed CI-5's "workflows repo goes PUBLIC".
> An earlier version of this section claimed the repo was public on purpose;
> it was wrong after D-VIS-1, and the claim was doing real damage because it
> was the *stated justification* for the self-hosted-runner warning below
> (`workflows#2`). The workflows still hold no secrets — every
> `workflow_call` secret is optional and skips cleanly, and `apple.yml` /
> `python-data.yml` declare none at all.
>
> **Callers must pin `@v1` or a full commit SHA — never `@main` — and a
> public (or possibly-future-public) caller must never pass a self-hosted
> runner label.** That warning is still correct, but it rests on the *caller*,
> not on this repo: `runner` is a free-form caller-supplied value, a repo can
> go public later, and a fork PR on a public caller can run
> attacker-controlled code on estate infrastructure. Reusable jobs default to
> GitHub-hosted `ubuntu-latest`; `apple.yml`'s Mac route is the one input with
> no default, deliberately.

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
in that proposal for the full rationale.

`apple.yml` and `python-data.yml` were the two named gaps left after that pass,
and company-hq#173 (R-14) is the issue that closed them: "adopt the shared
workflow" had been the standing answer for CI duplication, and for the Apple
and Python-data families there was nothing to adopt — so their duplication was
structural, not neglectful, and naming a workflow that did not exist made the
gap look like an adoption backlog. Both now exist and both have a real adopter
(see "Adopters" below). Still **not built**: a `nuxt-cloudflare-deploy.yml`
sibling for push-to-main deploys with real Cloudflare secrets.

The **browser / Playwright gap** (M-3 — "nothing here routes e2e to the
dedicated pool yet") is **closed**, and not by a separate workflow.
company-hq#278 established that every app of a class uses that class's
callable, which made the browser shape a `nuxt-cloudflare.yml` gap rather than
a missing eighth file: five repos had already hand-rolled the same sharded
`playwright-isolated` pattern (`status-apps`, `operator-portal`,
`earthdata-viewer`, `narduk-libs`, `nvault`) and a sixth had hand-rolled it
*wrong*, installing chromium on the general `linux-ci` guest
(`been-sober-for`, company-hq#276). See
[`e2e-runner` / `e2e-shards` below](#browser-shards-and-the-isolated-pool).

## Catalog

| Workflow | Purpose |
|----------|---------|
| `apple.yml` | CI gate for Apple repos (Swift packages, iOS/macOS apps): SwiftLint (official Linux binary) and boundary/plist checks on a Linux runner, `swift build` / `swift test` / `xcodebuild` on the repo-scoped Mac. **Two separately-routed runner inputs — that split is the point.** Release/signing stays per-repo |
| `python-data.yml` | CI gate for Python / data-pipeline repos: `uv` (lockfile check + sync) or pip/venv, pytest, opt-in pinned `ruff check`, plus an `extra-checks` hook so a repo-specific gate that needs the installed environment does not have to stay behind as a duplicate-install job |
| `docs-governance.yml` | Thin generic gate for docs/handbook-shaped repos: checkout, optionally provision Python/Node, run one repo-provided check command. Generalizes company-hq's `handbook-spine-check.yml` / `untangle-project-sync.yml` shape |
| `node-library.yml` | CI gate for `library` / `cli` project-lifecycle surfaces: lint/typecheck/test/build, each `--if-present`, with an optional per-package matrix generalizing narduk-libs' `package-gates` + `verify` pattern. See [relationship to `reusable-node-ci.yml`](#relationship-between-node-libraryyml-and-reusable-node-ciyml) below |
| `nuxt-cloudflare.yml` | CI gate for `nuxt-web` / `cloudflare-worker` surfaces: typecheck (worker + Nuxt split, matching hydrogen), build, optional Playwright e2e — optionally **sharded onto a separately-routed browser pool, with blob-report merge** — optional `wrangler deploy --dry-run` validation. CI only — no deploy job (see below) |
| `reusable-node-ci.yml` | Generic Node CI: lint, typecheck, test, build (pnpm or npm). Zero live callers as of 2026-07-24 — kept for compatibility; `node-library.yml` is the richer, preferred surface for new adoption |
| `reusable-weekly-drift-check.yml` | Weekly template-drift + quality check for fleet apps: typecheck, unit tests, and `narduk-fleet check-drift` |

All seven are `on: workflow_call` only — none of them declare their own
triggers, and none declare `concurrency:` (see "How to consume" below for why).

### Adopters

A reusable workflow with no adopter is the same defect as an adopter with no
workflow, so this table is part of the catalog rather than a footnote. "Enforced"
means `ci / Required` is an actual required status check on the repo's default
branch, read back from the API — not that the caller parses.

| Workflow | First adopter | Enforced |
|----------|---------------|----------|
| `apple.yml` | `narduk-enterprises/GeoGridKit` | not yet |
| `python-data.yml` | `narduk-enterprises/narduk-data` (`earth-data-ci.yml`) | not yet |
| `docs-governance.yml` | `narduk-enterprises/company-hq` | yes — repo ruleset `require-docs-governance` |
| `node-library.yml` | `narduk-enterprises/narduk-charts` | yes — repo ruleset `require-ci-required` |
| `nuxt-cloudflare.yml` | `hydrogen` | no — `hydrogen` has no branch protection; it called `@v1` unenforced for months, which is the failure mode this column exists to make visible |
| `reusable-node-ci.yml` | none | — |
| `reusable-weekly-drift-check.yml` | `narduk-template-smoke-app` | — |

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

`docs-governance.yml`, `node-library.yml`, `nuxt-cloudflare.yml` and
`python-data.yml` each accept a `runner` input: a **JSON-encoded string**,
decoded with `fromJSON()` at every job's `runs-on:`. `apple.yml` takes the
same encoding but splits it into **two** inputs — `lint-runner` and
`apple-runner` — because routing Apple CI per job rather than per repo is the
whole reason that file exists. It accepts three shapes:

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

**A PUBLIC CALLER MUST NEVER PASS A SELF-HOSTED LABEL.** The constraint lives
on the caller, not on this repo (which is private — see the top of this file
and `workflows#2`): a fork PR on a public caller can run attacker-controlled
code, so a self-hosted `runner` value on a public repo hands that PR estate
infrastructure. A repo can also become public later, and `runner` is a
free-form string these workflows cannot police. Only private, manifest-routed
callers may pass a self-hosted value — resolve it first with
`python3 scripts/github_runner_fleet.py route` (or
`scripts/onboard-proxmox-runner.sh --route-only`) in the `agent-infrastructure`
checkout, then copy the returned `runsOn` object verbatim. Every workflow file
in this repo repeats this rule in a loud top-of-file comment; don't rely on
this README alone when adding the next one.

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
propagate from called reusable workflows, and none of the workflows in this
repo declare their own.

`apple.yml` and `python-data.yml` declare **no `secrets:` block at all**, so a
caller must not pass one. That is deliberate: a reusable workflow receives only
what it declares (there is no ambient inheritance without `secrets: inherit`),
so the strongest way to say "this gate cannot reach your credentials" is to
declare nothing. A repo whose Apple build needs private SwiftPM access, or
whose data gate needs a cross-repo PAT, keeps that step in its own workflow
rather than widening the shared one.

### `apple.yml`

```yaml
jobs:
  ci:
    uses: narduk-enterprises/workflows/.github/workflows/apple.yml@v1
    with:
      # Repo-scoped Mac from Config/github-runner-fleet.json `appleRepositories`.
      # REQUIRED — there is no default, on purpose.
      apple-runner: '["self-hosted","macOS","apple-imac"]'
      run-swiftlint: true
      build-command: swift build
      test-command: swift test
```

Two runner inputs, routed per job, because the estate has one always-on Mac
slot and `AGENTS.md` requires that only work needing Xcode/macOS occupy it:

- `apple-runner` — `xcodebuild`, `swift build`/`swift test`, anything needing
  the Apple toolchain. Required, no default.
- `lint-runner` — SwiftLint (the official Linux release binary parses Swift
  without compiling), plus `linux-checks` for shell/grep boundary gates and
  plist checks via `python3` `plistlib` (not PlistBuddy/`plutil`, which are
  macOS-only). Defaults to `"ubuntu-latest"`; a repo approved for the
  `linux-ci` organization group should pass that route's `runsOn` object
  instead:

```yaml
      lint-runner: '{"group":"linux-ci","labels":["self-hosted","Linux","X64","proxmox","linux-ci"]}'
      linux-checks: |
        python3 scripts/check_plists.py
        ! grep -rn "import UIKit" Sources/MyCore
```

`run-swiftlint` is **opt-in** and the SwiftLint archive is verified against a
pinned `swiftlint-sha256` before it is unpacked. Enabling SwiftLint on a repo
with no `.swiftlint.yml` runs the full default rule set and is usually red on
first contact — GeoGridKit's first run produced 158 errors — so adopt a
repo-owned config in the same change. Prefer `only_rules:` over
`disabled_rules:` there: an allowlist cannot be broken by a future SwiftLint
release adding a rule.

Archive/sign/notarize/TestFlight/Sparkle are **not** here, for the same reason
`node-library.yml` omits publish: they need temporary keychains, the host-wide
Apple build lock and per-repo credentials, and folding them in would put
release credentials behind a PR-triggered gate. That stays the
`apple-release-pipeline` skill's per-repo workflow.

### `python-data.yml`

`uv` (the default), with a lockfile check:

```yaml
jobs:
  ci:
    uses: narduk-enterprises/workflows/.github/workflows/python-data.yml@v1
    with:
      runner: '{"group":"linux-ci","labels":["self-hosted","Linux","X64","proxmox","linux-ci"]}'
      working-directory: services/my-pipeline
      venv-path: .venv-ci
      uv-sync-args: "--locked --extra test"
      test-command: python -m pytest tests -q
```

pip/venv, for the repos that have not moved to `uv`:

```yaml
    with:
      dependency-manager: pip
      pip-install-args: '-e ".[test]"'
```

Notes:

- The environment's `bin/` is prepended to `$GITHUB_PATH` after install rather
  than `source`-ing an activate script, because activation dies with the step's
  shell. That is what makes `extra-checks` work from any directory.
- `extra-checks` is a multi-line shell hook that runs after the tests, in the
  same environment, and is covered by `ci / Required`. It exists because a
  caller cannot add steps to a called workflow's job, so a repo-specific gate
  that needs the installed package would otherwise have to stay behind as a
  second job duplicating the entire install.
- `run-ruff` is **opt-in** and `ruff-version` is pinned exactly, so a ruff
  release cannot turn a green repo red. Pointing ruff at a repo that never had
  a static-analysis gate is usually red on first contact — narduk-data's
  `earth-data-pipeline` has 36 violations today, including six `F821`
  undefined-name — so the template does not decide for the caller when to take
  that on.
- Several isolated pytest invocations (narduk-data's `ci.yml` needs them,
  because two suites share a module basename with no `__init__.py`) go in
  `test-command` as a multi-line string, or in `extra-checks`.

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

`install-args` and `extra-scripts` were added for narduk-charts' adoption
(company-hq#172) and are worth knowing about, because between them they are
the difference between retiring a local job and retiring half of one:

```yaml
    with:
      package-manager: npm
      install-args: "--legacy-peer-deps" # charts cannot `npm ci` without it
      extra-scripts: size                # runs after build, in the same lane
```

`extra-scripts` runs each named package script with `--if-present` *after*
`build`, in the same job, so a gate that needs build output (`size-limit`)
does not require a second job with a second full install and build.

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

#### Browser shards and the isolated pool

`run-e2e: true` on its own runs the suite as one job on the **same** runner as
the build. That is the right shape for a caller with no isolated pool, and it
stays the default — but it is not the shape any real browser adopter in the
estate uses, and "the callable can't express it" is why five of them
hand-rolled the same thing:

```yaml
jobs:
  ci:
    uses: narduk-enterprises/workflows/.github/workflows/nuxt-cloudflare.yml@v1
    with:
      run-e2e: true
      # Browsers go to the dedicated pool — NOT the guest that runs the build.
      # Paste the route's `runsOn` object verbatim; never hand-copy labels.
      e2e-runner: '{"group":"playwright-isolated","labels":["self-hosted","Linux","X64","proxmox-playwright-x64"]}'
      e2e-shards: 3               # adds --shard=n/3 --reporter=blob + a merge job
      e2e-args: "--project=chromium --workers=1"
      e2e-install-browsers: false # the pool image already has them
      e2e-browsers-path: /opt/playwright-ci/browsers
```

Three things worth stating plainly, because each one is a way this goes wrong:

- **`e2e-runner` names a pool; it does not grant access to one.** A repo that
  is not in the runner-fleet manifest's `playwright-isolated` group must leave
  it empty. Passing a group the repo is not registered for produces a job that
  queues forever, which reads exactly like a hung runner rather than like a
  permissions error. Route additions travel through the fleet manifest flow
  (company-hq#155 → #276), not through this input.
- **Sharding without merging is worse than not sharding.** `e2e-shards > 1`
  therefore also adds an `E2E report` job that merges the blob reports into
  one HTML report, on the plain `runner` — merging is Node work with no
  browser and has no business on the scarce isolated pool. It runs with
  `if: always()` so a *failing* shard still produces the report explaining why.
- **Everything here is backward-compatible by construction.** `e2e-shards: 1`
  (the default) emits no `--shard`, no blob reporter, and no merge job, so a
  caller that never asked for sharding sees byte-identical behaviour. The one
  visible change is the per-shard artifact name
  (`playwright-evidence-<n>`), because `upload-artifact` v4+ rejects duplicate
  artifact names and a fixed name would fail the instant anyone sharded.

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
- **`v1` is a moving major tag and is advanced by hand after a merge**, never
  as a side effect of merging. The CI-5 phase 2 files shipped ahead of the tag
  and `v1` was advanced to cover them afterwards; `apple.yml`,
  `python-data.yml` and `node-library.yml`'s two new optional inputs are
  backward-compatible additions to the same major, so `v1` moves again rather
  than a `v2` being cut. An adopter merged before the tag moves must pin the
  exact commit SHA and switch to `@v1` once the tag covers it.
- Adding a workflow, or adding an **optional** input with a default, is
  within-major. Renaming or newly requiring an input, removing a job, renaming
  a job (which renames the composed check context and silently orphans every
  branch-protection rule that required it), or changing a secret name is a new
  major.
- `nuxt-cloudflare.yml`'s browser-shard inputs (`e2e-runner`, `e2e-shards`,
  `e2e-args`, `e2e-install-browsers`, `e2e-browsers-path`) and its two new
  jobs are within-major on the same rule: five optional inputs whose defaults
  reproduce the previous behaviour, plus added jobs. **Adding a job is not a
  breaking change here specifically because the composed context comes from
  the caller's job id and this workflow's `Required` job** — neither of which
  moved. `v1` moved again rather than a `v2` being cut.

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
