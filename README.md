# workflows

Shared reusable GitHub Actions workflows for the narduk-enterprises estate (CI-5).

> **This repository is public.** Its own CI and public callers run on
> GitHub-hosted capacity, with no org-variable or Blacksmith routing. Private
> callers retain reusable-workflow compatibility and may use manifest-routed
> self-hosted capacity only where their repository policy permits it. The
> workflows hold no secrets — every
> `workflow_call` secret is optional and skips cleanly, and `apple.yml` and
> `python-data.yml` declare none at all. `reusable-browser-tests.yml` declares
> one optional secret (`NARDUK_PLATFORM_GH_PACKAGES_READ`) that falls back to
> the ephemeral `github.token` when a caller passes nothing.
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

The standalone **browser / Playwright gap** named by company-hq#197 is now
`reusable-browser-tests.yml`. It consumes a same-run production build, fans
Chromium (and opt-in WebKit) into three shards on the manifest-routed isolated
pool, and merges reports back on ordinary Linux CI. The older browser inputs in
`nuxt-cloudflare.yml` remain for backward compatibility; adopters that separate
browser CI use the dedicated callable instead of re-embedding the pool contract
in their app-class workflow.

## Catalog

| Workflow | Purpose |
|----------|---------|
| `apple.yml` | CI gate for Apple repos (Swift packages, iOS/macOS apps): SwiftLint (official Linux binary) and boundary/plist checks on a Linux runner, `swift build` / `swift test` / `xcodebuild` on the repo-scoped Mac. **Two separately-routed runner inputs — that split is the point.** Release/signing stays per-repo |
| `python-data.yml` | CI gate for Python / data-pipeline repos: explicit `uv` or Python provisioning, pytest, opt-in exact-version Ruff and **Pyright** (real static checking, not `py_compile`), plus an `extra-checks` hook |
| `reusable-browser-tests.yml` | Private-repo browser CI: validates the exact manifest browser-group object before any shard is scheduled, consumes a same-run production build, asserts the immutable Playwright package/browser image and real launch, runs three Chromium shards plus opt-in WebKit, then merges 14-day HTML/trace evidence on Linux CI |
| `docs-governance.yml` | Thin generic gate for docs/handbook-shaped repos: checkout, optionally provision Python/Node, run one repo-provided check command. Generalizes company-hq's `handbook-spine-check.yml` / `untangle-project-sync.yml` shape |
| `node-library.yml` | CI gate for `library` / `cli` project-lifecycle surfaces: script-probed lint/typecheck/test/build, with an optional per-package matrix generalizing narduk-libs' `package-gates` + `verify` pattern. See [relationship to `reusable-node-ci.yml`](#relationship-between-node-libraryyml-and-reusable-node-ciyml) below |
| `nuxt-cloudflare.yml` | CI gate for `nuxt-web` / `cloudflare-worker` surfaces: typecheck (worker + Nuxt split, matching hydrogen), optional unit tests, build, optional `extra-scripts`, optional web-foundation conformance check, optional Playwright e2e — optionally **sharded onto a separately-routed browser pool, with blob-report merge** — optional `wrangler deploy --dry-run` validation. CI only — no deploy job (see below) |
| `reusable-node-ci.yml` | Generic Node CI: script-probed lint, typecheck, test, build (pnpm or npm), fail-closed by default through `require-scripts`. Zero live callers as of 2026-07-27 — kept for compatibility; `node-library.yml` is the richer, preferred surface for new adoption |
| `code-review.yml` | **Advisory, default-off, not a CI gate.** Requests one containerized read-only agent review of a PR head from the estate's ephemeral pool, by firing a single `repository_dispatch` at `agent-infrastructure`. No `Required` job, never part of `ci / Required`, and every refusal path (opted out, fork, no secret, dispatch failure) exits SUCCESS. `enabled` defaults to `false`, so adopting the tag that carries it changes nothing until a repo opts in. See [Advisory code review](#advisory-code-review) |
| `closing-syntax-check.yml` | PR-closing-syntax gate (agent-infrastructure#837, #1085): rejects a PR body whose closing keyword is ambiguous (a bare comma-separated list) or sits outside a canonical closing line/list item, and rejects any commit in the PR's own commit range that carries a closing keyword at all — GitHub's squash-merge auto-close scan reads the landed commit message independently of the curated PR body. **Fully self-contained**: the checker's source (canonically `narduk-enterprises/agent-infrastructure`'s `scripts/check_pr_closing_syntax.py`) is vendored directly inside this callable, so an adopting repo needs no local copy at all — see the workflow file's own header for the sync procedure |
| `reusable-weekly-drift-check.yml` | Retired 2026-07-26: no live caller; see workflows#20 and the 2026-07-26 Actions-optimization audit |

All nine shipped callables are `on: workflow_call` only — none of them declare their own
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
| `reusable-browser-tests.yml` | `narduk-enterprises/been-sober-for` (first proof PR) | not yet |
| `docs-governance.yml` | `narduk-enterprises/company-hq` | yes — repo ruleset `require-docs-governance` |
| `node-library.yml` | `narduk-enterprises/narduk-charts` | yes — repo ruleset `require-ci-required` |
| `nuxt-cloudflare.yml` | `hydrogen` | no — `hydrogen` has no branch protection; it called `@v1` unenforced for months, which is the failure mode this column exists to make visible |
| `reusable-node-ci.yml` | none | — |
| `code-review.yml` | none yet — `agent-infrastructure`, `operator-portal`, and `stonx` are the allowlisted launch set | n/a — advisory by design; it must never become a required check |
| `closing-syntax-check.yml` | none yet — a re-home issue is filed on `narduk-enterprises-clients/pacc-trac` (agent-infrastructure#837), the repo the motivating incidents happened in; `narduk-enterprises/agent-infrastructure` stays on its own local, canonical invocation of the same (now commit-scanning) checker rather than adding a redundant cross-repo call to its own required gate | not yet |
| `reusable-weekly-drift-check.yml` | retired — zero live callers verified across `narduk-enterprises` and `narduk-incubator` | — |

## The `ci / Required` convention

This is the actual point of the repo, not an implementation detail.

`docs-governance.yml`, `node-library.yml`, and `nuxt-cloudflare.yml` each end
with a job named **exactly** `Required`. That job `needs:` every other job the
workflow defines, runs with `if: always()`, and explicitly checks each
`needs.<job>.result` — a job that's enabled must report `success`; it may
report `skipped` only when its controlling input is off. In particular,
`run-e2e: true` makes the plan, every E2E shard, and (when sharded) the report
merge mandatory. A skipped enabled job is failure, not an acceptable
substitute for a toolchain check that never ran. `if: always()` jobs succeed by
default if you don't check anything explicitly — these don't skip that check.

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

## Dependency caching: restore on every ref, write only on the default branch

Every Node workflow here (`node-library.yml`, `nuxt-cloudflare.yml`,
`docs-governance.yml`, `reusable-node-ci.yml`) used to hand `cache:` to
`actions/setup-node` and let it manage the whole round trip. That is the wrong
shape, and company-hq#269 measured why.

`setup-node` writes a fresh cache tarball whenever the primary key **misses**,
scoped to the ref the job ran on. A cache written on `refs/pull/N/merge` is
restorable only by that same pull request — no other PR and not the default
branch. So the sequence for any lockfile-changing PR was: miss → install →
pay a `Post Run actions/setup-node` tar for an entry nothing would ever read
→ branch dies → merge to `main` pays the identical tar a second time.

Measured on real runs before the change:

| Repo / job | restore | install | `Post Run setup-node` (save) | job wall |
|---|---|---|---|---|
| `marketing-web` / `Build` | 1s (miss) | 13s | **19s** | 67s |
| `narduk-charts` / `package / default` | 1s (miss) | 7s | **14s** | 52s |
| `status-apps` / browser shards (pool) | 5–7s | 19–20s | **32–54s** | — |
| `earthdata-viewer` / chromium shard | — | 10.6s | **29s avg, 115s max** | — |
| `nvault` / `Verify` | — | 9.7s | **31.5s avg, 315s max** | — |

The debris was visible in the API as well as the clock: `narduk-charts`
carried 8 cache entries, four of them `refs/pull/*` duplicates of a
`refs/heads/main` entry; `vtraceroute` held a 317MB `refs/pull/2/merge` copy
of the 317MB entry `main` already had.

So these workflows now split the two halves explicitly:

- **`actions/cache/restore` on every ref**, with a `restore-keys` prefix so a
  pull request whose lockfile moved still falls back to the default-branch
  entry — which is the entry it actually wants.
- **`actions/cache/save` only when `github.ref_name` equals the caller's
  default branch**, and only when the exact key missed.

`github.*` resolves against the **caller's** event inside a reusable workflow,
so `ref_name` is the caller's branch on a push and `N/merge` on a pull
request. If `github.event.repository` is ever absent the comparison is simply
false and the write is skipped — the safe direction.

Two deliberate exceptions:

- **`python-data.yml` keeps `setup-uv`'s `enable-cache: auto`.** `auto` means
  on for GitHub-hosted, off for self-hosted, which is already correct: a
  persistent linux-ci guest keeps `~/.cache/uv` between jobs, so uploading a
  tarball of an already-warm cache would be a regression for the only current
  adopter. Only `save-cache` is gated, for a future hosted caller.
- **`reusable-weekly-drift-check.yml` keeps `cache: pnpm`.** It is a weekly
  `schedule` on `ubuntu-latest`, so it runs on the default branch almost
  every time — the write it makes is the one the next run reads.

This is not a caller-visible change: no inputs were added or removed, and a
caller pinned to `@v1` picks it up when `v1` moves.

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

**A PUBLIC CALLER MUST NEVER PASS A SELF-HOSTED LABEL.** A fork PR on a public
caller can run attacker-controlled code, so a self-hosted `runner` value hands
that PR estate infrastructure. A repo can also become public later, and `runner` is a
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

### Blacksmith overflow (`BLACKSMITH_RUNNERS_ENABLED`)

D-CI-CAP-1 (c) (2026-09-07, extends D-BLACKSMITH-2; company-hq `DECISIONS.md`,
fleet#337) makes Blacksmith a kill-switched overflow for the `linux-ci`
class's ordinary private CI. Every `runs-on: ${{ fromJSON(inputs.runner) }}`
site in `nuxt-cloudflare.yml`, `node-library.yml`, `docs-governance.yml`,
`python-data.yml`, `closing-syntax-check.yml`, and `code-review.yml`
(16 sites, none of them a deploy-credentialed job — `nuxt-cloudflare.yml`'s
only deploy-shaped job, `deploy-dry-run`, runs `wrangler deploy --dry-run`
with zero Cloudflare secrets in scope) resolves as:

```
runs-on: ${{ fromJSON(vars.BLACKSMITH_RUNNERS_ENABLED == 'true' && inputs.runner != '"ubuntu-latest"' && format('"{0}"', vars.BLACKSMITH_LINUX_LABEL || 'blacksmith-2vcpu-ubuntu-2404') || inputs.runner) }}
```

- **Switch**: the org Actions variable `BLACKSMITH_RUNNERS_ENABLED`
  (default `false`, visibility restricted to private repos). Only the exact
  string `true` selects Blacksmith. A **repo-level** variable of the same
  name overrides the org default for exactly that repo (GitHub's normal
  repo-over-org precedence) — the mechanism for canarying or kill-switching
  one adopter without moving the org default.
- **Label, not a group id**: the vendor's documented
  `blacksmith-2vcpu-ubuntu-2404` string, overridable via
  `vars.BLACKSMITH_LINUX_LABEL`. No Blacksmith runner-group id is pinned
  anywhere — D-BLACKSMITH-4's live proof established that provider-created
  groups are not a stable routing contract; only the label is.
- **Public `node-library.yml` callers are never affected**: its routing
  expressions first require `github.event.repository.private == true`, so they
  ignore both Blacksmith and `CI_LIGHTWEIGHT_RUNNER` for public repositories.
  Their caller-supplied hosted `runner` value remains the route.
- **Manual, not automatic fallback**: GitHub does not move an already-queued
  job to another `runs-on:` target. If Blacksmith cannot schedule or its free
  allowance is exhausted, flip the variable back to `false` (or remove the
  repo-level override) and rerun — same operational shape as every other
  Blacksmith cohort (D-BLACKSMITH-3).
- **Cost boundary** (D-BLACKSMITH-2/3, unchanged): free allowance only, no
  payment method, no paid overage; disable at 2,400 equivalent 2-vCPU
  minutes in a monthly cycle, any non-zero amount due, or any unexpected
  billing state, whichever comes first.
- **Never routes here**: production/deploy jobs (all app-owned and bespoke,
  outside these six CI-only callables), the Playwright/browser class
  (`reusable-browser-tests.yml`'s browser-runner job is untouched — its own
  trust boundary per company-hq `CI-RUNNER-POLICY.md` §5), and Apple builds
  (`apple.yml` is untouched — it has its own D-APPLE-CI-1 ladder).
- **Adding a job is not additive here without checking this section again**:
  a new job that copies `${{ fromJSON(inputs.runner) }}` verbatim does *not*
  get Blacksmith overflow automatically — use the expression above, or it
  silently stays off the overflow route.

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

#### Concurrency is the caller's job, and putting it here would be actively dangerous

**Every caller must set its own workflow-level `concurrency`, as in the
template above. No workflow in this repo declares one, and none ever should.**
This is a hard rule, not a gap waiting to be filled — the structural gate in
`.github/workflows/ci.yml` fails the build if a callable grows a
`concurrency:` block (rule R6).

The reason is stronger than "it does not propagate". It is that a group here is
evaluated in the **caller's** context, which GitHub states plainly:

> A called workflow uses the name of its caller workflow in
> `${{ github.workflow }}`, so using this context as the value of
> `jobs.<job_id>.concurrency.group` in both caller and called workflows will
> cause the caller workflow to be cancelled when the called workflow runs.
>
> — [Reusing workflow configurations](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations)

So the obvious-looking group — `${{ github.workflow }}-${{ github.ref }}`,
which is what almost everyone writes — would collide with the caller's own
group and **cancel the run that is calling us**. Seven repos would start
cancelling their own CI the moment `v1` moved, and the symptom (a run that
cancels itself for no visible reason) points at the adopter, not at here.

A callable-level group that was carefully uniquified to avoid the collision
would still buy nothing: `cancel-in-progress` on the caller's workflow-level
group already supersedes the *entire* previous run, jobs of this callable
included. A second gate underneath it can only add a way to be wrong.

`concurrency` **is** a permitted key on a job that calls a reusable workflow,
so a caller with an unusual need can scope it at `jobs.ci.concurrency` — but
per the same doc, do not reuse the callable's group value there either.

#### Path filtering: use the boolean inputs, never `paths` / `paths-ignore`

**Do not put `paths:` or `paths-ignore:` on a caller's `on:` trigger.** If the
workflow does not run, `ci / Required` is never reported, and GitHub shows a
required check that never arrives as permanently "Expected" — the pull request
becomes unmergeable and stays that way. That is company-hq#146, and it does not
fail loudly; it just quietly stops being mergeable.

The safe mechanism **already exists** and needs no change to these workflows:
the opt-in gates are ordinary boolean inputs, so a caller can compute them from
its own diff and pass the result. A gate turned off this way reports `skipped`,
which every `Required` job already accepts, and `Required` itself still runs and
still reports. The context never disappears.

```yaml
jobs:
  changes: # cheap; no checkout of the heavy tree needed
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions: { contents: read, pull-requests: read }
    outputs:
      code: ${{ steps.f.outputs.code }}
    steps:
      - uses: dorny/paths-filter@<full-sha> # vX.Y.Z
        id: f
        with:
          filters: |
            code:
              - '!(**/*.md|docs/**)'

  ci: # still named `ci`; still composes `ci / Required`
    needs: changes
    uses: narduk-enterprises/workflows/.github/workflows/nuxt-cloudflare.yml@v1
    with:
      run-e2e: ${{ needs.changes.outputs.code == 'true' }}
      wrangler-dry-run: ${{ needs.changes.outputs.code == 'true' }}
```

Which gates each callable exposes this way:

| Callable | Caller-gatable | Always runs |
|---|---|---|
| `nuxt-cloudflare.yml` | `run-e2e` (`e2e`, `e2e-plan`, `e2e-report`), `wrangler-dry-run`, `run-tests` | `build` |
| `apple.yml` | `run-swiftlint` / `linux-checks` (`lint`), `run-build`, `run-tests` | `xcode` |
| `python-data.yml` | `run-ruff` (`lint`), `run-tests`, `run-pyright` | `test` |
| `reusable-browser-tests.yml` | `run-webkit` (`webkit`) | `validate`, `chromium`, `report` |
| `node-library.yml` | `run-lint`, `run-typecheck`, `run-tests`, `run-build` | `package` |
| `reusable-weekly-drift-check.yml` | all three jobs | — |
| `docs-governance.yml`, `reusable-node-ci.yml` | — | the single job |

**The "always runs" column is deliberate and is not a gap to be closed.** Those
jobs are the ones the required check actually certifies. Giving them a path
condition means `ci / Required` can report green on a change that was never
built — a false green, which is strictly worse than the wasted minutes it saves,
and which nobody discovers by looking at a passing pull request. If a caller
wants a docs-only change to cost less, it turns off the *opt-in* gates above and
still builds.

`python-data.yml` declares no secrets. `apple.yml` accepts an optional
`DEPENDENCY_SSH_KEY` for a private SwiftPM repository. Pass a **read-only deploy
key scoped to that dependency**; never a release, signing, or account key.
Build/test rewrite HTTPS package URLs to SSH only for the caller's GitHub
organization, using Git's process environment. A pinned GitHub Ed25519 host
key authenticates the server. The private key lives in a mode-0600 temporary
file removed on success or failure; Git config, Keychain and `GITHUB_ENV` stay
unchanged. Xcode callers should use `-scmProvider system` so resolution uses
this process configuration. Callers that omit the secret keep their existing
behavior. Do not pass credentials to untrusted code or fork pull requests.

```yaml
    secrets:
      DEPENDENCY_SSH_KEY: ${{ secrets.PRIVATE_SWIFTPM_SSH_KEY }}
```

`reusable-browser-tests.yml` is the one exception: it declares a single
optional `NARDUK_PLATFORM_GH_PACKAGES_READ` secret (`required: false`), mirroring
`node-library.yml` and `nuxt-cloudflare.yml`'s existing CI-alias-for-the-PAT
convention, so that a browser CI caller installing cross-repo
`@narduk-enterprises/*` packages can pass a real read credential. A caller that
passes nothing keeps today's behavior unchanged: every consuming step falls
back to the ephemeral `github.token`.

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
- `lint-runner` — SwiftLint plus `linux-checks` for shell/grep boundary gates
  and plist checks via `python3` `plistlib` (not PlistBuddy/`plutil`, which are
  macOS-only). SwiftLint does not compile the project, but its official Linux
  binary still dynamically loads `libsourcekitdInProc.so`; self-hosted
  `linux-ci` runners provide the pinned Swift SourceKit runtime layer. Defaults
  to `"ubuntu-latest"`; a repo approved for the `linux-ci` organization group
  should pass that route's `runsOn` object instead:

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

The checksum verifies the downloaded SwiftLint archive; it does not provide the
Swift SourceKit runtime. The install step separately fails closed unless
`/usr/lib/libsourcekitdInProc.so` is readable, exports that exact path through
`LINUX_SOURCEKIT_LIB_PATH`, and still locates the unpacked `swiftlint` binary
with `find` because upstream archive layouts have changed.

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
- `run-pyright` is **opt-in** for `v1` compatibility, but it is a real static
  gate: the workflow provisions Node 24 explicitly, installs exact
  `pyright-version` under `$RUNNER_TEMP`, and runs it in the installed Python
  environment. This is intentionally not `py_compile`, which proves syntax and
  nothing about names or types.
- Several isolated pytest invocations (narduk-data's `ci.yml` needs them,
  because two suites share a module basename with no `__init__.py`) go in
  `test-command` as a multi-line string, or in `extra-checks`.
- **`extra-env` values are expanded on the runner. Write `$GITHUB_WORKSPACE`,
  never `${{ github.workspace }}`** (workflows#4):

  ```yaml
      # RIGHT — expanded on the runner by the workflow itself
      extra-env: |
        PYTHONPATH=$GITHUB_WORKSPACE

      # WRONG — silently becomes `PYTHONPATH=` and breaks a later step
      extra-env: |
        PYTHONPATH=${{ github.workspace }}
  ```

  `with:` inputs are evaluated in the **caller**, and a `jobs.<id>.uses:` job is
  never assigned a runner, so `github.workspace` there is the empty string.
  Until this was fixed the workflow accepted `PYTHONPATH=` as a well-formed
  `KEY=VALUE` and the failure surfaced four steps later as
  `ModuleNotFoundError: No module named 'pipelines'`, with nothing anywhere
  naming `extra-env`.

  `$NAME` and `${NAME}` are expanded against the runner's environment. Nothing
  else is: no `$(...)`, no backticks, no `${NAME:-default}`, no globbing, no
  `eval`. An empty value — or one whose every reference is unset — is now a
  **hard error** naming this trap, because a silently-unset variable is the
  worst outcome. `scripts/test_extra_env.py` locks all of that down against the
  step text extracted from the YAML itself, so the tests cannot drift from the
  shipped script.

### Central lightweight-job routing

`nuxt-cloudflare.lightweight-runner` routes E2E plan, E2E report and Required
independently of the build. Selection is explicit input, then
`CI_LIGHTWEIGHT_RUNNER`, then the existing build/Blacksmith route. Public
callers always use `ubuntu-latest` for these three jobs. An unset override is
backward compatible; required check names and failure/skip semantics are unchanged.
Report merging still installs only the pinned Playwright tooling (workflows#49).


`CI_LIGHTWEIGHT_RUNNER` is an organization Actions variable containing a JSON
`runs-on` value, initially `"ubuntu-slim"` for the authorized company gates.
Every shared `Required` job reads it. Changing this one value changes routing
for subsequent jobs without changing callable code or repinning callers.
Package, browser, Apple, and deployment jobs retain their own routes.
`node-library.required-runner` remains an explicit per-caller override; avoid
setting it on ordinary callers that should follow the central route. Repository
variables take precedence over organization variables, so reserve repository
values for documented exceptions.

Inline planners or final assertions need a one-time adoption of the same
expression, preserving their job names and dependency conditions:

```yaml
runs-on: ${{ fromJSON(vars.CI_LIGHTWEIGHT_RUNNER || '"ubuntu-slim"') }}
```

Use this only for bounded, secrets-free metadata and result checks with no
private-network requirement. Their existing policy authorization still applies.
It is a routing contract, not automatic classification by job duration. Already
queued jobs keep their selected route. A composite action cannot select a
runner because it starts after GitHub has assigned one.

Consumers pinned before this feature need one reviewed SHA update. That initial
adoption is unavoidable; later capacity changes require only the organization
variable. Keep immutable workflow pins. Unsetting the variable restores each
callable's prior fallback, and malformed JSON fails visibly. Public callers must
continue to use GitHub-hosted runners; never give the variable a self-hosted
route in an organization that exposes it to public callers.

### `node-library.yml`

`required-runner` optionally separates the small `Required` aggregation job
from the package runner. It accepts the same JSON runner shape as `runner`;
empty uses the organization-level `CI_LIGHTWEIGHT_RUNNER` route for private
callers, falling back to existing routing (including Blacksmith) when that
variable is absent; public callers retain their supplied hosted route. An
explicit value takes precedence for `Required` only. The gate checks dependency results
without checking out source, installing packages, or receiving registry secrets.
For a secrets-free gate before a privileged self-hosted release, the caller may
pass `required-runner: '"ubuntu-slim"'` under company-hq's CI runner policy
exception 2. This keeps completion reporting independent of a saturated build
queue. It does not grant other callers a hosted-runner policy exception.

A monorepo may group packages into a bounded number of install lanes. Use
`filter: ""`, a lane-level `extra-scripts: "ci:batch"`, and set all four
`run-*` inputs to `false`. The root `ci:batch` script receives the complete
lane in `PACKAGE_MATRIX_JSON`; for example, a lane can carry
`packages: ["@example/core", "@example/auth"]`. That repository-owned script
must validate every selection, reject missing gates, retain every nonzero
exit and print per-package results. Run the same script locally. The callable
continues to own runner setup, one install per lane and auth cleanup; existing
single-package lanes are unchanged. Choose the batch count from measured
setup cost and capacity, and retain the caller's final integration aggregate.


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

`extra-scripts` runs each named package script *after* `build`, in the same job,
so a gate that needs build output (`size-limit`) does not require a second job
with a second full install and build. Each name is probed first and fails when
it is absent by default.

With a per-package matrix (generalizes narduk-libs' `package-gates`):

```yaml
jobs:
  ci:
    uses: narduk-enterprises/workflows/.github/workflows/node-library.yml@v1
    with:
      package-matrix: |
        [
          {
            "label": "narduk-core",
            "filter": "@narduk-enterprises/narduk-core",
            "extra-scripts": "check:dist"
          },
          {
            "label": "narduk-auth",
            "filter": "@narduk-enterprises/narduk-auth",
            "extra-scripts": ""
          }
        ]
    secrets:
      NARDUK_PLATFORM_GH_PACKAGES_READ: ${{ secrets.NARDUK_PLATFORM_GH_PACKAGES_READ }}
```

Matrix lanes own their extras. The optional lane-level `extra-scripts` field
overrides the legacy shared input even when it is `""`; that explicit empty
value means the lane has no extra gates. Omitting the field preserves the
shared-input behavior for existing callers, while declaring it on every lane
prevents one package's requirement from leaking into another.

The org Actions secret `NARDUK_PLATFORM_GH_PACKAGES_READ` maps into
`GH_PACKAGES_READ` on the auth and install steps. The legacy process alias is
also supplied for existing caller bootstrap scripts; it is an interface
compatibility detail, not another secret to create. There is no implicit
`github.token` fallback. Missing credentials fail before private-package
installs; public-only installs need no credential. Without a caller bootstrap,
the callable writes a temporary user config containing a literal variable
reference, then removes it after the install.

A public monorepo with only workspace packages under an estate-looking scope
can opt out of that automatic name-based detection without forwarding a token:

```yaml
jobs:
  ci:
    uses: narduk-enterprises/workflows/.github/workflows/node-library.yml@v1
    with:
      runner: '"ubuntu-latest"'
      required-runner: '"ubuntu-latest"'
      package-registry-auth: disabled
```

`disabled` asserts that every installed dependency is public. The default
`auto` remains fail-closed for private registry dependencies and preserves the
existing package-read-secret contract for private callers.

### `nuxt-cloudflare.yml`

```yaml
jobs:
  ci:
    uses: narduk-enterprises/workflows/.github/workflows/nuxt-cloudflare.yml@v1
    with:
      node-version: "24"
      package-manager: npm # hydrogen's current package manager; pnpm is the default
      # Optional: delegate the complete install to a caller-owned wrapper.
      # The callable passes the mapped secret only as NVAULT_TOKEN and skips
      # its legacy direct package-registry materialization/install path.
      install-script: ci:install
      typecheck-worker-script: typecheck
      typecheck-web-script: web:typecheck
      run-e2e: true
      wrangler-dry-run: true
    secrets:
      NARDUK_PLATFORM_GH_PACKAGES_READ: ${{ secrets.NVAULT_TOKEN }}
```

`install-script` is for repositories whose install wrapper exchanges an nVault
service token for the package credential, materializes any temporary registry
configuration itself, runs the package manager, and removes the configuration on exit.
The value must be one package.json script name using letters, digits, `:`, `_`,
or `-`; the callable rejects missing or unsafe names. Existing callers that
leave it empty keep the legacy install path unchanged.

The legacy callable input can carry either a direct package PAT or, only with
an explicitly selected caller-owned `install-script`, an nVault service token.
It does **not** follow that the org package PAT belongs in `NVAULT_TOKEN`.
For `install-script: ci:install`, pass the consumer's `secrets.NVAULT_TOKEN`
into the callable input, as the example above does. That installer resolves
`GH_PACKAGES_READ` from nVault before starting npm/pnpm. Raw PAT consumers omit
`install-script` and pass `secrets.NARDUK_PLATFORM_GH_PACKAGES_READ` instead.

Local workstations use `gh-packages-run`, and Workers Builds uses the protected
build secret `GH_PACKAGES_READ`. Neither uses the Actions input name as a vault
key. [The credential route](https://github.com/narduk-enterprises/agent-infrastructure/blob/main/docs/agents/credentials.md)
provides the exact nVault selector and value-free diagnostics.

Deploying with real Cloudflare credentials on push-to-main is **not** this
workflow's job — that stays a separate `nuxt-cloudflare-deploy.yml` sibling
(deferred, not built in this pass), matching hydrogen's existing two-job
`ci` / `deploy` split rather than folding deploy secrets into the CI gate.

#### The unit-test lane and `extra-scripts`

```yaml
    with:
      run-tests: true       # opt-in — see below for why the default is false
      test-script: test     # vitest, jest, whatever the repo already runs
      extra-scripts: check:vendor
```

Until `run-tests` existed this workflow had **no unit-test expression at all**,
while `node-library.yml` had one. A Nuxt app with a vitest suite therefore
could not adopt its own class's callable without dropping its unit tests — the
same defect the browser-shard gap was, and the same consequence: the repo keeps
hand-rolling. `earthdata-viewer` (company-hq#278) is the adopter that surfaced
it; its `unit` job ran `npm test` and had nowhere to go.

**`run-tests` defaults to `false` on purpose**, even though the script runs
`--if-present`. `test` is a near-universal `package.json` script, so a default
of `true` would hand every existing adopter a brand-new gate the instant the
moving `v1` tag advanced — and a suite that was never in a repo's CI turning
its default branch red is not a backward-compatible change, whatever the input
is labelled. Existing callers opt in when they mean to.

`extra-scripts` mirrors `node-library.yml`'s input of the same name and runs
after `build-script` in the same lane, so a gate needing build output doesn't
pay for a second install. earthdata-viewer's vendored-package pin check is the
first case.

#### `require-scripts`: a lane that matched no script is not a passing lane

Every gate in this file used to run `--if-present`, which means a gate whose
script does **not** exist matches nothing, exits 0, and reports a **green lane
that ran nothing**. `run-tests: true` is a caller *asserting* tests exist; the
callable was taking that assertion on faith.

`node-library.yml` got the first fix in workflows#14. This file did not, and
the missing `web:typecheck` default became the dominant dead lane across its
adopters. `typecheck-web-script` now defaults to `""`: a second web typecheck
is opt-in, while a non-empty name remains an assertion that the script exists.

Every gate now probes for the script first, and reacts by `require-scripts`:

| `require-scripts` | script missing | effect |
|---|---|---|
| `false` (explicit remediation opt-out) | named but absent | `::warning::` + a job-summary line naming the script; lane still green |
| `true` (default) | named but absent | `::error::` and the job **fails** |
| either | **empty script name** | lane skipped silently — the caller declared it absent |

The default is now `true`. The migration first declared absent lanes and proved
every remaining name; callers retain `false` only as an explicit, temporary
remediation opt-out.

A caller declares absent lanes by passing an empty script name. This is what
makes `require-scripts` usable when some repos genuinely have no build or split
typecheck lane — *"I have no web surface"* remains distinct from *"I named a
script that isn't there"*:

```yaml
    with:
      typecheck-worker-script: typecheck
      typecheck-web-script: ""   # no web surface in this repo
      build-script: ""           # no build step; wrangler dry-run IS the build
      run-tests: true
      require-scripts: true      # now every remaining lane is proven to run
```

`software-delivery` is the reference caller for that shape.

The gate text is not tested as a copy: `scripts/test_script_gates.py` extracts
each gate's `run:` block from the YAML and executes that exact text against real
`package.json` fixtures (54 cases across both Node callables), including
colon-bearing names like `web:typecheck` — the probe resolves scripts through
`npm pkg get scripts.<name>`, a dot-path, and a name that broke that lookup
would report every colon-bearing script as missing.

#### Browser shards and the isolated pool

New browser adopters separate application CI from browser execution. The
application-class callable produces one same-run build artifact; the standalone
browser callable consumes it without rebuilding:

```yaml
jobs:
  ci:
    uses: narduk-enterprises/workflows/.github/workflows/nuxt-cloudflare.yml@v1
    with:
      runner: '{"group":"linux-ci","labels":["self-hosted","Linux","X64","proxmox","linux-ci"]}'
      working-directory: apps/web
      run-e2e: false
      e2e-build-artifact-path: .output

  browser:
    needs: ci
    uses: narduk-enterprises/workflows/.github/workflows/reusable-browser-tests.yml@v1
    with:
      linux-runner: '{"group":"linux-ci","labels":["self-hosted","Linux","X64","proxmox","linux-ci"]}'
      browser-runner: '{"group":"playwright-isolated","labels":["self-hosted","Linux","X64","proxmox-playwright-x64"]}'
      working-directory: apps/web
      build-artifact-path: .output
      build-artifact-marker: server/index.mjs
      playwright-version: 1.61.1
      e2e-script: test:e2e:ci
      chromium-args: "--project=web --workers=1"
      shards: 3
```

The two route inputs are not suggestions. Resolve both from the fleet manifest
and copy each `runsOn` object verbatim. A hosted `contract` job checks the exact
group and ordered labels before any caller-controlled self-hosted route is
scheduled. It also rejects absolute or escaping artifact paths, unsupported
package managers, non-exact Playwright versions, and invalid shard counts.
`Required` is hosted for the same reason: even a bad route still produces a
visible failing gate instead of scheduling the failure aggregator on that bad
route.

Chromium and opt-in WebKit are the only jobs on
`proxmox-playwright-x64`. Production-build validation and report merging run on
`linux-ci`. The browser jobs declare no secrets and receive only GitHub's
short-lived token for checkout, same-run artifact transfer, and optional
package reads. The caller must provide workflow-level concurrency because
overlapping runs share fixed runner paths; the callable deliberately declares
none.

The pool's current defects are treated as fail-closed constraints:

- agent-infrastructure#323: the root-owned `/opt/playwright-ci` browser tree is
  never written and no `playwright install` fallback exists. Caller pin,
  installed packages, image package, manifests, executable ancestry, and a
  real headless launch must all agree.
- agent-infrastructure#267: the workflow does not alter guest firewall or DNS
  state, add sleeps, or hide egress failure.
- agent-infrastructure#248/#237: workflow code never manipulates leases or
  quarantine. An unavailable guest can leave work queued, but it cannot become
  a skipped green; every enabled shard result is mandatory in `Required`.

When the producer is an app-owned build job, export the upload step's
`artifact-id` as a job output and pass it as `build-artifact-id`. This keeps
failed-job reruns tied to the successful producer. Omitting it retains
the existing same-run, same-attempt artifact-name convention.

The older `nuxt-cloudflare.yml` `e2e-*` inputs remain supported for existing
callers and for repos without isolated-pool approval. New isolated-pool
adoptions use the standalone callable so the pool contract has one owner.

#### Reusing build output in E2E

The Nuxt callable reuses build output by default when `run-e2e` is enabled.
It requires exactly one of `.output/server/index.mjs` or
`apps/web/.output/server/index.mjs` under `working-directory`. Other layouts
name their output explicitly:

```yaml
      run-e2e: true
      e2e-build-artifact-path: apps/web/.output
```

The build job uploads that relative path only after its gates pass. Every E2E
shard downloads it to the same path and receives
`E2E_PREBUILT_ARTIFACT=1`. The consumer's launcher must treat that variable as
an assertion: validate the expected entry points and fail when any are absent,
rather than silently rebuilding. A missing upload already fails through
`if-no-files-found: error`. Hidden files are included because Nuxt's canonical
output directory is `.output`; the input is an explicit caller-selected path,
not a repository-wide artifact sweep.

The artifact name includes `github.run_id` and `github.run_attempt`. Embedded
E2E shards download the exact artifact ID exported by their successful build
job, so rerunning failed shards can reuse that build even when the attempt
number advances. Retention is one day. The default `auto` produces no artifact
when E2E is disabled; an explicit path also supports the standalone browser
callable. Set an explicitly empty path only for suites that do not test a
built application. Launchers must honor `E2E_PREBUILT_ARTIFACT=1`: the workflow
cannot suppress a build hardcoded inside an application script.

#### Skipping E2E on docs-only PRs (`e2e-skip-paths`)

(workflows#49) `e2e-skip-paths` is a space-separated list of glob patterns
(`*` and `**` supported), relative to the repository root. When `run-e2e` is
true, the triggering event is `pull_request`/`pull_request_target`, and
**every** changed file in that PR matches at least one pattern, `E2E plan`
emits an empty shard list and `E2E` / `E2E report` both report `skipped` —
`Required` still gates them, expecting `skipped` rather than an absent check,
so a workflow-expression mistake cannot silently turn "never checked" green.
A push event (e.g. the default branch) always runs the full suite regardless
of this input: the skip is a PR fast path, not a weaker canonical-branch gate.

```yaml
      run-e2e: true
      e2e-skip-paths: "**/*.md design/** docs/** .lane-evidence/** LICENSE"
```

Never include a CSS pattern here — a CSS-only diff is expected to run E2E in
full, on purpose (overflow and mobile-layout specs exist specifically to
catch CSS regressions).

The decision is made by calling `gh api repos/<repo>/pulls/<n>/files
--paginate` from the `E2E plan` job, so it needs no repository checkout of
its own. `gh` is standard on GitHub-hosted runners but self-hosted runners
provision their own toolchains — its absence degrades to "run the full
suite" (a warning, not a failure), same as zero reported changed files or a
missing PR number. An empty `e2e-skip-paths` (the default) never skips and
adds no behavior for existing callers.

#### The isolated Playwright toolchain gate

`playwright-isolated` is download-free and image-owned. The current pool pins
Playwright `1.61.1`: Chromium headless shell revision `1228` (Chromium
`149.0.7827.55`) and WebKit revision `2311` (WebKit `26.5`). The image keeps
the package, `browsers.json`, and browser payloads root-owned/read-only under
`/opt/playwright-ci`; the ephemeral guest exports a job-visible symlink tree
through `PLAYWRIGHT_BROWSERS_PATH`.

Before a shard starts its suite, the callable now fails unless all of these are
true:

- the caller directly pins `@playwright/test` to an exact version (no caret,
  tilde, tag, or range);
- that pin, the installed `@playwright/test`, installed `playwright-core`, and
  image Playwright version are identical;
- the caller and image `browsers.json` SHA-256 values match, and each requested
  engine's revision/upstream version matches;
- the selected executable exists, resolves into the immutable image browser
  tree, has root-owned non-writable ancestry, and passes a real headless launch
  canary;
- `PLAYWRIGHT_BROWSERS_PATH` is the absolute `/opt` path exported by the guest,
  never a workspace or `$RUNNER_TEMP` cache.

The isolated-route guard runs before dependency installation. It rejects
`e2e-install-browsers: true` and every `e2e-browsers-path` override, while the
installer step independently excludes both the `playwright-isolated` group and
`proxmox-playwright-x64` label. A mismatch names consumer/image versions,
manifest digests, browser revisions, and the upgrade choice, then exits
non-zero without invoking an installer. Hosted browser jobs may still opt into
the explicit installer because they own their toolchain rather than consuming
this pool.

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

### Weekly drift check (retired 2026-07-26)

`reusable-weekly-drift-check.yml` was removed after a fresh organization-wide
caller search found no live consumer (`workflows#20`, Actions-optimization
audit). `narduk-template-smoke-app` is disabled and documentation references
were not runtime callers.

### Generic Node CI

```yaml
jobs:
  ci:
    uses: narduk-enterprises/workflows/.github/workflows/reusable-node-ci.yml@v1
    with:
      node-version: "22"
      run-lint: true
      run-tests: true
      require-scripts: true
    secrets:
      NARDUK_PLATFORM_GH_PACKAGES_READ: ${{ secrets.NARDUK_PLATFORM_GH_PACKAGES_READ }}
```

Notes:

- Secrets are optional for public-only dependencies; private package installs fail early when no
  token is passed, so public/forked callers still run.
- Every enabled lint/typecheck/test/build lane probes for its package script
  before running it. Missing scripts fail by default. A caller with no such
  lane disables the matching `run-*` input; `require-scripts: false` is only
  an explicit temporary remediation opt-out and emits a visible warning.
- **Public repos must never pass a self-hosted `runner`/label** (fork PRs
  would run attacker code on estate infrastructure) — see "Runner routing"
  above.

## Advisory code review

`code-review.yml` is the odd one out in this repository, and it is worth
understanding why before adopting it: **it is not a CI gate.** Every other
callable here exists to produce `ci / Required`. This one produces nothing a
branch ruleset can require, has no `Required` job, and cannot fail your build.
It asks the estate's ephemeral agent pool for one read-only review of a pull
request head, and the review arrives — or does not — as a comment on the PR.

That framing is load-bearing. A review request that can redden CI turns an
optional quality aid into an outage every time the pool is busy, the dispatch
token rotates, or the network hiccups. So every refusal path exits SUCCESS with
a `::notice::` naming which one fired:

| Condition | Result |
|---|---|
| `enabled` not passed (the default) | job skipped, nothing dispatched |
| PR carries the `no-ai-review` label | `review skipped: opted out` |
| PR head is a fork | `review skipped: ... head is a fork` |
| `AGENT_REVIEW_DISPATCH_TOKEN` not available | `review skipped: ... not available` |
| the dispatch call fails or returns non-204 | `::warning::`, job still green |
| the pool is busy (decided downstream) | nothing queues, nothing retries |

### Consuming it

```yaml
  code-review:
    uses: narduk-enterprises/workflows/.github/workflows/code-review.yml@v1
    with:
      enabled: true
      review-tier: cheapest-capable
      runner: '{"group":"linux-ci","labels":["self-hosted","Linux","X64","proxmox","linux-ci"]}'
    secrets:
      AGENT_REVIEW_DISPATCH_TOKEN: ${{ secrets.AGENT_REVIEW_DISPATCH_TOKEN }}
```

Call it as a job **beside** your `ci` job, never inside its `needs:` chain —
putting it upstream of `Required` would reintroduce exactly the coupling the
advisory design removes.

Adding `enabled: true` is not sufficient on its own: the receiving repository
keeps a closed allowlist (`Config/agent-review-repos.json` in
`agent-infrastructure`) and refuses a dispatch from anything absent from it.
Enabling a new repo is therefore two one-line changes in two repositories, on
purpose — one caller-side opt-in and one estate-side admission.

### What the reviewer can and cannot do

The container holds a read-only clone and a `contents: read` token. It cannot
push, cannot approve, cannot dismiss a review, and cannot reach the pull
requests API. Its whole output is one comment. Findings are required to name a
`file:line` the reviewer actually opened, and PR content is treated as
untrusted data rather than as instructions — the brief that says so lives in
`agent-infrastructure` at
`skills/proxmox-agent-execution/references/agent-review-brief.md`, so tuning the
reviewer is a docs pull request there rather than a change here.

Refs `narduk-enterprises/agent-infrastructure#333` (D-AGENT-POOL-1).

## Relationship between `node-library.yml` and `reusable-node-ci.yml`

`reusable-node-ci.yml` already does the core of this — script-probed
lint/typecheck/test/build, pnpm or npm — and a GitHub code search across
`narduk-enterprises`, `narduk-incubator`, `narduk-enterprises-clients`, and
`loganrenz` (2026-07-27, `reusable-node-ci`)
found **zero** repos currently calling it. So `node-library.yml` did not
migrate a live caller — it was additive — and hardening this compatibility
surface does not disrupt a current consumer.

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
- **A fan-out canary precedes an advance**: before moving the tag, trigger or
  find at least one adopter run on the new commit and confirm it resolves and
  succeeds, per Logan, 2026-09-04: "Fan-out canary before the tag advances
  (Recommended)" (Refs company-hq#536).
- Adding a workflow, or adding an **optional** input with a default, is
  within-major. Renaming or newly requiring an input, removing a job, renaming
  a job (which renames the composed check context and silently orphans every
  branch-protection rule that required it), or changing a secret name is a new
  major.
- **A new job-level `permissions:` scope is a BREAKING change, not an
  addition.** A job in a called workflow may only request permissions the
  caller granted on its `uses:` job; ask for one it did not and GitHub fails
  the caller's *entire run* at startup — `startup_failure`, `jobs: []`, no
  logs, no check-run annotation — before any job is created. Nothing in the
  adopter's repository changed, so it reads as an infrastructure outage rather
  than an interface break, and it lands on every `@v1` adopter simultaneously
  the moment the tag moves. On 2026-09-04 `pull-requests: read` on
  nuxt-cloudflare.yml's `E2E plan` job did exactly that to harvest-tracker,
  marketing-web, vtraceroute and hydrogen, while the two repos pinned to older
  commit SHAs kept running (workflows#59). Each callable's permitted set is
  declared in `scripts/lint_callables.py`'s `CALLER_GRANTS` and enforced as
  **R12**; widening one means updating every adopter's `permissions:` block
  first, then the map, and only then moving the tag.
- `nuxt-cloudflare.yml`'s browser-shard inputs (`e2e-runner`, `e2e-shards`,
  `e2e-args`, `e2e-install-browsers`, `e2e-browsers-path`) and its two new
  jobs are within-major on the same rule: five optional inputs whose defaults
  reproduce the previous behaviour, plus added jobs. **Adding a job is not a
  breaking change here specifically because the composed context comes from
  the caller's job id and this workflow's `Required` job** — neither of which
  moved. `v1` moved again rather than a `v2` being cut.
- `reusable-browser-tests.yml` is a new callable, so its required route,
  artifact, and exact-version inputs do not break an existing caller.
  `python-data.yml`'s `run-pyright`, exact `pyright-version`, and
  `pyright-args` inputs are optional additions; existing Python callers keep
  their prior behavior until they opt into static analysis.
  `reusable-browser-tests.yml`'s later addition of the optional
  `NARDUK_PLATFORM_GH_PACKAGES_READ` secret (`required: false`) is within-major on the same rule as a new optional
  input: a caller that passes nothing gets byte-identical behavior to before
  the secret existed (workflows#50).
- `nuxt-cloudflare.yml`'s `run-tests` / `test-script` / `extra-scripts` are
  within-major on the same rule — three optional inputs, no new job, one
  conditional step each. **`run-tests` defaults to `false` precisely so that
  it is within-major**: `test` is a near-universal package script, so
  defaulting it on would have made a moving `v1` tag introduce a gate to
  callers who never asked for one, which is a breaking change dressed as an
  additive input. Getting the *default* wrong is how an "additive" change
  breaks people.
- `nuxt-cloudflare.yml`'s `foundation-check` / `foundation-check-tool-version`
  (company-hq docs/WEB-FOUNDATION-CHECK.md, D-WEBFOUND-2 Q5/Q9 (a),
  D-WEBFOUND-3) are within-major on the same rule — two optional inputs, no
  new job, three added steps inside the existing `build` job, `Required`'s
  `needs:` graph unchanged. **`foundation-check` defaults to `false`** for the
  same reason `run-tests` does: the web-foundation program is a multi-wave
  fleet migration (D-WEBFOUND-2 Q4/Q10), most fleet apps do not conform to
  the seven-item contract yet, and a moving `v1` tag must not hand every
  existing adopter a brand-new red gate the day the tag advances.
- `foundation-check-auth` defaults to `package-token`. Callers whose
  `NARDUK_PLATFORM_GH_PACKAGES_READ` secret mapping carries an nVault service
  token set `foundation-check-auth: nvault`. The callable resolves the existing
  package-read grant for that one step and supplies `NODE_AUTH_TOKEN` to both
  the installed checker and the optional pinned download. The checker needs
  this credential for its live N-1 registry lookup even when dependencies are
  already installed. Neither the service token nor the resolved package token
  is exported to later steps; temporary npm configuration contains only an
  environment-variable reference. Missing or rejected credentials leave a
  blocking UNKNOWN artifact rather than falling back to `github.token`.
- The D-CI-CAP-1 (c) Blacksmith-overflow change (see "Blacksmith overflow"
  above) is within-major on the same rule: no new input, no new job, no new
  job-level `permissions:`, and the default (`vars.BLACKSMITH_RUNNERS_ENABLED`
  undeclared) reproduces every existing adopter's behavior byte-for-byte.
  `v1` moves again rather than a `v2` being cut, behind the usual fan-out
  canary.

## Maintainer conventions

- **`.github/workflows/ci.yml` gates this repo** (~7s). `actionlint` +
  `scripts/lint_callables.py` + behavior tests including
  `scripts/test_playwright_toolchain.py`. The structural gate
  enforces every convention in this list, so none of them can regress silently:
  see the rule table (R1–R11) at the top of `scripts/lint_callables.py`. Run it
  locally before pushing: `python3 scripts/lint_callables.py`.
- Third-party and first-party actions are pinned to full commit SHAs with a
  version comment, targeting the current Actions Node runtime (enforced: R4).
  All nine pins currently resolve to their claimed tags and every one runs on
  `node24`. `astral-sh/setup-uv` is deliberately held at `v8.3.2` rather than
  `v9.0.0`: v9's sole breaking change flips `prune-cache` to `false`, which
  would grow cache usage for every adopter, and v8.3.2 is already on the current
  runtime — so the bump would be a behaviour change with no hardening benefit.
- Jobs declare minimal `permissions` and explicit `timeout-minutes` (enforced:
  R1/R2/R3).

### Timeout basis

Every job has had a finite `timeout-minutes` since the day it was written — the
guard is not new. What was missing was a *basis*. Measured 2026-07-25 from the
**jobs** endpoint, counting **only jobs the callable actually composed** (a
`ci / …` or `drift-check / …` name), execution time only with queue excluded,
never extrapolated from a run count.

That filter is the whole measurement, not a detail. A first pass that matched on
the bare job name mixed each adopter's *pre-adoption* local job into the same
bucket and reported `python-data / test` at p95 444s / max 722s. Split
correctly, the callable's `ci / test` is p95 90s / max 91s and the 722s belongs
to the local `test` job narduk-data ran before it adopted — a 5× error, in the
direction that would have made a fine timeout look nearly breached.

| Callable | Job | Timeout | p50 | p95 | max | n | repos | ×p95 |
|---|---|---|---|---|---|---|---|---|
| `apple.yml` | `xcode` | 45 (`xcode-timeout-minutes`) | 116s | 228s | 231s | 8 | 4 | 11.8× |
| `apple.yml` | `lint` | 15 (`lint-timeout-minutes`) | 8s | 12s | 12s | 4 | 2 | 75.9× |
| `docs-governance.yml` | `check` | 15 | 10s | 18s | 22s | **39** | 1 | 49.7× |
| `node-library.yml` | `package / <label>` | 30 | 78s | 289s | 298s | 17 | 4 | **6.2×** |
| `nuxt-cloudflare.yml` | `Build` | 30 | 72s | 131s | 145s | 16 | 4 | 13.8× |
| `nuxt-cloudflare.yml` | `Deploy dry run` | 15 | 18s | 20s | 20s | 4 | 1 | 45.7× |
| `python-data.yml` | `test` | 30 (`test-timeout-minutes`) | 75s | 90s | 91s | 5 | 1 | 20.0× |
| `reusable-weekly-drift-check.yml` | `Typecheck` | 15 | 69s | 69s | 69s | 1 | 1 | 13.0× |
| `reusable-weekly-drift-check.yml` | `Unit Tests` | 15 | 50s | 50s | 50s | 1 | 1 | 18.0× |
| `reusable-weekly-drift-check.yml` | `Template Drift Check` | 10 | 45s | 45s | 45s | 1 | 1 | 13.3× |
| *(all five)* | `Required` | 5 | 4–5s | 5–7s | 7s | 83 | 11 | 43–65× |

**Jobs with no data at all** — their timeouts are declared, finite, and
unmeasured. Do not read the numbers above onto them:

| Callable | Job | Timeout | Why nothing ran |
|---|---|---|---|
| `nuxt-cloudflare.yml` | `E2E`, `E2E plan`, `E2E report` | 30 / 5 / 15 | **never executed on any adopter.** hydrogen and software-delivery set `run-e2e: false`; marketing-web and vtraceroute leave it at the default. 35 skipped instances, 0 runs |
| `python-data.yml` | `lint` | 10 | narduk-data leaves `run-ruff` false — skipped in every run |
| `reusable-node-ci.yml` | `ci` | 20 | **zero adopters, estate-wide.** Nothing has ever run it |

**Nothing was changed as a result.** Every measured timeout sits between 6.2×
and 76× its observed p95, so none is close to producing a false red. The one job
near the 4–6× target band is `node-library / package` (6.2×), which is correct
as-is. The rest are looser than a band would suggest, deliberately:

- **The sample is tiny and 24 hours old.** Composed jobs first appear
  2026-07-24T21:54Z and most adopters landed the next day. Only
  `docs-governance / check` (n=39) is a distribution; below n≈10 a percentile is
  one observation wearing a hat.
- **For a short job the floor is not p95.** It is "long enough that a cold cache
  or a slow guest is not a false red", which is minutes regardless of a 12s p95.
- **`apple / xcode` at 11.8× is the loosest that matters**, because it holds the
  estate's single Mac slot. It stays: n=8 across four small Swift repos is far
  too thin to justify tightening a real iOS archive toward ~20 minutes, and it
  is already a caller-tunable input. An adopter that knows its build should set
  it.

The regression guard is rule R2 in `scripts/lint_callables.py`, which makes it
impossible to add a job without a finite timeout — including via an input whose
numeric default was removed.

**`timeout-minutes` measures execution, never queue — and here that gap is
enormous.** `docs-governance / check` executes in 10s and has waited **1973s
(33 min)** for a `linux-ci` runner; its `Required` job has waited 1044s. Anyone
sizing a timeout from a run's wall-clock duration would set it wildly wrong.

When adding a job, size its timeout from the same place — the jobs endpoint,
per job, never extrapolated from a run count.
- This is a public repository. Its own gate and public callers use
  GitHub-hosted runners; private callers retain reusable-workflow compatibility
  and may use manifest-routed self-hosted runners only where policy permits.
- New reusable workflows follow both estate-wide conventions added by CI-5
  phase 2: the workflow's last job is named exactly `Required` and `needs:`
  everything else (see above), and `runs-on:` is `${{ fromJSON(inputs.runner) }}`
  fed by a JSON-encoded `runner` input defaulting to `'"ubuntu-latest"'` (see
  "Runner routing" above).
- Estate conventions live in the `ci-workflow-author` skill
  (agent-infrastructure repo); consult it before adding workflows here.
