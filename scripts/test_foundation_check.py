#!/usr/bin/env python3
"""Behaviour tests for `nuxt-cloudflare.yml`'s opt-in web-foundation gate.

Same discipline as `test_script_gates.py` and `test_store_placement.py`: this
does NOT test a copy of the logic, it extracts the two shipped `run:` blocks
("Run web-foundation conformance check" and "Evaluate web-foundation
conformance check") out of the workflow YAML and executes that exact text
under bash, so an edit to the callable either still passes these tests or
fails them.

Company-hq `docs/WEB-FOUNDATION-CHECK.md` §5: no warning tier -- `PASS` (exit
0) is the only green result; `FAIL` and `UNKNOWN` both block, and an artefact
this step cannot produce or parse is itself treated as `UNKNOWN` rather than
as a pass, because a check that cannot see cannot pass.

Run: python3 scripts/test_foundation_check.py
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile

import yaml

NUXT_CF = pathlib.Path(".github/workflows/nuxt-cloudflare.yml")


def gate_script(job: str, name: str) -> str:
    doc = yaml.safe_load(NUXT_CF.read_text())
    for step in doc["jobs"][job]["steps"]:
        if step.get("name") == name:
            return step["run"]
    raise SystemExit(f"::error::no step named {name!r} in {NUXT_CF} job {job!r}")


def input_default(name: str) -> object:
    doc = yaml.safe_load(NUXT_CF.read_text())
    trigger = doc.get("on", doc.get(True))
    return trigger["workflow_call"]["inputs"][name]["default"]


RUN_STEP = gate_script("build", "Run web-foundation conformance check")
EVAL_STEP = gate_script("build", "Evaluate web-foundation conformance check")

PNPM_STUB = """#!/usr/bin/env bash
set -euo pipefail
test "${GH_PACKAGES_READ:-}" = "$EXPECTED_PACKAGE_TOKEN"
test -z "${NARDUK_PLATFORM_GH_PACKAGES_READ:-}"
echo "pnpm $*" >> "$STUB_LOG"
if [ "$1" = "exec" ] && [ "$2" = "narduk-app" ]; then
  cat "$FIXTURE_JSON"
  exit 0
fi
if [ "$1" = "dlx" ]; then
  if [ -z "${NPM_CONFIG_USERCONFIG:-}" ] || [ ! -f "$NPM_CONFIG_USERCONFIG" ]; then
    echo "dlx invoked with no readable NPM_CONFIG_USERCONFIG" >&2
    exit 65
  fi
  cp "$NPM_CONFIG_USERCONFIG" "$CAPTURED_NPMRC"
  cat "$FIXTURE_JSON"
  exit 0
fi
echo "unexpected pnpm invocation: $*" >&2
exit 64
"""

NPX_STUB = """#!/usr/bin/env bash
set -euo pipefail
test "${GH_PACKAGES_READ:-}" = "$EXPECTED_PACKAGE_TOKEN"
test -z "${NARDUK_PLATFORM_GH_PACKAGES_READ:-}"
echo "npx $*" >> "$STUB_LOG"
if [ "$1" = "--no-install" ] && [ "$2" = "narduk-app" ]; then
  cat "$FIXTURE_JSON"
  exit 0
fi
if [ "$1" = "--yes" ]; then
  if [ -z "${NPM_CONFIG_USERCONFIG:-}" ] || [ ! -f "$NPM_CONFIG_USERCONFIG" ]; then
    echo "dlx invoked with no readable NPM_CONFIG_USERCONFIG" >&2
    exit 65
  fi
  cp "$NPM_CONFIG_USERCONFIG" "$CAPTURED_NPMRC"
  cat "$FIXTURE_JSON"
  exit 0
fi
echo "unexpected npx invocation: $*" >&2
exit 64
"""

FIXTURE_ARTEFACT = json.dumps({"schemaVersion": 1, "result": "PASS", "exitCode": 0})

FETCH_STUB = """
globalThis.fetch = async (url, options) => {
  if (url !== 'https://api.nvault.nardukenterprises.com/v1/resolve'
      || options.method !== 'POST' || options.body !== '{}'
      || options.headers.authorization !== 'Bearer test-service-token'
      || options.redirect !== 'error' || !options.signal) {
    throw new Error('Unexpected credential request');
  }
  if (process.env.STUB_RESOLVE === 'denied') return { ok: false, status: 403 };
  if (process.env.STUB_RESOLVE === 'old-alias') return { ok: true, json: async () => ({ data: { secrets: { NARDUK_PLATFORM_GH_PACKAGES_READ: 'old-alias-token' } } }) };
  return { ok: true, json: async () => ({ data: { secrets: {
    GH_PACKAGES_READ: process.env.STUB_RESOLVE === 'malformed'
      ? 'invalid\\ntoken' : 'resolved-test-package-token'
  } } }) };
};
"""


def run_check_step(
    *, has_dep: bool, pm: str, auth_source: str = "package-token",
    resolve: str = "ok", credential: str | None = None,
) -> tuple[int, str, str | None, str]:
    """Execute the "Run..." step; returns (rc, stdout+stderr, produced JSON, stub log)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        pkg: dict[str, object] = {"name": "f"}
        if has_dep:
            pkg["dependencies"] = {"@narduk-enterprises/narduk-app-tools": "0.2.0"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        pnpm = bin_dir / "pnpm"
        pnpm.write_text(PNPM_STUB)
        pnpm.chmod(0o755)
        npx = bin_dir / "npx"
        npx.write_text(NPX_STUB)
        npx.chmod(0o755)

        fixture_json = tmp_path / "fixture.json"
        fixture_json.write_text(FIXTURE_ARTEFACT)
        fetch_stub = tmp_path / "fetch-stub.mjs"
        fetch_stub.write_text(FETCH_STUB)
        captured_npmrc = tmp_path / "captured.npmrc"
        stub_log = tmp_path / "stub.log"
        # A dedicated, otherwise-empty TMPDIR proves the throwaway npmrc
        # (mktemp + `trap ... EXIT`) does not linger after the step exits.
        mktemp_dir = tmp_path / "mktemp-scope"
        mktemp_dir.mkdir()

        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "PM": pm,
            "TOOL_VERSION": "0.2.0",
            "AUTH_SOURCE": auth_source,
            "NARDUK_PLATFORM_GH_PACKAGES_READ": credential if credential is not None else (
                "test-service-token" if auth_source == "nvault" else "test-secret-token"
            ),
            "EXPECTED_PACKAGE_TOKEN": (
                "resolved-test-package-token" if auth_source == "nvault" else "test-secret-token"
            ),
            "STUB_RESOLVE": resolve,
            "NODE_OPTIONS": f"--import={fetch_stub}",
            "FIXTURE_JSON": str(fixture_json),
            "CAPTURED_NPMRC": str(captured_npmrc),
            "STUB_LOG": str(stub_log),
            "TMPDIR": str(mktemp_dir),
        }
        p = subprocess.run(["bash", "-c", RUN_STEP], cwd=tmp, env=env, capture_output=True, text=True)
        produced = None
        out_file = tmp_path / "foundation-check.json"
        if out_file.exists():
            produced = out_file.read_text()
        leftover = list(mktemp_dir.iterdir())
        return p.returncode, p.stdout + p.stderr, produced, str(leftover)


def run_eval_step(*, artefact_content: str | None) -> tuple[int, str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        if artefact_content is not None:
            (tmp_path / "foundation-check.json").write_text(artefact_content)
        summary = tmp_path / "summary"
        summary.touch()
        env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary)}
        p = subprocess.run(["bash", "-c", EVAL_STEP], cwd=tmp, env=env, capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr, summary.read_text()


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("PASS  " if ok else "FAIL  ") + label)
    if not ok and detail:
        print(f"      {detail}")
    return ok


def main() -> int:
    failures = 0
    total = 0

    total += 1
    ok = input_default("foundation-check") is False
    failures += 0 if check("foundation-check input defaults to false (opt-in)", ok) else 1

    total += 1
    ok = input_default("foundation-check-tool-version") == "0.2.0"
    failures += 0 if check("foundation-check-tool-version pins 0.2.0", ok) else 1

    total += 1
    ok = input_default("foundation-check-auth") == "package-token"
    failures += 0 if check("existing package-token callers retain the default auth type", ok) else 1

    # --- "Run web-foundation conformance check" ---

    for pm in ("pnpm", "npm"):
        total += 1
        rc, out, produced, leftover = run_check_step(has_dep=True, pm=pm)
        ok = rc == 0 and produced == FIXTURE_ARTEFACT and leftover == "[]"
        failures += 0 if check(
            f"[{pm}] existing dependency -> runs the installed binary directly",
            ok, f"rc={rc} produced={produced!r} leftover={leftover}\n{out}",
        ) else 1

    for pm in ("pnpm", "npm"):
        total += 1
        rc, out, produced, leftover = run_check_step(has_dep=False, pm=pm)
        ok = rc == 0 and produced == FIXTURE_ARTEFACT and leftover == "[]"
        failures += 0 if check(
            f"[{pm}] no dependency -> pinned dlx, throwaway npmrc cleaned up",
            ok, f"rc={rc} produced={produced!r} leftover={leftover}\n{out}",
        ) else 1

    total += 1
    # Re-run the no-dependency pnpm case and inspect what the dlx path wrote
    # into the throwaway npmrc, to prove the SAME secret the callable already
    # receives is what authenticates the pinned fetch -- no new secret.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        (tmp_path / "package.json").write_text(json.dumps({"name": "f"}))
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        pnpm = bin_dir / "pnpm"
        pnpm.write_text(PNPM_STUB)
        pnpm.chmod(0o755)
        fixture_json = tmp_path / "fixture.json"
        fixture_json.write_text(FIXTURE_ARTEFACT)
        captured_npmrc = tmp_path / "captured.npmrc"
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "PM": "pnpm",
            "TOOL_VERSION": "0.2.0",
            "AUTH_SOURCE": "package-token",
            "NARDUK_PLATFORM_GH_PACKAGES_READ": "test-secret-token",
            "EXPECTED_PACKAGE_TOKEN": "test-secret-token",
            "FIXTURE_JSON": str(fixture_json),
            "CAPTURED_NPMRC": str(captured_npmrc),
            "STUB_LOG": str(tmp_path / "stub.log"),
        }
        subprocess.run(["bash", "-c", RUN_STEP], cwd=tmp, env=env, capture_output=True, text=True)
        npmrc_text = captured_npmrc.read_text() if captured_npmrc.exists() else ""
        ok = (
            "@narduk-enterprises:registry=https://npm.pkg.github.com" in npmrc_text
            and "//npm.pkg.github.com/:_authToken=${GH_PACKAGES_READ}" in npmrc_text
            and "test-secret-token" not in npmrc_text
        )
        failures += 0 if check(
            "dlx uses a child-scoped token reference without persisting its value",
            ok, f"npmrc={npmrc_text!r}",
        ) else 1

    for pm in ("pnpm", "npm"):
        for has_dep in (True, False):
            total += 1
            rc, out, produced, leftover = run_check_step(has_dep=has_dep, pm=pm, auth_source="nvault")
            ok = rc == 0 and produced == FIXTURE_ARTEFACT and leftover == "[]" and "test-service-token" not in out
            failures += 0 if check(
                f"[{pm}] nvault dependency={has_dep}: resolves once, isolates credential, supports registry lookup",
                ok, f"rc={rc} produced={produced!r} leftover={leftover}\n{out}",
            ) else 1

    for label, kwargs in (
        ("nVault denial", {"auth_source": "nvault", "resolve": "denied"}),
        ("malformed resolved token", {"auth_source": "nvault", "resolve": "malformed"}),
        ("missing canonical nVault key", {"auth_source": "nvault", "resolve": "old-alias"}),
        ("missing input credential", {"credential": ""}),
        ("unknown credential type", {"auth_source": "unsupported"}),
    ):
        total += 1
        rc, out, produced, leftover = run_check_step(has_dep=True, pm="pnpm", **kwargs)
        eval_rc, _, _ = run_eval_step(artefact_content=produced)
        ok = rc == 0 and produced == "" and leftover == "[]" and eval_rc == 1
        failures += 0 if check(
            f"{label}: leaves an empty artifact, then required evaluation blocks",
            ok, f"rc={rc} produced={produced!r} eval_rc={eval_rc}\n{out}",
        ) else 1

    total += 1
    # The command failing outright must not fail the step -- `|| true` -- so
    # the (empty/partial) artefact still reaches the evaluation step, which
    # decides UNKNOWN on its own terms rather than the run step deciding it.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        (tmp_path / "package.json").write_text(json.dumps({"name": "f"}))
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        pnpm = bin_dir / "pnpm"
        pnpm.write_text("#!/usr/bin/env bash\nexit 7\n")
        pnpm.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "PM": "pnpm",
            "TOOL_VERSION": "0.2.0",
            "AUTH_SOURCE": "package-token",
            "NARDUK_PLATFORM_GH_PACKAGES_READ": "test-secret-token",
        }
        p = subprocess.run(["bash", "-c", RUN_STEP], cwd=tmp, env=env, capture_output=True, text=True)
        ok = p.returncode == 0
        failures += 0 if check(
            "a failing check command never fails the run step itself (|| true)",
            ok, f"rc={p.returncode}\n{p.stdout}{p.stderr}",
        ) else 1

    # --- "Evaluate web-foundation conformance check" ---

    for result, want_rc in (("PASS", 0), ("FAIL", 1), ("UNKNOWN", 1)):
        total += 1
        rc, out, summary = run_eval_step(artefact_content=json.dumps({"result": result}))
        ok = rc == want_rc
        failures += 0 if check(
            f'eval: result="{result}" -> exit {want_rc}',
            ok, f"rc={rc} summary={summary!r}\n{out}",
        ) else 1

    total += 1
    rc, out, summary = run_eval_step(artefact_content=None)
    ok = rc == 1 and "UNKNOWN" in summary
    failures += 0 if check("eval: missing artefact -> UNKNOWN (blocks)", ok, f"rc={rc} summary={summary!r}") else 1

    total += 1
    rc, out, summary = run_eval_step(artefact_content="")
    ok = rc == 1 and "UNKNOWN" in summary
    failures += 0 if check("eval: empty artefact -> UNKNOWN (blocks)", ok, f"rc={rc} summary={summary!r}") else 1

    total += 1
    rc, out, summary = run_eval_step(artefact_content="{not json")
    ok = rc == 1 and "UNKNOWN" in summary
    failures += 0 if check("eval: malformed JSON -> UNKNOWN (blocks)", ok, f"rc={rc} summary={summary!r}") else 1

    total += 1
    rc, out, summary = run_eval_step(artefact_content=json.dumps({"result": "WARNING"}))
    ok = rc == 1 and "UNKNOWN" in summary
    failures += 0 if check(
        "eval: no warning tier -- an unrecognized result blocks, never passes",
        ok, f"rc={rc} summary={summary!r}",
    ) else 1

    print(f"\ntest_foundation_check: {total} case(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
