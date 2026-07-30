#!/usr/bin/env python3
"""Source-level contract: nuxt-cloudflare package auth stays install-scoped.

Persistent self-hosted guests keep the workspace volume between jobs. The
callable can materialise `${working-directory}/.npmrc.auth` for
`NPM_CONFIG_USERCONFIG`; that exact path must be removed with `if: always()`
after the install consumers so success, failed install, and cancelled jobs
cannot leave registry credentials on the guest.

Callers can instead opt into an app-owned install wrapper. In that path the
workflow must expose the service credential only as `NVAULT_TOKEN`, suppress
the legacy direct-materialization path, validate the package.json script name,
and invoke it without eval.

This is a pure source contract over the shipped YAML (same discipline as
`test_e2e_build_artifact.py`): enumerate every auth-creation site, require the
paired legacy cleanup and the caller-owned alternative, then seed red
mutations so the assertions cannot pass against a weakened workflow.

Run: python3 scripts/test_package_auth_cleanup.py
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import subprocess
import tempfile

import yaml

WORKFLOW = Path(".github/workflows/nuxt-cloudflare.yml")

AUTH_STEP = "Configure package registry auth"
CLEANUP_STEP = "Remove package auth materialization"
CALLER_INSTALL_STEP = "Install dependencies (caller script)"
INSTALL_STEPS = (
    "Install dependencies (pnpm)",
    "Install dependencies (npm)",
)

# Jobs in nuxt-cloudflare.yml that can materialise .npmrc.auth via the
# package-registry auth bootstrap (token present + consumer auth script).
AUTH_JOBS = (
    "build",
    "extra-gate",
    "e2e",
    "e2e-report",
    "deploy-dry-run",
)

AUTH_PATH_EXPR = (
    "${{ github.workspace }}/${{ inputs.working-directory }}/.npmrc.auth"
)
CLEANUP_RM = (
    'rm -f "${GITHUB_WORKSPACE}/${{ inputs.working-directory }}/.npmrc.auth"'
)

# Patterns that would print auth content or delete more than the exact file.
FORBIDDEN_CLEANUP = (
    re.compile(r"\bcat\b"),
    re.compile(r"\btee\b"),
    re.compile(r"\bhead\b"),
    re.compile(r"\btail\b"),
    re.compile(r"\bless\b"),
    re.compile(r"\bmore\b"),
    re.compile(r"\bprintenv\b"),
    re.compile(r"\becho\b.*npmrc", re.I),
    # Recursive rm flag only (do not match incidental 'r' letters in paths).
    re.compile(r"\brm\s+-[a-zA-Z]*r\b"),
    re.compile(r"\brm\s+--recursive\b"),
)


def named_steps(job: dict, name: str) -> list[dict]:
    return [step for step in job.get("steps", []) if step.get("name") == name]


def step_names(job: dict) -> list[str]:
    return [step.get("name") for step in job.get("steps", []) if step.get("name")]


def auth_jobs(document: dict) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for job_id, job in (document.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        if named_steps(job, AUTH_STEP):
            found[job_id] = job
    return found


def validate_cleanup_step(job_id: str, step: dict) -> None:
    assert step.get("if") == "always() && inputs.install-script == ''", (
        f"{job_id}: cleanup must always run for the legacy materialization path, "
        f"got {step.get('if')!r}"
    )
    run = step.get("run")
    assert isinstance(run, str) and run.strip(), f"{job_id}: cleanup missing run:"
    assert CLEANUP_RM in run, (
        f"{job_id}: cleanup must remove exact auth materialization path; got {run!r}"
    )
    # Single exact file only — reject broader deletes and content printers.
    assert run.count("rm ") == 1, f"{job_id}: cleanup must be a single rm"
    assert ".npmrc.auth" in run
    assert "*" not in run
    assert ".." not in run.replace("${{ inputs.working-directory }}", "")
    for pattern in FORBIDDEN_CLEANUP:
        assert not pattern.search(run), (
            f"{job_id}: cleanup run matches forbidden pattern {pattern.pattern!r}: {run!r}"
        )


def validate_job(job_id: str, job: dict) -> None:
    names = step_names(job)
    auth = named_steps(job, AUTH_STEP)
    cleanup = named_steps(job, CLEANUP_STEP)
    assert len(auth) == 1, f"{job_id}: expected one auth step, found {len(auth)}"
    assert len(cleanup) == 1, (
        f"{job_id}: expected one cleanup step after auth materialization, found {len(cleanup)}"
    )
    validate_cleanup_step(job_id, cleanup[0])

    auth_i = names.index(AUTH_STEP)
    cleanup_i = names.index(CLEANUP_STEP)
    assert auth_i < cleanup_i, f"{job_id}: cleanup must follow auth configuration"
    assert auth[0].get("if") == "inputs.install-script == ''", (
        f"{job_id}: legacy auth must be disabled when caller-owned install is selected"
    )

    # Install consumers that point NPM_CONFIG_USERCONFIG at .npmrc.auth must
    # run before cleanup so the file still exists for a successful install.
    for install_name in INSTALL_STEPS:
        if install_name not in names:
            continue
        install_i = names.index(install_name)
        assert auth_i < install_i < cleanup_i, (
            f"{job_id}: {install_name} must sit between auth and cleanup "
            f"(auth={auth_i}, install={install_i}, cleanup={cleanup_i})"
        )
        install_step = named_steps(job, install_name)[0]
        assert install_step.get("if", "").startswith("inputs.install-script == '' &&"), (
            f"{job_id}/{install_name}: legacy install must be disabled for caller-owned install"
        )
        env = install_step.get("env") or {}
        userconfig = env.get("NPM_CONFIG_USERCONFIG")
        if userconfig is not None:
            assert userconfig == AUTH_PATH_EXPR, (
                f"{job_id}/{install_name}: unexpected NPM_CONFIG_USERCONFIG {userconfig!r}"
            )

    caller_install = named_steps(job, CALLER_INSTALL_STEP)
    assert len(caller_install) == 1, (
        f"{job_id}: expected one caller-owned install step, found {len(caller_install)}"
    )
    caller = caller_install[0]
    assert caller.get("if") == "inputs.install-script != ''"
    caller_env = caller.get("env") or {}
    assert caller_env.get("NVAULT_TOKEN") == "${{ secrets.NARDUK_PLATFORM_GH_PACKAGES_READ }}"
    assert "NARDUK_PLATFORM_GH_PACKAGES_READ" not in caller_env
    caller_run = caller.get("run") or ""
    assert "case \"$INSTALL_SCRIPT\" in" in caller_run
    assert "*[!A-Za-z0-9:_-]*" in caller_run
    assert '"$PM" run "$INSTALL_SCRIPT"' in caller_run
    assert "eval " not in caller_run
    assert "NVAULT_TOKEN" not in caller_run
    assert names.index(CALLER_INSTALL_STEP) > cleanup_i


def validate(document: dict) -> None:
    found = auth_jobs(document)
    assert set(found) == set(AUTH_JOBS), (
        f"auth-materializing jobs changed: expected {set(AUTH_JOBS)}, got {set(found)}"
    )
    for job_id in AUTH_JOBS:
        validate_job(job_id, found[job_id])

    # No other nuxt-cloudflare job may mention .npmrc.auth without the paired
    # cleanup contract above (guards a silent new materialization site).
    for job_id, job in (document.get("jobs") or {}).items():
        if job_id in AUTH_JOBS:
            continue
        blob = yaml.safe_dump(job)
        assert ".npmrc.auth" not in blob, (
            f"job {job_id!r} references .npmrc.auth but has no auth bootstrap step"
        )


def cleanup_is_safe_when_absent_and_targeted() -> None:
    """Execute the shipped cleanup command shape against a temp workspace."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        wd = workspace / "apps" / "web"
        wd.mkdir(parents=True)
        auth = wd / ".npmrc.auth"
        sibling = wd / "package.json"
        sibling.write_text('{"name":"f"}\n')
        auth.write_text("# fake auth materialization\n//registry.npmjs.org/:_authToken=REDACTED\n")

        # Missing file must not fail.
        missing = subprocess.run(
            ["bash", "-c", 'rm -f "${GITHUB_WORKSPACE}/${WORKING_DIRECTORY}/.npmrc.auth"'],
            env={
                "GITHUB_WORKSPACE": str(workspace),
                "WORKING_DIRECTORY": "does-not-exist-yet",
                "PATH": "/usr/bin:/bin",
            },
            capture_output=True,
            text=True,
        )
        assert missing.returncode == 0, missing.stderr
        assert missing.stdout == ""
        assert "REDACTED" not in missing.stdout + missing.stderr

        # Present file is removed; siblings stay; stdout stays empty.
        present = subprocess.run(
            ["bash", "-c", 'rm -f "${GITHUB_WORKSPACE}/${WORKING_DIRECTORY}/.npmrc.auth"'],
            env={
                "GITHUB_WORKSPACE": str(workspace),
                "WORKING_DIRECTORY": "apps/web",
                "PATH": "/usr/bin:/bin",
            },
            capture_output=True,
            text=True,
        )
        assert present.returncode == 0, present.stderr
        assert present.stdout == ""
        assert "REDACTED" not in present.stdout + present.stderr
        assert not auth.exists()
        assert sibling.exists()


def main() -> None:
    document = yaml.safe_load(WORKFLOW.read_text())
    validate(document)
    cleanup_is_safe_when_absent_and_targeted()

    # Seeded-red: dropping cleanup from any single auth job must fail the contract.
    for job_id in AUTH_JOBS:
        candidate = deepcopy(document)
        job = candidate["jobs"][job_id]
        job["steps"] = [
            step
            for step in job["steps"]
            if step.get("name") != CLEANUP_STEP
        ]
        try:
            validate(candidate)
        except AssertionError:
            pass
        else:
            raise AssertionError(
                f"missing-cleanup mutation for job {job_id!r} did not fail the contract"
            )

    # Seeded-red: cleanup without always() must fail.
    candidate = deepcopy(document)
    step = named_steps(candidate["jobs"]["build"], CLEANUP_STEP)[0]
    step["if"] = "inputs.install-script == ''"
    try:
        validate(candidate)
    except AssertionError:
        pass
    else:
        raise AssertionError("non-always cleanup mutation did not fail the contract")

    # Seeded-red: broader delete must fail.
    candidate = deepcopy(document)
    step = named_steps(candidate["jobs"]["e2e"], CLEANUP_STEP)[0]
    step["run"] = 'rm -rf "${GITHUB_WORKSPACE}/${{ inputs.working-directory }}"'
    try:
        validate(candidate)
    except AssertionError:
        pass
    else:
        raise AssertionError("broad-delete cleanup mutation did not fail the contract")

    # Seeded-red: mapping the service token as a direct package token must fail.
    candidate = deepcopy(document)
    step = named_steps(candidate["jobs"]["build"], CALLER_INSTALL_STEP)[0]
    step["env"] = {
        "NARDUK_PLATFORM_GH_PACKAGES_READ": "${{ secrets.NARDUK_PLATFORM_GH_PACKAGES_READ }}",
        "INSTALL_SCRIPT": "${{ inputs.install-script }}",
        "PM": "${{ inputs.package-manager }}",
    }
    try:
        validate(candidate)
    except AssertionError:
        pass
    else:
        raise AssertionError("direct-package-token mapping mutation did not fail the contract")

    # Seeded-red: bypassing script-name validation must fail.
    candidate = deepcopy(document)
    step = named_steps(candidate["jobs"]["e2e"], CALLER_INSTALL_STEP)[0]
    step["run"] = '"$PM" run "$INSTALL_SCRIPT"'
    try:
        validate(candidate)
    except AssertionError:
        pass
    else:
        raise AssertionError("unvalidated caller-script mutation did not fail the contract")

    print(
        "package-auth install-scope contract passed "
        f"({len(AUTH_JOBS)} jobs, cleanup and caller-install red cases)"
    )


if __name__ == "__main__":
    main()
