#!/usr/bin/env python3
"""Structural gate for this repo's reusable workflows.

`actionlint` checks that a workflow is *valid*. It cannot check that a callable
in this repo is *safe to hand seven adopters*, because those rules are local
conventions, not GitHub schema. This script checks the conventions.

Why a gate at all: every file here is `on: workflow_call`, so until this landed
nothing in this repository validated a callable before an adopter ran it
(company-hq#269, "Noted, not filed"). A defect here does not fail one repo, it
fails every repo pinned to the tag that carries it.

Each rule below exists because breaking it has a specific, known blast radius:

  R1  top-level `permissions:`         a job added later inherits least
                                       privilege instead of the caller's grant
  R2  every job has `timeout-minutes`  a hung job otherwise runs to GitHub's
                                       6-hour default while holding one of six
                                       `linux-ci` slots, three browser guests,
                                       or the single Mac slot
  R3  every job has `permissions:`     job-level replaces (never merges with)
                                       the workflow level, so an omission is
                                       silent
  R4  actions pinned to a full SHA     a moved tag is a supply-chain change
      with a `# vX.Y.Z` comment        nobody reviews; the comment is what
                                       makes the pin auditable
  R5  `Required` needs EVERY job       adding a job without adding it to
                                       `needs:` produces a green `ci / Required`
                                       that never saw the new job — a required
                                       check that silently stops checking
  R6  no workflow-level `concurrency`  `${{ github.workflow }}` in a called
      in a callable                    workflow resolves to the CALLER's name,
                                       so a group here can cancel the caller's
                                       own run. See README "Concurrency".
  R7  declared secrets are optional    `required: true` would hard-fail every
                                       caller that does not hold the secret
  R8  every dependency-cache step is   a self-hosted guest's package store is
      guarded to GitHub-hosted only    PERSISTENT and SHARED, so `cache/save`
                                       tars it while other lanes write into it
                                       and `cache/restore` lays the torn
                                       archive back over a live store. That is
                                       the confirmed source of the estate's
                                       `ERR_PNPM_BAD_PACKAGE_JSON` corruption
                                       (vtraceroute#4, company-hq#269)
  R9  a pnpm install on a self-hosted   pnpm hard-links `node_modules` out of
      job is preceded by the store-      its store and a hard link cannot cross
      placement step                     a device boundary. On `linux-ci` the
                                        workspace is on the transient volume
                                        and the default store is on the root
                                        volume, so an install without this step
                                        silently COPIES every file: slower, and
                                        220 MiB of transient volume per
                                        workspace instead of 17 MiB, on the
                                        volume whose 80% mark blocks new
                                        allocations (company-hq#269,
                                        been-sober-for#74)
  R10 a `run:` block that pipes into    without pipefail, `cmd | tee file`
      `tee` must enable pipefail        returns tee's exit status (usually 0),
                                        so a failing build/test/lint still
                                        paints the step green — a gate that
                                        cannot go red. set -o pipefail (or
                                        set -euo pipefail) makes the pipeline
                                        fail on cmd's status

Run: python3 scripts/lint_callables.py [paths...]
Exit 0 clean, 1 on any finding. No third-party imports beyond PyYAML.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

SHA_PIN = re.compile(r"^[0-9a-f]{40}$")
# `uses: owner/repo@<sha> # v1.2.3` — the trailing comment is required.
USES_LINE = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)\s*(#.*)?$")
VERSION_COMMENT = re.compile(r"#\s*v?\d+(\.\d+)*")
# `timeout-minutes: ${{ inputs.foo }}` — legal only if `foo` has a finite default.
INPUT_EXPR = re.compile(r"^\$\{\{\s*inputs\.([A-Za-z0-9_-]+)\s*\}\}$")

# Local composite/local-path uses are exempt from SHA pinning: `./...` and
# `owner/repo/.github/workflows/x.yml@<ref>` calls resolved inside this repo.
LOCAL_USES = ("./", "docker://")


class Findings:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, path: Path, msg: str) -> None:
        self.items.append(f"{path}: {msg}")


def workflow_call_inputs(doc: dict) -> dict:
    # PyYAML parses the bare key `on:` as the boolean True.
    on = doc.get("on", doc.get(True))
    if not isinstance(on, dict):
        return {}
    call = on.get("workflow_call")
    if not isinstance(call, dict):
        return {}
    return call


def check_timeout(path: Path, job_id: str, job: dict, inputs: dict, f: Findings) -> None:
    value = job.get("timeout-minutes")
    if value is None:
        f.add(path, f"R2 job '{job_id}' has no timeout-minutes (would inherit GitHub's 6-hour default)")
        return
    if isinstance(value, (int, float)):
        return
    m = INPUT_EXPR.match(str(value).strip())
    if not m:
        f.add(path, f"R2 job '{job_id}' timeout-minutes is neither a number nor a plain inputs.* reference: {value!r}")
        return
    name = m.group(1)
    spec = inputs.get(name)
    if not isinstance(spec, dict):
        f.add(path, f"R2 job '{job_id}' timeout-minutes references undeclared input '{name}'")
        return
    default = spec.get("default")
    if not isinstance(default, (int, float)):
        # An input with no default that a caller omits must still be finite.
        f.add(
            path,
            f"R2 job '{job_id}' timeout-minutes uses input '{name}', which has no finite numeric default "
            f"(got {default!r}) — a caller that omits it would get no timeout at all",
        )


def check_uses_pins(path: Path, f: Findings) -> None:
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        m = USES_LINE.match(line)
        if not m:
            continue
        ref, comment = m.group(1), m.group(2) or ""
        if ref.startswith(LOCAL_USES):
            continue
        if "@" not in ref:
            f.add(path, f"R4 line {lineno}: `uses: {ref}` has no ref at all")
            continue
        pin = ref.rsplit("@", 1)[1]
        if not SHA_PIN.match(pin):
            f.add(path, f"R4 line {lineno}: `uses: {ref}` is not pinned to a full 40-character commit SHA")
            continue
        if not VERSION_COMMENT.search(comment):
            f.add(
                path,
                f"R4 line {lineno}: `uses: {ref}` is SHA-pinned but carries no `# vX.Y.Z` comment, "
                "so nobody can tell what version it is without a network call",
            )


def check_required_job(path: Path, doc: dict, f: Findings) -> None:
    jobs = doc.get("jobs") or {}
    required = {jid: j for jid, j in jobs.items() if (j.get("name") or jid) == "Required"}
    if not required:
        return
    for jid, job in required.items():
        cond = str(job.get("if", "")).strip()
        if "always()" not in cond:
            f.add(path, f"R5 job '{jid}' is named Required but its `if:` is {cond!r} — it must be `always()`")
        needs = job.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        missing = sorted(set(jobs) - set(needs) - {jid})
        if missing:
            f.add(
                path,
                f"R5 job '{jid}' does not `needs:` {missing} — `ci / Required` would report green "
                "without ever having seen those jobs",
            )


HOSTED_GUARD = "runner.environment == 'github-hosted'"
CACHE_STEP_NAMES = {
    "Resolve dependency cache directory",
    "Restore dependency cache",
    "Save dependency cache",
}


def check_cache_guards(path: Path, doc: dict, f: Findings) -> None:
    """R8: no dependency-cache step may run on a self-hosted runner.

    The store there is persistent and shared, so tarballing it captures other
    lanes mid-write. Restore is guarded too, not just save: an unsound writer
    poisons every reader of the same key.
    """
    for job_id, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            name = step.get("name", "")
            uses = str(step.get("uses", ""))
            if "actions/cache" not in uses and name not in CACHE_STEP_NAMES:
                continue
            cond = " ".join(str(step.get("if", "")).split())
            if HOSTED_GUARD not in cond:
                f.add(
                    path,
                    f"R8 job '{job_id}' step '{name or uses}' touches the dependency cache without "
                    f"`if: {HOSTED_GUARD}` — on a self-hosted guest the store is persistent and shared, "
                    "so saving tars it mid-write and restoring lays the torn archive back over it",
                )


PNPM_INSTALL_STEP = "Install dependencies (pnpm)"
STORE_STEP = "Point pnpm at a workspace-local store (self-hosted)"
SELF_HOSTED_GUARD = "runner.environment == 'self-hosted'"


def check_store_placement(path: Path, doc: dict, f: Findings) -> None:
    """R9: a pnpm install must be preceded by the store-placement step.

    Without it the store stays at pnpm's default under `$HOME`, which on a
    self-hosted guest is a DIFFERENT FILESYSTEM from the workspace — and pnpm
    cannot hard-link across a device boundary, so it copies instead. That
    failure is silent: same result, slower, and a full private copy of every
    dependency on the volume the reclaimer is fighting to keep under 80%.
    """
    for job_id, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        steps = [s for s in (job.get("steps") or []) if isinstance(s, dict)]
        names = [s.get("name", "") for s in steps]
        if PNPM_INSTALL_STEP not in names:
            continue
        if STORE_STEP not in names:
            f.add(
                path,
                f"R9 job '{job_id}' installs with pnpm but has no '{STORE_STEP}' step — on a "
                "self-hosted guest the default store is on a different filesystem from the "
                "workspace, so pnpm copies every file instead of hard-linking it",
            )
            continue
        if names.index(STORE_STEP) > names.index(PNPM_INSTALL_STEP):
            f.add(path, f"R9 job '{job_id}' places the store AFTER the install, which is too late")
        placement = steps[names.index(STORE_STEP)]
        cond = " ".join(str(placement.get("if", "")).split())
        if SELF_HOSTED_GUARD not in cond:
            f.add(
                path,
                f"R9 job '{job_id}' step '{STORE_STEP}' is missing `if: {SELF_HOSTED_GUARD}` — a "
                "GitHub-hosted runner has no transient mount and its restored cache already shares "
                "the workspace's filesystem",
            )


# `cmd | tee file` (or `tee` as the last stage of a longer pipe). Word-boundary
# on both sides so comments that merely *mention* tee, and identifiers like
# `guaranteed`, do not trip the rule. Matches the pipeline form only — bare
# `tee file < input` is uncommon here and does not have the same status-mask
# shape (its status is tee's by definition, not a masked left-hand command).
TEE_PIPELINE = re.compile(r"\|\s*tee\b")
# Any of the usual ways a step enables pipefail for its shell.
# Anchor this to a shell command line so a comment mentioning "pipefail"
# cannot satisfy R10.
PIPEFAIL = re.compile(
    r"(?m)^\s*set\s+(?:-[a-zA-Z]*o\s+pipefail|-o\s+pipefail)\s*(?:#.*)?$"
)


def _run_script(step: dict) -> str:
    """Return the shell text of a step's `run:` field, or '' if absent/non-shell."""
    run = step.get("run")
    if isinstance(run, str):
        return run
    return ""


def check_tee_pipefail(path: Path, doc: dict, f: Findings) -> None:
    """R10: `| tee` in a run block requires pipefail in that same step.

    Without pipefail the pipeline's exit status is tee's (almost always 0), so
    a failing left-hand command still reports success. That is the
    "check that cannot fail" class. Prefer fixing with `set -euo pipefail` at
    the top of the step (the house style here) rather than rewriting the
    capture; either is fine as long as the real exit code propagates.
    """
    for job_id, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        # Job- or workflow-level defaults.run.shell with pipefail also count,
        # but this repo sets pipefail inside each step rather than via defaults.
        # Scan step text only so the rule stays local and reviewable.
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            script = _run_script(step)
            if not script or not TEE_PIPELINE.search(script):
                continue
            if PIPEFAIL.search(script):
                continue
            # defaults.run.shell: bash {0} with -o pipefail would also clear
            # this, but we do not declare that form anywhere; require the
            # explicit set so a reader of the step can see the guarantee.
            name = step.get("name") or "(unnamed step)"
            f.add(
                path,
                f"R10 job '{job_id}' step '{name}' pipes into `tee` without enabling pipefail "
                "in that step — the pipeline would report tee's status (usually 0) even when "
                "the left-hand command fails. Add `set -euo pipefail` (or `set -o pipefail`) "
                "at the top of the step, or capture without a pipe",
            )


def check_file(path: Path, f: Findings) -> None:
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict):
        f.add(path, "does not parse as a YAML mapping")
        return

    call = workflow_call_inputs(path and doc)
    is_callable = bool(call) or "workflow_call" in str(doc.get("on", doc.get(True)))

    if doc.get("permissions") is None:
        f.add(path, "R1 no top-level `permissions:` block")

    if is_callable and doc.get("concurrency") is not None:
        f.add(
            path,
            "R6 workflow-level `concurrency:` in a `workflow_call` workflow — the group is evaluated in the "
            "CALLER's context and can cancel the caller's own run. Concurrency belongs to the caller.",
        )

    for name, spec in (call.get("secrets") or {}).items():
        if isinstance(spec, dict) and spec.get("required") is True:
            f.add(path, f"R7 secret '{name}' is `required: true` — every caller without it hard-fails")

    inputs = call.get("inputs") or {}
    for job_id, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        check_timeout(path, job_id, job, inputs, f)
        if job.get("permissions") is None:
            f.add(path, f"R3 job '{job_id}' has no `permissions:` block (job level replaces, never merges)")

    check_uses_pins(path, f)
    check_required_job(path, doc, f)
    check_cache_guards(path, doc, f)
    check_store_placement(path, doc, f)
    check_tee_pipefail(path, doc, f)


def main(argv: list[str]) -> int:
    paths = [Path(p) for p in argv[1:]] or sorted(Path(".github/workflows").glob("*.yml"))
    if not paths:
        print("no workflow files found", file=sys.stderr)
        return 1
    f = Findings()
    for path in paths:
        check_file(path, f)
    for item in f.items:
        print(f"::error::{item}")
    print(f"\nlint_callables: {len(paths)} file(s) checked, {len(f.items)} finding(s)")
    return 1 if f.items else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
