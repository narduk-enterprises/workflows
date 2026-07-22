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
> never pass a self-hosted runner label.** A fork PR on a public caller can run
> attacker-controlled code, so estate self-hosted runners are off-limits here;
> reusable jobs default to GitHub-hosted `ubuntu-latest`.

Fix CI in one place, not 100. Application repos call these workflows via
`workflow_call` instead of blob-copying YAML. This repo replaces the broken
pattern where ~20 repos carried copies of `weekly-drift-check.yml` pointing at
`narduk-enterprises/narduk-nuxt-template/.github/workflows/reusable-quality.yml@main`
— a repo name that no longer exists (renamed to `narduk-template`), so every
scheduled run 404'd silently.

## Catalog

| Workflow | Purpose |
|----------|---------|
| `reusable-weekly-drift-check.yml` | Weekly template-drift + quality check for fleet apps: typecheck, unit tests, and `narduk-fleet check-drift` |
| `reusable-node-ci.yml` | Generic Node CI: lint, typecheck, test, build (pnpm or npm) |

## How to consume

In a caller repo, e.g. `.github/workflows/weekly-drift-check.yml`:

```yaml
name: Weekly Drift Check

on:
  schedule:
    - cron: "0 6 * * 1" # Mondays 06:00 UTC
  workflow_dispatch:

concurrency:
  group: weekly-drift-check-${{ github.ref }}
  cancel-in-progress: true

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

Generic Node CI:

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

- Set `concurrency` in the **caller** — workflow-level concurrency does not
  propagate from called reusable workflows.
- All secrets are optional; package-registry auth is skipped cleanly when no
  token is passed, so public/forked callers still run.
- `reusable-node-ci.yml` defaults to GitHub-hosted `ubuntu-latest`. Private
  repos may pass a manifest-routed self-hosted label via the `runner` input.
  **Public repos must never pass a self-hosted label** (fork PRs would run
  attacker code on estate infrastructure).

## Versioning policy

- **Callers pin `@vN` tags or full commit SHAs, never `@main`.** `@main` is how
  the last outage happened; it is not a supported reference.
- Maintainers cut tags. `vN` major tags (`v1`, `v2`, …) move forward only for
  backward-compatible changes within that major; breaking changes (renamed or
  newly-required inputs, removed jobs, changed secret names) get a new major.
- To adopt a fix, callers bump their pinned tag/SHA deliberately — nothing
  changes under them silently unless they chose a moving `vN` major tag.

## Maintainer conventions

- Third-party and first-party actions are pinned to full commit SHAs with a
  version comment, targeting the current Actions Node runtime.
- Jobs declare minimal `permissions` and explicit `timeout-minutes`.
- This is a private repo shared org-wide: Actions access is set to
  `organization` so other narduk-enterprises repos can call these workflows.
- Estate conventions live in the `ci-workflow-author` skill
  (agent-infrastructure repo); consult it before adding workflows here.
