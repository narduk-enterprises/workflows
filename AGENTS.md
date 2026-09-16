# workflows — agent entry pointer

workflows holds the shared reusable GitHub Actions workflows that the rest of
the `narduk-enterprises` estate calls instead of blob-copying YAML — fix CI in
one place, not in a hundred repos. Nine callables live in
[`.github/workflows/`](./.github/workflows/), all `on: workflow_call` only
(`apple.yml`, `closing-syntax-check.yml`, `code-review.yml`,
`docs-governance.yml`, `node-library.yml`, `nuxt-cloudflare.yml`,
`python-data.yml`, `reusable-browser-tests.yml`, `reusable-node-ci.yml`);
`ci.yml` is the one non-callable, and it is this repo's own gate. The repo is
public; its own CI and every public caller must use GitHub-hosted runners.
Private callers retain their existing reusable-workflow access and may use
manifest-routed self-hosted capacity where their repository policy permits it.

The estate-wide operating manual for agents lives in
[narduk-enterprises/agent-infrastructure](https://github.com/narduk-enterprises/agent-infrastructure)
(its root `AGENTS.md`). On the Macs it is loaded automatically; in a cloud
container nothing loads it for you — read it first, then return here.

## Fast orientation

| Question | Answer |
|---|---|
| What is this repo? | The estate's shared reusable Actions workflows, consumed via `workflow_call` |
| Estate operating manual | [narduk-enterprises/agent-infrastructure](https://github.com/narduk-enterprises/agent-infrastructure) → `AGENTS.md` |
| Run the checks | `actionlint -no-color -oneline .github/workflows/*.yml`, `python3 scripts/lint_callables.py`, then each `python3 scripts/test_*.py` |
| Catalog, consumption, versioning | [`README.md`](./README.md) — what each callable does, how to call it, how tags move |
| Structural rules (R1–R11) | [`scripts/lint_callables.py`](./scripts/lint_callables.py) — the rule table at the top |
| Cloud-session setup | [narduk-enterprises/agent-infrastructure](https://github.com/narduk-enterprises/agent-infrastructure) → `docs/cloud-sessions.md` |

## Gates

[`.github/workflows/ci.yml`](./.github/workflows/ci.yml) is the only thing here
that looks at a callable before its adopters do — a defect in this repo does not
fail one repo, it fails every repo pinned to the tag carrying it. It runs on the
self-hosted `linux-ci` runner group and layers two kinds of check:

- **`actionlint`** — is the workflow *valid*? Schema, expressions, and
  shellcheck over every `run:` block. It needs `shellcheck` on PATH; without it
  actionlint skips run-block linting silently and still exits 0, so the workflow
  asserts its presence.
- **`scripts/lint_callables.py` plus twelve `scripts/test_*.py`** — is the
  callable *safe to ship*? Timeouts, permissions, SHA pins, `Required`
  completeness, concurrency, and behavior tests that extract the shipped `run:`
  text out of the callables and execute it against fixtures, so the tests cannot
  drift from the workflow they describe.

All of it is runnable locally from a clean checkout with no credentials, given
`python3` with PyYAML, `node`, `actionlint`, and `shellcheck`, and the whole gate
is deliberately cheap. Two conventions it exists to protect: every callable ends
with a job named exactly `Required`, and every caller names its calling job id
`ci`, so the composed check context is the identical `ci / Required` string
estate-wide. Callers pin `@vN` or a full commit SHA, never `@main`.

## Cloud sessions

A cloud container starts with none of the Mac-side wiring. Read the estate
operating manual first (root `AGENTS.md` in
[narduk-enterprises/agent-infrastructure](https://github.com/narduk-enterprises/agent-infrastructure)),
then its `docs/cloud-sessions.md` for the container bootstrap and the credential
boundary. The rollout program that added this pointer is tracked in
[company-hq#487](https://github.com/narduk-enterprises/company-hq/issues/487).

## Issue labels

Labels that exist here: GitHub's stock defaults (`bug`, `documentation`,
`duplicate`, `enhancement`, `good first issue`, `help wanted`, `invalid`,
`question`, `wontfix`) plus `triaged-keep` and `area:foundation`. The estate
baseline's `P0-critical`–`P3-low` priority axis has not been created here. Label
from that set at issue creation; never invent labels that do not exist in the
repo.
