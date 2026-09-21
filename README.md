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
> **Callers must pin the full 40-character commit SHA that `v2` points to,
> with a `# v2` comment (new work; `@v1` is frozen, see
> [v1 is frozen; adopt v2 when touched](#v1-is-frozen-adopt-v2-when-touched))
> — never a bare `@v2` tag and never `@main` — and a
> public (or possibly-future-public) caller must never pass a self-hosted
> runner label.** That warning is still correct, but it rests on the *caller*,
> not on this repo: `runner` is a free-form caller-supplied value, a repo can
> go public later, and a fork PR on a public caller can run
> attacker-controlled code on estate infrastructure. A reusable job whose
> runner input is left empty resolves it per run from the caller's own
> visibility: a private caller gets the fleet manifest's `linux-ci`
> organization-group route, a public (or visibility-unknown) caller gets
> GitHub-hosted `ubuntu-latest` — see [Default route](#default-route-empty-runner).
> `apple.yml`'s Mac route is the one input with no default, deliberately.

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
| `cursor-review.yml` | **Default-off PR reviewer, not a CI gate.** Reviews a pull request with one Cursor Cloud agent (`composer-2.5`, `fast: false`; explicit `review-deep` uses Grok 4.6 xhigh) that has the caller checked out at the PR head plus read-only context repos (estate manual, coding standards, decisions), then posts a REAL pull-request review on the reviewed head — APPROVE / COMMENT / REQUEST_CHANGES with inline comments — using the job's own `GITHUB_TOKEN`. Skips (draft, fork, `no-ai-review`, no secret) exit SUCCESS; a reviewer error fails the job. A REQUEST_CHANGES review blocks merge under the repo's pull-request rule. See [Cursor review](#cursor-review) (agent-infrastructure#1564) |
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
| `code-review.yml` | retired 2026-09-19 — source retained, no live pool adopters | n/a — historical advisory callable; current review uses `cursor-review.yml` |
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
runner: '"ubuntu-latest"'                                              # plain string
runner: '["self-hosted","Linux","X64","proxmox","linux-ci"]'           # JSON array
runner: '{"group":"linux-ci","labels":["self-hosted","Linux","X64","proxmox","linux-ci"]}'  # JSON object
```

The object form matches `Config/github-runner-fleet.json`'s `runsOn` shape
exactly, so a private manifest-routed caller can paste that value verbatim.
The value must be **valid JSON** — a bare string still needs its own quotes,
which is why the explicit hosted value is the four-character JSON string
`"ubuntu-latest"`, not the bare word.

### Default route (empty `runner`)

The `runner` inputs of `closing-syntax-check.yml`, `code-review.yml`,
`docs-governance.yml`, `node-library.yml`, `python-data.yml` and
`reusable-node-ci.yml`, and `apple.yml`'s `lint-runner`, default to the empty
string (row 18 Q3, Logan 2026-09-18, "Flip the default (Recommended)"). Every
`runs-on:` resolves the **effective route** as:

```
inputs.runner || github.event.repository.private == true && '{"group":"linux-ci","labels":["self-hosted","Linux","X64","proxmox","linux-ci"]}' || '"ubuntu-latest"'
```

| Caller | Before | After |
|---|---|---|
| private, passes nothing | GitHub-hosted `ubuntu-latest` (policy drift, company-hq `CI-RUNNER-POLICY.md` §1) | `linux-ci` organization group, group **and** labels (§4) |
| public, or an event with no `repository` payload, passes nothing | `ubuntu-latest` | `ubuntu-latest` (§3) |
| any caller, explicit value | that value | that value — unchanged, proven by `scripts/test_runner_default.py` |

The comparison is `== true`, so an unknown visibility falls to hosted, the safe
direction. Because visibility is read at run time, a caller that goes public
lands on hosted on its next run with no edit. Blacksmith overflow and
`CI_LIGHTWEIGHT_RUNNER` apply to the effective route exactly as they applied to
`inputs.runner` before; an effective `"ubuntu-latest"` is never sent to
Blacksmith. `reusable-browser-tests.yml`'s `contract` job and its `Required`
fallback use the same visibility gate with no caller input at all, so the
route contract is still validated on a runner this file chose.

The route literal is the fleet manifest's `linux-ci` class
(`fleet/Config/github-runner-fleet.json`, organization group `linux-ci`). It is
one string repeated at each site; `test_runner_default.py` fails if any copy
diverges, but nothing here re-reads the manifest, so a manifest label change
must be mirrored here.

**The `linux-ci` group is `selected`-visibility.** A private caller the
manifest does not list in that group gets a job that queues with no eligible
runner when it passes nothing. Such a repo must be added to the manifest group
(the policy fix) or pass `'"ubuntu-latest"'` explicitly, and a repo holding a
§2 hosted exception (for example `package-delivery`, exception 5) must pass
`'"ubuntu-latest"'` explicitly. See the versioning note on this change below.

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
(no `fromJSON` on the caller's value) — it predates this convention. Don't pass
a JSON-encoded value to it; it isn't decoded. Its empty default follows the same
visibility-gated route as above.

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

In the callables with an empty `runner` default, each `inputs.runner` above is
the parenthesised effective route from [Default route](#default-route-empty-runner),
so a private caller that passes nothing is Blacksmith-eligible (it is on the
`linux-ci` class this overflow serves) and a public one never is.

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

## Cursor review

**On-demand reviews (2026-09-21).** Add `review-now` whenever a review is
wanted: it uses Composer 2.5 standard and bypasses every spend gate. Daily
and per-PR round caps default to zero (unlimited). Automatic reviews retain
the class rule and small-diff floors. Their skip messages recommend
`review-now`; there is no need to select a more expensive model to get a review.

`review-deep` explicitly requests Grok 4.6 xhigh, with fast mode off. Both
request labels are cleared before launch, including failed launches, so the
next add is a fresh request. A later `review-now` stays on Composer even if
an old deep label remains. Draft, fork, opt-out and credential checks still
apply. Callers must update their immutable pin to adopt this behavior.

`cursor-review.yml` is the estate's pull-request reviewer (design and decisions:
[agent-infrastructure#1564](https://github.com/narduk-enterprises/agent-infrastructure/issues/1564)).
It is default-off and never part of `ci / Required`. Every enrolled repository
calls it from its own workflow file (`.github/workflows/cursor-review.yml`, the
shape below) rather than from inside `ci.yml`: the trigger set includes
`ready_for_review`, which most `ci.yml` files do not carry, and a `ci.yml`
change in some repos re-runs the full gate. The caller grants exactly
`contents: read` + `pull-requests: write`. Its workflow-level concurrency group
MUST NOT reuse the callable's job group name (`cursor-review-<repo>-<pr>`); the
same name deadlocks the job at scheduling.

**Triggers and the class rule (reviewer untangle, 2026-09-19).** The trigger
set deliberately omits `synchronize`: on 2026-09-19 push-driven re-reviews took
the estate to 165 runs over 80 heads and exhausted the Cursor Models pool for
seven and a half hours. A lane that wants the new head reviewed adds the
`review-now` label; the callable clears it again so the next add is a fresh
event. `review-deep` is also consumed; unrelated label additions skip. One ordering matters: the job
refuses to wake at all while `no-ai-review` is on, and removing that label is
an `unlabeled` event nothing listens for — so **clear the opt-out first, then
add `review-now`**, not the other way round. The callable's job `if:` is an
allow-list of `opened` / `reopened` / `ready_for_review` / a re-request label,
so a caller that has not yet dropped `synchronize` launches nothing here.

**A pin-only bump is not safe.** A caller that keeps `synchronize` and a single
`cancel-in-progress` concurrency group starts a run on the first push, cancels
the opened review's waiter *before* any job condition is evaluated, and is then
skipped by the job `if:` — so `cancel_previous` never runs, the Cursor agent
keeps burning the pool, and the pull request is left unreviewed. Drop
`synchronize` in the same commit as the pin, or put every non-review action in
the `other` concurrency bucket first. The caller group below does the latter:
it mirrors the job `if:` exactly, so `synchronize`, `edited` and an ignored
label all share one bucket that no live review is ever in. `review-p0` and `review-p1` also
wake the reviewer, and because waking it cancels any in-flight waiter they beat
the inferred P2 signals too. Inside that, Logan's answer of
2026-09-19 governs volume, in his words: *"No numeric cap, only the P0/P1/P2
class rule"*. P0 (`.github/workflows/**`, `.github/actions/**`, `docs/agents/**`, a policy
basename — `AGENTS.md`, `CLAUDE.md`, `CODEX.md`, `SKILL.md`, `DECISIONS.md`,
matched case-insensitively — or the `review-p0` label) is always reviewed; P1 is
ordinary code, reviewed once per open/reopen/ready; P2 — an automation author
(`dependabot[bot]`, `github-actions[bot]`, `renovate[bot]`), a
`changeset-release/*` head, a metadata-only diff, or the `review-p2` label —
never launches an agent, and `review-now` overrides every P2 signal. P0 is
decided first, so a `review-p2` label or an automation author never lowers a
workflow change out of review, and metadata means prose only: `CODEOWNERS`,
`.gitignore` and `.gitattributes` are code, because one can drop required
reviewers and another can stop ignoring secret material. The title is never a
signal either — it is author-controlled, and `chore:` on a code change would be
a free skip of the merge-gating reviewer. An **unknown** file list (a files-API
failure, or a diff past the page bound) is classified reviewable, never P2. There is no launch counter here on purpose; the
ledger is `gh run list --repo narduk-enterprises/<repo> --workflow cursor-review.yml`.

Provider failure is reported as provider failure. In particular,
`usage_limit_exceeded` does not fall back to the retired Proxmox pool and does
not prove that a usage reset, spending change, or other account mutation
succeeded; the workflow records the review as unavailable until a later run.

```yaml
name: Cursor review

# No `synchronize`: a push does not re-review. A lane adds `review-now` to ask
# for the new head, and the callable clears the label again.
on:
  pull_request:
    types: [opened, reopened, ready_for_review, labeled]

# A label addition that is NOT a review re-request must never cancel a live
# review: a run-level cancel happens before any job condition is evaluated, so
# the discrimination has to be in the GROUP NAME, not only in the callable.
concurrency:
  group: cursor-review-caller-${{ github.repository }}-${{ github.event.pull_request.number || github.ref }}-${{ (github.event.pull_request.draft == false && !contains(github.event.pull_request.labels.*.name, 'no-ai-review') && (github.event.action == 'opened' || github.event.action == 'reopened' || github.event.action == 'ready_for_review' || (github.event.action == 'labeled' && contains(fromJSON('["review-now","review-p0","review-p1","review-deep"]'), github.event.label.name)))) && 'review' || 'other' }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  cursor-review:
    uses: narduk-enterprises/workflows/.github/workflows/cursor-review.yml@<full-sha> # workflows#<pr>; the moving v2 tag predates this callable, keep the SHA
    permissions:
      contents: read
      pull-requests: write
    with:
      enabled: true
      # runner: '"ubuntu-latest"'   # a PUBLIC caller pins GitHub-hosted explicitly
    secrets:
      CURSOR_CLOUD_AGENTS_API_KEY: ${{ secrets.CURSOR_CLOUD_AGENTS_API_KEY }}
```

What happens per pull request head:

1. The job launches one Cursor Cloud agent with the caller repository at the PR
   head branch and the `context-repos` (default `narduk-enterprises/agent-infrastructure`
   and `narduk-enterprises/company-hq`) attached read-only. The brief lives in
   `scripts/cursor_review_brief.md`; the agent reads the target repo's own
   `AGENTS.md`, the estate coding standards, may run the repo's cheap checks, and
   answers with one fenced JSON block (`verdict`, `summary`, `findings[]`).
2. The job polls the run (30 s) up to `wait-minutes` (default 30), reads the run's
   `result`, and posts a formal review on the reviewed head. Any `blocking`
   finding requests changes. Each finding whose `path:line` is inside the PR
   diff becomes an inline comment; the rest are listed in the review body.
3. A push does NOT re-review: `synchronize` is not in the trigger set. A lane
   that wants the new head reviewed adds `review-now`, which cancels the
   in-flight waiter and whatever agent the marker comment still points at —
   same head or not, because `review-now` is itself the same-head re-request —
   reviews the new head, and is cleared again so the next add is a fresh event. A non-blocking review dismisses the bot's
   own stale REQUEST_CHANGES itself, on the new head, with the reason recorded.
   Enable `required_review_thread_resolution` on the repo's ruleset to make
   lanes answer every thread. Do **not** enable `dismiss_stale_reviews_on_push`
   alongside this trigger set: with `synchronize` gone, a push would clear a
   REQUEST_CHANGES and nothing would launch to replace it, so
   `scripts/verify-pr-gate.py` would print green on a head no reviewer ever
   saw. A repository that keeps `dismiss_stale_reviews_on_push` on must treat
   `review-now` after every push as mandatory rather than optional. **With
   `dismiss_stale_reviews_on_push` off, the duty is the same**: an APPROVE or
   COMMENT on head N still satisfies GitHub's `reviewDecision` on head N+1, and
   `verify-pr-gate.py` reads only that decision — it does not bind the review to
   `headRefOid`. So under company-hq D-AGENT-REVIEW-2 a lane that pushes after a
   review adds `review-now`, whichever way the ruleset is set.

The only secret is `CURSOR_CLOUD_AGENTS_API_KEY`, a GitHub Actions repository
secret delivered from nvault at a workstation (company-hq D-CLOUD-SECRETS-1). No
GitHub credential enters the Cursor VM. Public callers (`narduk-libs`, this repo)
wait on `ubuntu-latest`; private callers on the manifest's `linux-ci` route via
the `runner` default, a near-idle poll.

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
    uses: narduk-enterprises/workflows/.github/workflows/<workflow>.yml@<sha-of-v2> # v2
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
    uses: narduk-enterprises/workflows/.github/workflows/nuxt-cloudflare.yml@<sha-of-v2> # v2
    with:
      run-e2e: ${{ needs.changes.outputs.code == 'true' }}
      wrangler-dry-run: ${{ needs.changes.outputs.code == 'true' }}
```

Which gates each callable exposes this way:

| Callable | Caller-gatable | Always runs |
|---|---|---|
| `nuxt-cloudflare.yml` | `run-e2e` (`e2e`, `e2e-plan`, `e2e-report`), `wrangler-dry-run`, `run-tests` | `build`, `checks` |
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
    uses: narduk-enterprises/workflows/.github/workflows/apple.yml@<sha-of-v2> # v2
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
  `linux-ci` runners provide the pinned Swift SourceKit runtime layer. Empty
  by default: a private caller gets the `linux-ci` organization-group route
  and a public one `"ubuntu-latest"` (see [Default route](#default-route-empty-runner)).
  Passing the route explicitly still works and still wins:

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
    uses: narduk-enterprises/workflows/.github/workflows/python-data.yml@<sha-of-v2> # v2
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
    uses: narduk-enterprises/workflows/.github/workflows/node-library.yml@<sha-of-v2> # v2
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
    uses: narduk-enterprises/workflows/.github/workflows/node-library.yml@<sha-of-v2> # v2
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
installs; public-only installs need no credential. A caller whose committed
project `.npmrc` routes `@narduk-enterprises` to the anonymous
`https://npm.nard.uk` mirror (company-hq `D-PKG-6`) counts as public for that
scope and can drop the secret, unless it also depends on `@narduk-geo` (not
mirrored) or its lockfile still names `npm.pkg.github.com` (workflows#106).
The web-foundation check (`foundation-check: true`) follows the same route
(workflows#109): a mirror-routed caller runs it with no credential at all,
and the pinned dlx download fetches from `https://npm.nard.uk`. That needs
narduk-app-tools 0.10.0 or later (the `foundation-check-tool-version` default),
because older releases hard-code GitHub Packages for their live registry
lookup. A caller that has adopted the tool as a dependency must depend on
0.10.0 or later too.
Without a caller bootstrap,
the callable writes a temporary user config containing a literal variable
reference, then removes it after the install.

A public monorepo with only workspace packages under an estate-looking scope
can opt out of that automatic name-based detection without forwarding a token:

```yaml
jobs:
  ci:
    uses: narduk-enterprises/workflows/.github/workflows/node-library.yml@<sha-of-v2> # v2
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
    uses: narduk-enterprises/workflows/.github/workflows/nuxt-cloudflare.yml@<sha-of-v2> # v2
    # A called workflow's jobs may only request permissions the caller granted;
    # asking for one it did not kills the whole run at startup (workflows#59).
    # `pull-requests: write` is required by the `preview` lane's sticky comment
    # -- see "Preview checks" below before pinning a ref that carries it.
    permissions:
      contents: read
      packages: read
      pull-requests: write
      actions: read # E2E proof lookup (required by the new revision)
    with:
      node-version: "24"
      package-manager: npm # hydrogen's current package manager; pnpm is the default
      # Optional: delegate the complete install to a caller-owned wrapper.
      # The callable passes the distinct service-token secret only as
      # NVAULT_TOKEN and skips its legacy direct package-registry path.
      install-script: ci:install
      typecheck-worker-script: typecheck
      typecheck-web-script: web:typecheck
      run-e2e: true
      wrangler-dry-run: true
    secrets:
      # Foundation checks and direct registry installs use the canonical PAT.
      NARDUK_PLATFORM_GH_PACKAGES_READ: ${{ secrets.NARDUK_PLATFORM_GH_PACKAGES_READ }}
      # Caller-owned install scripts receive the service token separately.
      NVAULT_TOKEN: ${{ secrets.NVAULT_TOKEN }}
```

`install-script` is for repositories whose install wrapper exchanges an nVault
service token for the package credential, materializes any temporary registry
configuration itself, runs the package manager, and removes the configuration on exit.
The value must be one package.json script name using letters, digits, `:`, `_`,
or `-`; the callable rejects missing or unsafe names. Existing callers that
leave it empty keep the legacy install path unchanged.

`NARDUK_PLATFORM_GH_PACKAGES_READ` always carries the org package-read PAT;
foundation checks use it by default. `NVAULT_TOKEN` carries the caller's
service token when an explicitly selected `install-script` needs nVault; that
installer resolves `GH_PACKAGES_READ` from nVault before starting npm/pnpm.
Generic caller-owned install scripts may omit it and validate their own
credentials. When `NVAULT_TOKEN` is empty (Dependabot runs see only
Dependabot-store secrets, and `NVAULT_TOKEN` is Actions-only) and
`foundation-check-auth` is not `nvault`, the caller script instead receives
the org package-read secret as `GH_PACKAGES_READ`, so it can install without
an nVault exchange. It never receives both (workflows#98). Raw PAT consumers omit `install-script` and pass only
`secrets.NARDUK_PLATFORM_GH_PACKAGES_READ`.

`foundation-check-auth: nvault` remains a compatibility mode for callers that
previously mapped their service token into the package-read secret. New callers
use the default `package-token` mode and map both secrets as shown above.

Local workstations use `gh-packages-run`, and Workers Builds uses the protected
build secret `GH_PACKAGES_READ`. Neither uses the Actions input name as a vault
key. [The credential route](https://github.com/narduk-enterprises/agent-infrastructure/blob/main/docs/agents/credentials.md)
provides the exact nVault selector and value-free diagnostics.

Deploying with real Cloudflare credentials on push-to-main is **not** this
workflow's job — that stays a separate `nuxt-cloudflare-deploy.yml` sibling
(deferred, not built in this pass), matching hydrogen's existing two-job
`ci` / `deploy` split rather than folding deploy secrets into the CI gate.

#### `node-version-file`: single-sourcing the Node version

| Input | Type | Default | Purpose |
|---|---|---|---|
| `node-version` | string | `"24"` | Node.js version passed straight to `actions/setup-node` |
| `node-version-file` | string | `""` | Optional path (relative to `working-directory`) to a file declaring the Node version, e.g. `.node-version` or `.nvmrc` |

Empty (the default) preserves prior behaviour exactly: every `setup-node`
step in this callable uses `node-version`. Setting `node-version-file`
lets a caller single-source its Node version from a file it already
maintains — `.node-version`, `.nvmrc`, `package.json`'s `engines` via a
generated file, etc. — instead of ALSO pinning it as this input's own value,
which is how a caller's Node pin and its CI pin drift apart.

`actions/setup-node` rejects `node-version` and `node-version-file` together,
so every `setup-node` step here resolves them as two mutually exclusive
expressions rather than passing both: `node-version-file` is passed through
unchanged, and `node-version` resolves to an empty string whenever
`node-version-file` is set (an empty string is `setup-node`'s own "not
provided" sentinel for either input). A caller that sets both gets
`node-version-file`; `node-version` is silently ignored in that case, exactly
as if the caller had left it unset.

#### `caller-lint`: hygiene gate over the caller's OWN workflows

Every `nuxt-cloudflare.yml` adopter now gets a `caller-lint` job as part of
`Required` (Logan, 2026-09-17 askme round, "Caller lint + job timeouts in
workflows (Recommended)"; company-hq#745). It checks out the CALLING
repository (not this one), runs pinned `actionlint` over the caller's own
`.github/workflows/*.yml`, and runs a small inline Python audit that fails
the job when a caller workflow:

- has no workflow-level `concurrency:` block (skipped for a file whose only
  trigger is `workflow_call` — a callable must NOT declare one; see
  "Concurrency is the caller's job" above);
- has a job that does not `uses:` a reusable workflow and has no
  `timeout-minutes` (a job that DOES `uses:` one is reported as an
  informational `::notice::` naming the called workflow instead — that job
  cannot declare `timeout-minutes` at all, because the called workflow's own
  jobs own it, and flagging it as a finding was a false positive this repo
  used to ship in `agent-infrastructure`'s own `audit_workflows.py`);
- has any `uses:` step or job not pinned to a full 40-character commit SHA;
- is missing `permissions:` at the workflow level or on any job.

This is a caller-side hygiene check, distinct from what `actionlint` alone
proves (schema/expression validity) and distinct from what `lint_callables.py`
proves about THIS repo's own callables — `caller-lint` proves the same class
of thing about the repository that adopted one.

#### Dependency audit (`dependency-audit`, `audit-ignore`)

| Input | Type | Default | Purpose |
|---|---|---|---|
| `dependency-audit` | boolean | `true` | Fail the build on a high/critical advisory **that has a published fix** |
| `audit-ignore` | string | `""` | Comma-separated `GHSA-xxxx-xxxx-xxxx=reason` suppressions; the reason is required |

The estate security bar (company-hq#745; Logan, askme round 2026-09-17, "Fail on
fixable high/critical (Recommended)"; company-hq D-ORG-1 (g), 2026-09-02) is
*alerts on, and no **fixable** high or critical advisory in the tree*. That is
deliberately not what `pnpm audit --audit-level=high` reports on its own: its
exit status goes non-zero for **any** high/critical finding, including ones
upstream has published no patch for. A gate that cannot tell "you have not
upgraded" from "there is nothing to upgrade to" is a gate whose only
sustainable reaction is `|| true`, and once that lands the fixable advisories
stop being caught too.

So the audit command's exit status is discarded on purpose and the JSON report
is what decides:

| Finding | Result |
|---|---|
| high/critical **with** a published fix | `::error::` — the `build` job fails |
| high/critical with **no** published fix | `::warning::` — the build passes |
| moderate / low / info | counted in the job summary, never blocking |
| report missing, empty, unparseable, or in an unrecognised shape | `::error::` — hard failure |

That last row is the same fail-closed rule `require-scripts` exists for: "the
audit did not run" must never be indistinguishable from "the audit found
nothing".

Both report shapes are parsed, and the `package-manager` input selects the
*command*, never the parser — npm changed this format once already, and a
parser keyed on the input would silently read zero advisories the next time it
changes:

- **pnpm / npm 6** — the `advisories` map. Fixable means `patched_versions` is a
  real range rather than the `"<0.0.0"` no-patch sentinel.
- **npm 7+** (`auditReportVersion: 2`) — the `vulnerabilities` map. Fixable
  means `fixAvailable` is `true` or a `{name, version, isSemVerMajor}` object;
  a fix that needs a major bump still counts as a fix.

Estate contract pins are not advisories and do not count here — only what the
package manager's own audit reports does.

##### `audit-ignore`: a suppression must carry its reason

```yaml
audit-ignore: >-
  GHSA-aaaa-bbbb-cccc=no upstream release yet, tracked in company-hq#812, review 2026-12-01,
  GHSA-dddd-eeee-ffff=unreachable code path behind a disabled flag, review 2026-11-01
```

Each entry is `<id>=<reason>`, entries separated by commas. **The reason is
required**: an entry with no `=reason` fails the gate rather than silently
muting an advisory, which mirrors the written-reason convention a Dependabot
`ignore:` block carries. Every suppression is echoed as a `::warning::` with its
reason attached, so a muted advisory cannot become invisible tribal knowledge,
and an entry that matches nothing in the current report is reported as a stale
suppression so it gets removed instead of accumulating. Advisories the report
carries no GHSA id for are matched by `NPM-<numeric-id>`.

##### Why a step in `build` and not its own job

The tree it audits is the one `build` just installed. A standalone lightweight
job on the `caller-lint` runner class would cost a second checkout, a second
`setup-node` and a second full install — 60–120s and a second runner slot on
this repo's adopters — to re-derive state that already exists in `build`, for
the ~5–10s the audit command itself takes. It never touches the browser pool.
`Required` covers it through `build`, which it already demands success from;
there is no separate result to aggregate.

##### Turning it off

`dependency-audit: false` is a bounded remediation, not a setting — the same
status `require-scripts: false` has. Unlike `run-tests` and `foundation-check`,
this input defaults to **`true`**: those two run a caller-specific script that
may not exist, while this one reads the lockfile every adopter already has, and
it is a security bar rather than an optional lane. It is still a new gate that
can turn an existing adopter red, so the `v1` tag must not move onto it until
the adopters have been checked — see [Versioning policy](#versioning-policy).

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

Everything in `build` delays the prebuilt E2E application, and so every E2E
shard, the preview and the deploy dry run. The typecheck and unit-test lanes
therefore run in their own parallel `checks` job, and `e2e-plan` no longer
waits for `build`. An extra script that does **not** need build output belongs
in `extra-gate-scripts`, which also runs in parallel. On riverstatus, two
migration-proof scripts in `extra-scripts` held its E2E back by about 8 minutes.

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
    uses: narduk-enterprises/workflows/.github/workflows/nuxt-cloudflare.yml@<sha-of-v2> # v2
    with:
      runner: '{"group":"linux-ci","labels":["self-hosted","Linux","X64","proxmox","linux-ci"]}'
      working-directory: apps/web
      run-e2e: false
      e2e-build-artifact-path: .output

  browser:
    needs: ci
    uses: narduk-enterprises/workflows/.github/workflows/reusable-browser-tests.yml@<sha-of-v2> # v2
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

#### Skipping irrelevant changes (`e2e-skip-paths`)

E2E now skips by default when **every** changed path is documentation or
repository metadata: root Markdown, Markdown under `docs/`, app/package-root
READMEs, agent guidance files, licenses, issue/PR templates, or CODEOWNERS. Runtime
Markdown under `content/`, executable files under `docs/`, app code, CSS,
dependencies, configuration, workflows, tests and unknown paths still run.

The same rule applies to pull requests and forward pushes, including the merge
push. Manual, scheduled and merge-group runs always run E2E. A skipped plan
emits no shards; `Required` checks that E2E and its report actually skipped.

`e2e-skip-paths` replaces the default with repository-root-relative globs
(`*` does not cross directories; `**` does). An explicitly empty string disables
path skipping. Narrow or disable the list if your app renders those documents;
never add CSS or executable documentation to the list. `e2e-full-paths` takes
precedence over a matching skip pattern.

The plan reads GitHub's compare endpoint with `contents: read`, including both
names of renamed files. Empty or failed comparisons, new/deleted branch SHAs,
force-push divergence, unsupported filenames and the API's 300-file cap all
run E2E. See [GitHub's compare API](https://docs.github.com/en/rest/commits/commits#compare-two-commits).

#### Reusing full PR E2E after merge (`e2e-reuse-pr-results`)

Default-branch pushes first look for a successful PR run of the **same Git tree,
caller workflow path and callable inputs**. This handles squash and merge
commits whose SHA differs but whose tested contents are identical. Other CI
checks and production delivery still run normally.

`Required` publishes a seven-day proof artifact only after every required gate
passed and the actual E2E arguments and shard count matched the full suite.
A PR subset, docs-only skip, failed shard or fork cannot publish proof. Reuse
requires a completed successful `pull_request` run from the same repository
and workflow, and a proof from its current run attempt. The lookup checks at
most five matching artifacts and fails toward running E2E on absent, expired,
changed or unreadable evidence. Scheduled/manual runs always run; set
`e2e-reuse-pr-results: false` for tests with intentionally different PR/push
behavior or external state that must be rechecked after merge.

**Permission migration:** adopting this revision requires `actions: read` on
all Nuxt callable `ci` jobs, even if E2E is disabled. GitHub validates the
permission ceiling before evaluating job conditions. The proof lookup needs
only read access to Actions results; it gains no write permission. Add this
alongside the existing `contents: read`, `packages: read`, and
`pull-requests: write` grants before or with the SHA bump. Existing pinned
callers do not change. This is a breaking permission change: do not advance
`v2` over it; publish a new major only after an adopter canary is green.

```yaml
    permissions:
      contents: read
      packages: read
      pull-requests: write
      actions: read # read the prior PR's E2E proof
    with:
      run-e2e: true
      # Defaults: conservative path skipping and full-PR proof reuse.
```

#### A smaller suite on pull requests (`e2e-pr-shards`, `e2e-pr-args`)

(workflows#83) `e2e-skip-paths` above is all-or-nothing: it either runs the
whole matrix or none of it. These two inputs are the middle setting — a
**caller-defined** subset on pull requests, the full suite on the default
branch.

```yaml
      run-e2e: true
      e2e-shards: 3                  # push / default branch: unchanged
      e2e-args: ""
      e2e-pr-shards: 1               # pull request: one lane
      e2e-pr-args: "--project=smoke" # pull request: the caller's own project
```

Why this exists: the browser class now has three active 8 GiB on-prem primary
guests (343, 345 and 346), three 4 GiB `pve-hetzner` fallback guests (340–342),
and CT 344 is a configured dormant guest rather than an active slot. The old
three-effective-slots/seven-declared queue measurements are historical; heavy
jobs request `memory-8g`, while not every browser guest is 8 GiB. Capacity and
tiering belong to `narduk-enterprises/fleet`, not to this callable. See the
[canonical host inventory](https://github.com/narduk-enterprises/fleet/blob/main/docs/host-inventory.md) for names and placement.
The primary/fallback flags do not establish GitHub scheduling priority: normal
browser jobs can land on all six active guests; `memory-8g` matches the three
active on-prem guests.

**Fewer lanes is not by itself faster — it is fewer slots.** Measured on gonogo
with `e2e-pr-shards: 1` and no `e2e-pr-args`: the same suite ran 527 / 407 /
292 s in three lanes plus a 32 s report (a 559 s critical path), and 913 s in
one. Aggregate pool occupancy dropped 27% (1258 runner-seconds over four jobs
to 913 over one) and the run held **one** isolated slot instead of three, which
is the part that shortens every other repository's queue — but the pull
request's own wall clock got *longer*, because the tests were redistributed
rather than reduced. `e2e-pr-shards` alone is a courtesy to the pool. To make
your own pull request faster, pair it with an `e2e-pr-args` subset that runs
genuinely fewer tests.

The rules, all of which fail toward running **more**:

- **Unset is today's behaviour.** `e2e-pr-shards: 0` and `e2e-pr-args: ""` are
  the defaults and mean "no override". Every current adopter passes neither,
  so they see no change whatsoever.
- **Pull-request events only.** `push`, `schedule`, `workflow_dispatch` and
  anything unrecognised run `e2e-shards`/`e2e-args` even when an override is
  configured — the same confinement `e2e-skip-paths` has, and for the same
  reason: the default branch is the canonical validation, and neither input
  may weaken it.
- **`e2e-pr-args` replaces, it does not append.** A pull-request subset cannot
  silently inherit a conflicting `--project` from `e2e-args`.
- **Empty means inherit.** A caller that wants arguments on a push and *none*
  on a pull request cannot express that here; put a `github.event_name`
  expression in the caller's own `with:` block instead. A sentinel for
  "explicitly empty" would be a second, weaker way to say the same thing.
- **The subset is the caller's to define.** This workflow never guesses what
  "smoke" means — it passes your arguments to your `e2e-script`. Tag the
  subset in your own `playwright.config` (a project) or with `--grep`.

`Required` is unaffected as a gate: it still demands `E2E` succeed, and it
derives "was there more than one shard, so must `E2E report` have run?" from
the same single resolved value `E2E` sharded on, so a one-lane pull request
correctly expects `E2E report` to be `skipped` rather than absent.

**A subset is a smaller gate, not a weaker one.** Whatever a pull request
stops running, the default-branch push still runs — but it runs it *after* the
merge. Choose the subset so a failure it cannot catch is one you are willing
to find on `main`.

#### The full suite for risky paths (`e2e-full-paths`)

`e2e-full-paths` is the escape hatch from the pull-request subset: a
space-separated list of globs (the same syntax as `e2e-skip-paths`). When any
file a pull request changes matches one, that pull request runs the full
`e2e-shards`/`e2e-args` instead of `e2e-pr-shards`/`e2e-pr-args`.

```yaml
    with:
      e2e-pr-shards: 1
      e2e-pr-args: "--project=smoke"
      e2e-full-paths: "server/database/** drizzle/** playwright.config.ts"
```

The use is a fast pull-request smoke by default, with the whole suite reserved
for the paths whose breakage the smoke tier cannot see — schema, auth,
routing, the Playwright config itself. The rules:

- **Unset is today's behaviour.** An empty `e2e-full-paths` (the default)
  never forces the full suite.
- **It beats `e2e-skip-paths`.** A file matching both lists runs the full
  suite; it is never skipped.
- **Unknown means full, for an opted-in caller.** When the changed files
  cannot be listed (no base/head SHA, no `gh`, a compare API error, an empty
  list, or the 300-file compare cap), a caller that set `e2e-full-paths` gets
  the full suite. A caller that did not keeps its pull-request tier.
- **Full-tier selection is for pull requests.** Pushes use the full tier when
  neither path skipping nor equivalent PR proof applies.
- The `E2E plan` job summary lists which changed files forced the full run.

#### A non-blocking quarantine lane (`e2e-quarantine-args`)

A flaky test is taken out of the gate by tagging it (for example
`@quarantine`) and excluding that tag from the caller's gating Playwright
projects. It still needs somewhere to run, or it can never show it is fixed.
`e2e-quarantine-args` is that place:

```yaml
    with:
      e2e-quarantine-args: "--project=quarantine --retries=0"
```

When set, an extra `E2E (quarantine)` job runs `e2e-script` with exactly
these arguments, on every event E2E runs on:

- **It cannot fail the gate.** The job is `continue-on-error: true`,
  `Required` does not list it, and no job `needs:` it. A red quarantine run
  shows as a warning and a job-summary line. It never turns `ci / Required`
  or the caller's run red. `lint_callables.py` enforces this through its
  `NON_GATING_JOBS` exemption, which is the only job allowed outside R5.
- **It is the gate's setup.** It runs `E2E`'s own steps through a YAML alias:
  the same prebuilt artifact, runner route, toolchain checks and auth cleanup.
  Only the arguments differ. It is unsharded.
- **Its evidence is separate.** It uploads `playwright-quarantine`, which is
  outside `E2E report`'s `playwright-evidence-*` merge, so quarantined results
  never enter the gate's report.
- **It skips with E2E.** A docs-only PR skipped by `e2e-skip-paths` runs
  neither.

The job's history on the default branch is the "N consecutive green runs"
record a test needs to leave quarantine. Pass `--retries=0` so a retry can't
hide a flake. Empty (the default) adds no job.

#### Preview checks (`preview-checks`, `preview-url-source`)

| Input | Type | Default | Purpose |
|---|---|---|---|
| `preview-checks` | string | `og` | `none`, `og`, `e2e-subset`, or `og,e2e-subset` |
| `preview-url-source` | string | `pr-comment` | `pr-comment` or `url-template` |
| `preview-url-template` | string | `""` | URL template for `url-template`; `{branch}`, `{branch-alias}`, `{sha}` |
| `preview-timeout-minutes` | number | `20` | Whole-job budget; the bounded wait is this minus five minutes |
| `preview-working-directory` | string | `""` | Directory the preview checks run from; empty uses `working-directory` |

> **This is a BREAKING interface change.** The `preview` job requests
> `pull-requests: write`, and a called workflow asking for a permission its
> caller did not grant kills the caller's **entire** run at startup —
> `startup_failure`, zero jobs, no logs, no annotation (workflows#59, which took
> down every `@v1` adopter on 2026-09-04). **Every adopter must add
> `pull-requests: write` to its `ci:` job before the `v1` tag moves onto this
> commit**, and an adopter whose repository has no Workers Builds preview must
> also pass `preview-checks: none` in the same change:
>
> ```yaml
> jobs:
>   ci:
>     uses: narduk-enterprises/workflows/.github/workflows/nuxt-cloudflare.yml@<sha-of-v2> # v2
>     permissions:
>       contents: read
>       packages: read
>       pull-requests: write # sticky preview comment
> ```

##### What Cloudflare actually exposes, and what this workflow therefore reads

Workers Builds' GitHub App creates **check runs**, **commit statuses** and a
**pull request comment**, and "a preview URL will be provided for any builds
which perform `wrangler versions upload`"
([Cloudflare: Workers Builds GitHub integration](https://developers.cloudflare.com/workers/ci-cd/builds/git-integration/github-integration/)).
It does **not** create a GitHub Deployment — that is the *Pages* integration's
shape — so there is no `environment_url` to read, and this workflow does not
offer a source that pretends there is. Adding one would also have cost every
adopter a `deployments: read` grant to carry dead code.

That leaves two honest sources:

- **`pr-comment` (default)** — read the pull request's comments and take the
  first `*.workers.dev` URL posted by an author whose login contains
  `cloudflare`. A `workers.dev` link posted by anyone else is ignored.
- **`url-template`** — derive the URL locally, with no GitHub read at all.
  Cloudflare's aliased preview URLs are
  `<ALIAS>-<WORKER_NAME>.<SUBDOMAIN>.workers.dev`, so a typical template is
  `https://{branch-alias}-myworker.myaccount.workers.dev`. `{branch-alias}` is
  the head ref lowercased with every character outside `[a-z0-9]` replaced by
  `-`. That reproduces the transform this estate has **observed** (buoys'
  branch `codex/buoys-complete` →
  `codex-buoys-complete-buoys.narduk-enterprises.workers.dev`); Cloudflare does
  not publish the truncation rule it applies to long branch names, so a
  repository with long branch names should stay on `pr-comment`, which reads
  the URL Cloudflare actually minted rather than predicting it.

##### The URL is not the proof — `x-build-version` is

A preview link in a comment says a build was *attempted*. The wait is not
satisfied until the URL itself answers and its `x-build-version` header is a
prefix of the pull request's head SHA. That header is the estate's existing
live-proof convention (buoys `docs/workers-builds.md` records
`x-build-version: a84fa2163903` for commit `a84fa216390321…`), and it is
compared **by prefix** because Cloudflare emits a 12-character short SHA while
`git rev-parse --short` defaults to 7. A fixed-width comparison would be a gate
that never passes, and a gate that never passes is a gate somebody deletes.

Each round of the bounded wait is one comment read and one HTTP read — no sleep
loop that can outlive the job, and the wait is `preview-timeout-minutes` minus
five so the checks and the comment still have room after it.

##### Fail closed

A preview that never becomes ready inside the bound is a **FAILURE, not a
skip**, and so is one that answers 4xx/5xx, carries no `x-build-version`, or
serves a different commit. "The preview never showed up" is the single most
common way a Workers Builds connection silently breaks, and it is
indistinguishable from "this repository does not use previews" only if you
refuse to make the caller say which it is. That is what `preview-checks: none`
is for.

The lane runs on `pull_request` / `pull_request_target` events only. A push to
the default branch has no pull-request preview to check, and `Required` expects
the job `skipped` there — a `preview` job that *runs* on a push is a failure
too.

##### The checks

- **`og`** — `narduk-app og:check --live --base-url <preview>`, run from the
  caller's **own** installed `@narduk-enterprises/narduk-app-tools`. Unlike
  `foundation-check`, there is no pinned `dlx` fallback here: `preview-checks:
  og` is a caller asserting it has the tool, and a second resolution path would
  mean a second credential path and a second version to keep in step.
- **`e2e-subset`** — runs `e2e-script` with `e2e-pr-args` (the same
  pull-request argument set `e2e-pr-shards`/`e2e-pr-args` introduced in
  workflows#83) against the preview with `PLAYWRIGHT_BASE_URL` set. An empty
  `e2e-pr-args` is a hard failure rather than a silent full-suite run against a
  shared preview.

  **The caller's Playwright config must honour `PLAYWRIGHT_BASE_URL` and must
  not start its own `webServer`**, or the suite will test localhost and report
  green. buoys cannot use this yet for exactly that reason — its
  `playwright.config.ts` has only dev-server and prebuilt-Worker modes and "no
  fixture accepts an override base URL" (`docs/e2e-testing.md`).

##### Runner class

Lightweight (the same route as `caller-lint` and `E2E plan`) **unless**
`e2e-subset` is selected, which needs a browser guest and therefore the
`e2e-runner` route. On that route the lane runs the **same YAML nodes** as the
`e2e` job's isolated-route guard, image-equality assertion and browser
installer — shared by anchor, not copied, because an image-equality gate that
exists twice is an image-equality gate that drifts. `lint_callables.py` R11 now
holds *every* job routed to `e2e-runner` to that preflight, not just `e2e`.

Note that with `e2e-subset` selected the bounded wait holds a
`playwright-isolated` slot (three effective slots across roughly ten repos —
see "A smaller suite on pull requests"). That is why `preview-timeout-minutes`
is a caller input and why `og` is the default.

##### One sticky comment

The lane posts a single comment carrying the URL, the build version, the head
SHA and each check's result, marked with `<!-- narduk-ci:preview -->`. Later
runs **find it by marker and edit it**; they never append. The same table also
goes to the job summary, so the URL survives even when the comment API call
does not — and a failure to post warns rather than turning an otherwise passing
lane red.

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
    uses: narduk-enterprises/workflows/.github/workflows/docs-governance.yml@<sha-of-v2> # v2
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
    uses: narduk-enterprises/workflows/.github/workflows/reusable-node-ci.yml@<sha-of-v2> # v2
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

## Historical advisory code review (retired 2026-09-19)

`code-review.yml` is retained as a compatibility and provenance surface, not a
current route. It is worth understanding why it was built: **it is not a CI
gate.** Every other
callable here exists to produce `ci / Required`. This one produces nothing a
branch ruleset can require, has no `Required` job, and cannot fail your build.
It asked the estate's ephemeral agent pool for one read-only review of a pull
request head. The pool is retired, its allowlist is empty, and no current
workflow should adopt this callable; current adoption uses `cursor-review.yml`.

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
| the retired pool is unavailable | historical behavior: nothing queues, nothing retries |

### Consuming it

```yaml
  code-review:
    uses: narduk-enterprises/workflows/.github/workflows/code-review.yml@<sha-of-v2> # v2
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

- **Callers pin full commit SHAs, never `@main`.** `@main` is how the last
  outage happened; it is not a supported reference. The caller-lint job and
  narduk-app-tools foundation item 5.1 both reject a bare `@v2` tag, so a
  caller pins the SHA the tag points to and names the tag in a comment:
  `uses: …/<workflow>.yml@<40-char sha> # v2`. Existing `@v1` pins still
  pass while `v1` is frozen.
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
- **`v1` is frozen; adopt `v2` when touched.** See the section below.
- **A fan-out canary precedes an advance**: before moving the tag, trigger or
  find at least one adopter run on the new commit and confirm it resolves and
  succeeds, per Logan, 2026-09-04: "Fan-out canary before the tag advances
  (Recommended)" (Refs company-hq#536).
- **The empty-`runner` default flip (row 18 Q3) must not reach `v1` until its
  unlisted private callers are fixed.** It changes no input name, job, check
  context or permission, so it stays within `v1`, but it moves every private
  caller that passes nothing onto the `selected`-visibility `linux-ci` group.
  At the flip, the `@v1` callers passing nothing were `coding-standards`
  (`docs-governance.yml`) and `x-event-recap` (`node-library.yml`) — both
  outside that group — and `package-delivery` and `software-delivery` on
  `nuxt-cloudflare.yml` (whose flip ships separately). Before `v1` advances
  over this change: add each such repo to the fleet manifest's `linux-ci`
  group, or have it pass `'"ubuntu-latest"'` explicitly (mandatory for a §2
  hosted exception such as `package-delivery`); then run the fan-out canary.
  SHA-pinned callers pick the change up only when they bump their pin.
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
- `nuxt-cloudflare.yml`'s pull-request subset inputs (`e2e-pr-shards`,
  `e2e-pr-args`, workflows#83) are within-major for the same reason: both are
  optional, both default to an UNSET sentinel (`0` / `""`), and with them
  unset every event resolves the shard count and argument list exactly as
  before. No job, check name, or `Required` expectation changed — the effective
  shard count simply moved from `inputs.e2e-shards` to an `E2E plan` output
  that equals it whenever no override is supplied.
  `e2e-full-paths` is within-major on the same rule: optional, default `""`,
  and with it unset every event plans exactly as before.
  `e2e-quarantine-args` is too: optional, default `""`, and when unset its
  job is skipped. The job it adds is never `needs:`-ed and never gates, so no
  check context `Required` reads changes.
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
- `foundation-check-auth` defaults to `package-token`. `nvault` remains only
  for callers whose legacy `NARDUK_PLATFORM_GH_PACKAGES_READ` mapping carries
  an nVault service token; new callers use the canonical package PAT there and
  map the service token separately as `NVAULT_TOKEN` for `install-script`.
  The callable resolves the existing package-read grant for that legacy one
  step and supplies `NODE_AUTH_TOKEN` to both the installed checker and the
  optional pinned download. The checker needs this credential for its live N-1
  registry lookup even when dependencies are already installed, unless the
  caller's project `.npmrc` routes `@narduk-enterprises` to `npm.nard.uk`
  (workflows#109): then no credential is resolved or exported. Neither the
  service token nor the resolved package token is exported to later steps;
  temporary npm configuration contains only an environment-variable reference.
  Missing or rejected credentials leave a blocking UNKNOWN artifact rather
  than falling back to `github.token`.
- The D-CI-CAP-1 (c) Blacksmith-overflow change (see "Blacksmith overflow"
  above) is within-major on the same rule: no new input, no new job, no new
  job-level `permissions:`, and the default (`vars.BLACKSMITH_RUNNERS_ENABLED`
  undeclared) reproduces every existing adopter's behavior byte-for-byte.
  `v1` moves again rather than a `v2` being cut, behind the usual fan-out
  canary.

### v1 is frozen; adopt v2 when touched

Logan, 2026-09-18 (askme, 13:41 CT): "Moving v2 tag; repos move when touched
(Recommended)".

- **`v1` stays at f8e3cc6 and does not move again.** Everything after it is
  on the `v2` line. `v2.0.0` (a29cd16, the pull-request preview gate) is a
  breaking change for `nuxt-cloudflare.yml`: its `preview` job requests
  `pull-requests: write`, and a caller that does not grant it hits
  `startup_failure` on its **whole** run (workflows#59). Moving `v1` over it
  would have broken every `@v1` nuxt-cloudflare caller at once. On
  2026-09-18 none of the seven (hydrogen, marketing-web, my-farm,
  narduk-nvr, package-delivery, software-delivery, vtraceroute) granted it.
- **`v2` is the moving major tag**, advanced by hand behind the fan-out
  canary, the same way `v1` was.
- **A repo moves to `v2` in the next PR that works on it**, not in a sweep.
  Make these changes in that same PR:
  1. every `uses: …/<workflow>.yml@v1` becomes the full SHA `v2` points to,
     with a `# v2` comment (`git ls-remote https://github.com/narduk-enterprises/workflows refs/tags/v2`;
     `3cc8c736d07aa821daeb417e7f6682ecd3935aa8` on 2026-09-18). A bare `@v2` fails caller-lint
     ("is not pinned to a full 40-character commit SHA") and foundation item 5.1;
  2. a `nuxt-cloudflare.yml` caller adds `pull-requests: write` to its `ci:`
     job's `permissions:`, and passes `preview-checks: none` if the repo has
     no Workers Builds preview (see [the preview gate](#nuxt-cloudflareyml));
  3. a **private** caller that passes no `runner` now lands on the
     `linux-ci` organization group ([Default route](#default-route-empty-runner)).
     Confirm the repo is in that group. If it is not, it queues forever
     rather than failing. Otherwise pass `runner:` explicitly (for example
     `'"ubuntu-latest"'` for a named CI-RUNNER-POLICY §2 hosted exception);
  4. `nuxt-cloudflare.yml`'s `dependency-audit` defaults to `true` on `v2`
     and fails on fixable high/critical advisories. Fix them, or pass
     `dependency-audit: false` with its written reason.

  The PR's own CI run on the `v2` SHA is the proof it moved cleanly.

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
  everything else (see above), and `runs-on:` decodes the effective route
  (`inputs.runner`, else the visibility-gated default) from a JSON-encoded
  `runner` input defaulting to `''` (see "Default route" above; add the new
  callable to `scripts/test_runner_default.py`).
- Estate conventions live in the `ci-workflow-author` skill
  (agent-infrastructure repo); consult it before adding workflows here.
