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
  R11 isolated Playwright is fail       the image is the browser supply-chain
      closed and download-free          boundary. Installer opt-ins, path
                                        overrides, warning-only/missing gates,
                                        continue-on-error, or accepting a
                                        skipped enabled E2E job turn drift into
                                        a false green (company-hq#343)
  R12 no job requests a permission      a job in a CALLED workflow may only
      outside the callable's            request permissions the CALLER granted.
      documented caller grant           Ask for one it did not and the caller's
                                        ENTIRE run dies at startup with zero
                                        jobs, no logs and no annotation, on
                                        every adopter simultaneously, the
                                        moment `v1` moves. `pull-requests:
                                        read` added to nuxt-cloudflare.yml's
                                        `E2E plan` job did exactly that to
                                        every `@v1` adopter (workflows#59).
                                        Widening a set here is a BREAKING
                                        interface change: every caller must
                                        grant the new permission BEFORE the
                                        tag carrying it moves

  NON_GATING_JOBS is the one sanctioned exception to R5 and to R11's
  continue-on-error rule: a named, reviewed job that must NOT reach the gate
  (the quarantine lane). It is exempt only while it is `continue-on-error:
  true`, `Required` does not list it, and no other job `needs:` it. Every
  other R11 rule still applies to it.

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

# R12: the permission ceiling each callable's DOCUMENTED caller grant covers —
# the `permissions:` block README tells adopters to put on their `ci:` job. No
# job in that file may request a key outside its set. This is an INTERFACE, not
# a preference: a called workflow's job asking for a permission its caller did
# not grant fails the caller's whole run at startup (`startup_failure`, zero
# jobs, no annotation), which is why widening one of these sets means updating
# every adopter first and only then moving the tag (workflows#59).
CALLER_GRANTS: dict[str, set[str]] = {
    "apple.yml": {"contents"},
    "closing-syntax-check.yml": {"contents"},
    "code-review.yml": {"contents"},
    "docs-governance.yml": {"contents"},
    "node-library.yml": {"contents"},
    # WIDENED for the `preview` job's sticky pull-request comment (V1). This
    # is a BREAKING interface change, not a tidy-up: every adopter's `ci:` job
    # must add `pull-requests: write` BEFORE the `v1` tag moves onto the commit
    # that carries it, or its whole run dies at startup exactly the way
    # workflows#59 did. `write`, not `read`: the same grant both reads
    # Cloudflare's preview comment and updates the sticky one.
    "nuxt-cloudflare.yml": {"contents", "packages", "pull-requests"},
    "python-data.yml": {"contents"},
    "reusable-browser-tests.yml": {"contents", "packages", "actions"},
    "reusable-node-ci.yml": {"contents", "packages"},
}

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
    if "uses" in job:
        # A reusable-workflow call cannot declare a job timeout. The called
        # workflow's execution jobs own it and are checked in their own file.
        return
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


# workflow file name -> job ids that deliberately never reach `Required`.
# Adding an entry is a gate-shape change; see the module docstring.
NON_GATING_JOBS: dict[str, frozenset[str]] = {
    # `e2e-quarantine-args`: quarantined tests run here, non-blocking, so
    # their pass history can build up without a flake failing the gate.
    "nuxt-cloudflare.yml": frozenset({"e2e-quarantine"}),
}


def non_gating_jobs(path: Path, doc: dict, f: Findings) -> frozenset[str]:
    """The declared non-gating jobs present in this file, after checking that each
    one really cannot reach the gate."""
    jobs = doc.get("jobs") or {}
    declared = NON_GATING_JOBS.get(path.name, frozenset()) & set(jobs)
    for jid in sorted(declared):
        if jobs[jid].get("continue-on-error") is not True:
            f.add(path, f"R5 non-gating job '{jid}' must declare `continue-on-error: true`")
        for other_id, other in jobs.items():
            needs = other.get("needs") or []
            if isinstance(needs, str):
                needs = [needs]
            if jid in needs:
                f.add(
                    path,
                    f"R5 non-gating job '{jid}' is needed by '{other_id}' — a non-gating job "
                    "must not feed any job, least of all `Required`",
                )
    return declared


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
        missing = sorted(set(jobs) - set(needs) - {jid} - non_gating_jobs(path, doc, f))
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


def check_fail_closed_playwright(path: Path, doc: dict, f: Findings) -> None:
    """R11: the isolated browser lane cannot download, skip, or soften drift."""
    jobs = doc.get("jobs") or {}
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if "continue-on-error" in job and job_id not in NON_GATING_JOBS.get(path.name, frozenset()):
            f.add(path, f"R11 job '{job_id}' declares continue-on-error — a required gate may not soften failure")
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "continue-on-error" in step:
                f.add(
                    path,
                    f"R11 job '{job_id}' step '{step.get('name') or step.get('uses') or '(unnamed)'}' "
                    "declares continue-on-error — a required gate may not soften failure",
                )

    # Any OTHER job routed to the browser pool is held to the same fail-closed
    # preflight as `e2e`. Without this, adding a second browser-capable job
    # (the `preview` job's `e2e-subset` lane) would silently escape R11
    # entirely: the pool's image-equality gate would be enforced on one lane
    # and absent on the other, which is the same false green company-hq#343 is
    # about.
    for job_id, job in jobs.items():
        if job_id == "e2e" or not isinstance(job, dict):
            continue
        if "e2e-runner" not in str(job.get("runs-on", "")):
            continue
        steps = [step for step in (job.get("steps") or []) if isinstance(step, dict)]
        names = [step.get("name") for step in steps]
        for required_name in ("Guard isolated Playwright route", "Assert isolated Playwright toolchain"):
            if required_name not in names:
                f.add(
                    path,
                    f"R11 job '{job_id}' is routed to the browser pool but has no "
                    f"'{required_name}' step",
                )
        installs = [
            names.index(name)
            for name in ("Install dependencies (pnpm)", "Install dependencies (npm)")
            if name in names
        ]
        if "Guard isolated Playwright route" in names and installs:
            if names.index("Guard isolated Playwright route") > min(installs):
                f.add(
                    path,
                    f"R11 job '{job_id}' runs its isolated-route guard after dependency "
                    "installation — browser acquisition could already occur",
                )
        if "Install Playwright browsers" in names:
            installer_condition = " ".join(
                str(steps[names.index("Install Playwright browsers")].get("if", "")).split()
            )
            for fragment in (
                "inputs.e2e-install-browsers",
                "!contains",
                "playwright-isolated",
                "proxmox-playwright-x64",
            ):
                if fragment not in installer_condition:
                    f.add(
                        path,
                        f"R11 job '{job_id}' browser installer is not proven unreachable on "
                        f"the isolated route (missing {fragment!r})",
                    )

    e2e = jobs.get("e2e")
    if not isinstance(e2e, dict) or "e2e-runner" not in str(e2e.get("runs-on", "")):
        return
    steps = [step for step in (e2e.get("steps") or []) if isinstance(step, dict)]
    by_name = {step.get("name"): step for step in steps if step.get("name")}
    required_names = {
        "Guard isolated Playwright route",
        "Assert isolated Playwright toolchain",
        "Install Playwright browsers",
        "Run e2e suite",
    }
    missing = sorted(required_names - set(by_name))
    if missing:
        f.add(path, f"R11 e2e job lacks fail-closed Playwright step(s): {missing}")
        return

    guard = by_name["Guard isolated Playwright route"]
    assertion = by_name["Assert isolated Playwright toolchain"]
    installer = by_name["Install Playwright browsers"]
    suite = by_name["Run e2e suite"]
    names = [step.get("name") for step in steps]
    first_install = min(
        names.index(name)
        for name in ("Install dependencies (pnpm)", "Install dependencies (npm)")
        if name in names
    )
    if names.index("Guard isolated Playwright route") > first_install:
        f.add(path, "R11 isolated-route guard runs after dependency installation — browser acquisition could already occur")
    if names.index("Assert isolated Playwright toolchain") > names.index("Run e2e suite"):
        f.add(path, "R11 Playwright image equality is asserted after the suite, which is too late")

    for label, step in (("route guard", guard), ("toolchain assertion", assertion)):
        condition = str(step.get("if", ""))
        script = _run_script(step)
        if "always()" in condition:
            f.add(path, f"R11 Playwright {label} uses always() — it is a gate, not a diagnostic")
        if "|| true" in script:
            f.add(path, f"R11 Playwright {label} contains `|| true` — failure would be swallowed")

    assertion_condition = " ".join(str(assertion.get("if", "")).split())
    for marker in ("playwright-isolated", "proxmox-playwright-x64"):
        if marker not in assertion_condition:
            f.add(path, f"R11 toolchain assertion condition does not cover isolated route marker {marker!r}")

    installer_condition = " ".join(str(installer.get("if", "")).split())
    for fragment in (
        "inputs.e2e-install-browsers",
        "!contains",
        "playwright-isolated",
        "proxmox-playwright-x64",
    ):
        if fragment not in installer_condition:
            f.add(path, f"R11 browser installer is not proven unreachable on the isolated route (missing {fragment!r})")

    suite_script = _run_script(suite)
    for forbidden in ("--if-present", "::warning::", "|| true"):
        if forbidden in suite_script:
            f.add(path, f"R11 E2E suite gate contains forbidden fail-open shape {forbidden!r}")

    required = next(
        (job for job in jobs.values() if isinstance(job, dict) and (job.get("name") or "") == "Required"),
        None,
    )
    required_text = "\n".join(
        _run_script(step) for step in (required or {}).get("steps", []) if isinstance(step, dict)
    )
    if "require_success e2e \"$E2E_RESULT\"" not in required_text:
        f.add(path, "R11 Required does not demand success from enabled E2E; a skipped toolchain gate could satisfy CI")
    if "allow_skip e2e " in required_text:
        f.add(path, "R11 Required still accepts a skipped enabled E2E job")


def check_permission_ceiling(path: Path, doc: dict, f: Findings) -> None:
    """R12 — no job may request a permission outside the callable's caller grant."""
    grant = CALLER_GRANTS.get(path.name)
    if grant is None:
        f.add(
            path,
            "R12 callable has no entry in lint_callables.CALLER_GRANTS — declare the exact "
            "permission set its README-documented caller grant covers before shipping it",
        )
        return
    for job_id, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        perms = job.get("permissions")
        if perms is None:
            continue  # R3 already reports this
        if not isinstance(perms, dict):
            f.add(
                path,
                f"R12 job '{job_id}' uses the shorthand `permissions: {perms}` — a called "
                "workflow must name each scope so the caller grant can be audited",
            )
            continue
        extra = sorted(set(perms) - grant)
        if extra:
            f.add(
                path,
                f"R12 job '{job_id}' requests {extra} beyond the caller grant "
                f"{sorted(grant)} — a called job asking for a permission its caller did not "
                "grant fails the caller's ENTIRE run at startup (workflows#59). Update every "
                "adopter's `permissions:` block and CALLER_GRANTS first, then move the tag",
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
    if is_callable:
        check_permission_ceiling(path, doc, f)
    check_required_job(path, doc, f)
    check_cache_guards(path, doc, f)
    check_store_placement(path, doc, f)
    check_tee_pipefail(path, doc, f)
    check_fail_closed_playwright(path, doc, f)


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
