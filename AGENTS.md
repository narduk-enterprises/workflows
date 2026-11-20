# workflows — agent guide

workflows holds the shared GitHub Actions that the rest of the `narduk-enterprises`
estate calls instead of copying YAML: fix CI in one place, not in a hundred repos.

- **v1 and v2: reusable workflows** in [`.github/workflows/`](./.github/workflows/), all
  `on: workflow_call` only: `apple.yml`, `change-scope.yml`, `cloudflare-version-upload.yml`,
  `design-ledger.yml`, `design-publish.yml`, `docs-governance.yml`, `flake-digest.yml`,
  `node-library.yml`, `nuxt-cloudflare.yml`, `python-data.yml`, `red-main-listener.yml`,
  `reusable-browser-tests.yml`, `taskgraph.yml`. `ci.yml` and `red-main-self.yml` are the two
  non-callables: this repo's own gate and its own red-main listener. `closing-syntax-check.yml`,
  `code-review.yml`, `reusable-node-ci.yml` and `cursor-review.yml` are retired (README, Catalog
  and Adopters).
- **v3: four composite actions** under [`actions/`](./actions/README.md) (`setup`, `verify`,
  `version-proof`, `health`): steps a caller's own job uses, with no runner input and no
  `Required` job. `scripts/test_actions.py` is their gate.

The repo is public: its own CI and every public caller use GitHub-hosted runners. Private callers
keep their reusable-workflow access and may use self-hosted capacity where their repository allows.

## Checks before a push

`python3 scripts/precheck` runs the whole gate from a clean checkout, one timed line per command:
it installs actionlint, runs `actionlint -no-color -oneline .github/workflows/*.yml`,
`python3 scripts/lint_callables.py`, then every `python3 scripts/test_*.py`, and stops at the first
failure. It needs `python3` with PyYAML, `node`, `shellcheck` on PATH (without it actionlint skips
run-block linting and still exits 0, so precheck refuses to run) and, off Linux x64 where no
prebuilt actionlint exists, Go 1.26.1 to build it. No credentials.
While iterating, run the one test that covers what you changed (`test_actions.py` alone covers
`actions/`).

[`.github/workflows/ci.yml`](./.github/workflows/ci.yml) runs the same checks in CI as
`Validate workflows`, the required check on `main`. It is the only thing here that looks at a
callable before its adopters do: a defect in this repo fails every repo pinned to the commit
carrying it. Its two kinds of check:

- **`actionlint`**: is the workflow _valid_? Schema, expressions, and shellcheck over every `run:`
  block.
- **`scripts/lint_callables.py` plus the `scripts/test_*.py` suite**: is the callable _safe to
  ship_? Timeouts, permissions, SHA pins, `Required` completeness, concurrency (the rule table is at
  the top of `lint_callables.py`), and behavior tests that extract the shipped `run:` text out of
  the callables and execute it against fixtures, so the tests cannot drift from the workflow they
  describe.

Two conventions it protects: every callable ends with a job named exactly `Required`, and every
caller names its calling job id `ci`, so the composed check context is the identical
`ci / Required` everywhere. Callers pin a full commit SHA and name the tag in a comment, never
`@main` (README, Versioning policy). Tags move by hand after a merge, never as a side effect of it.

## Pull requests

Small pull requests, one concern each; squash merge. The catalog, how to call each callable and
how tags move are in [`README.md`](./README.md).

## Issue labels

Label an issue from what exists here (`gh label list`): a type from GitHub's stock set (`bug`,
`enhancement`, `documentation`, `question`, …), a priority (`P0-critical` to `P3-low`), and
`area:foundation` where it fits. Never invent a label.
