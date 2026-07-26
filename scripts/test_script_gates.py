#!/usr/bin/env python3
"""Behaviour tests for the Node callables' script gates and `require-scripts`.

Like `test_extra_env.py`, this does NOT test a copy: it extracts each gate's
`run:` block out of the workflow YAML and executes that exact text under bash
against real `package.json` fixtures. If someone edits a gate, these tests
either still pass against the new text or they fail.

The defect being locked down: every gate used to run `--if-present`, so a gate
whose script does not exist matched nothing, exited 0, and reported a GREEN
lane that ran no tests at all. `run-tests: true` is a caller asserting tests
exist. narduk-libs hit this — its packages define `test:unit`, not `test`.

COVERS BOTH NODE CALLABLES. `node-library.yml` got the mechanism in
workflows#14; `nuxt-cloudflare.yml` did not, and was the worse case — 4 of its
5 adopters were running at least one dead lane, because its `web:typecheck`
and `build` defaults are absent in most of the class. The two files differ in
shape (node-library has a workspace `FILTER`, nuxt-cloudflare has none and
instead lets a caller pass an EMPTY script name to declare a lane absent), so
each gets the cases its own contract promises.

Run: python3 scripts/test_script_gates.py
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile

import yaml

NODE_LIB = pathlib.Path(".github/workflows/node-library.yml")
NUXT_CF = pathlib.Path(".github/workflows/nuxt-cloudflare.yml")

# (workflow, job id, step name, script name the gate looks for)
NODE_LIB_GATES = [
    (NODE_LIB, "package", "Lint", "lint"),
    (NODE_LIB, "package", "Typecheck", "typecheck"),
    (NODE_LIB, "package", "Test", "test"),
    (NODE_LIB, "package", "Build", "build"),
]

# nuxt-cloudflare's defaults are deliberately included verbatim — `web:typecheck`
# and `test:e2e` carry a COLON, and the probe resolves the script through
# `npm pkg get scripts.<name>`, a dot-path. A name that broke that lookup would
# report every colon-bearing script as missing, which under `require-scripts`
# is a hard failure for most of the class.
NUXT_CF_GATES = [
    (NUXT_CF, "build", "Typecheck Worker", "typecheck"),
    (NUXT_CF, "build", "Typecheck Nuxt", "web:typecheck"),
    (NUXT_CF, "build", "Unit tests", "test"),
    (NUXT_CF, "build", "Build", "build"),
    (NUXT_CF, "e2e", "Run e2e suite", "test:e2e"),
]


def gate_script(workflow: pathlib.Path, job: str, name: str) -> str:
    doc = yaml.safe_load(workflow.read_text())
    for step in doc["jobs"][job]["steps"]:
        if step.get("name") == name:
            return step["run"]
    raise SystemExit(f"::error::no step named {name!r} in {workflow} job {job!r}")


def run(script: str, fixture: dict, *, env_extra: dict[str, str]) -> tuple[int, str, str]:
    """Execute the shipped gate text against a real package.json fixture."""
    with tempfile.TemporaryDirectory() as tmp:
        pathlib.Path(tmp, "package.json").write_text(json.dumps({"name": "f", "scripts": fixture}))
        summary = pathlib.Path(tmp, "summary")
        summary.touch()
        env = {
            **os.environ,
            "FILTER": "",
            "PM": "npm",
            # e2e-only knobs; harmless for the other gates and required by the
            # e2e step's own text (a single unsharded lane, no extra args).
            "SHARD": "1",
            "TOTAL": "1",
            "EXTRA_ARGS": "",
            "GITHUB_STEP_SUMMARY": str(summary),
            **env_extra,
        }
        p = subprocess.run(["bash", "-c", script], cwd=tmp, env=env, capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr, summary.read_text()


def check(label: str, gate: str, rc: int, out: str, summary: str,
          want_rc: int, want_sub: str, *, want_summary_warning: bool | None = None) -> bool:
    ok = rc == want_rc and want_sub in out
    if ok and want_summary_warning is not None:
        ok = (":warning:" in summary) == want_summary_warning
    print(("PASS  " if ok else "FAIL  ") + f"{gate:18s} {label}")
    if not ok:
        print(f"      rc={rc} (want {want_rc}); summary={summary.strip()!r}\n{out.strip()[:600]}")
    return ok


def main() -> int:
    failures = 0
    total = 0

    # workflows#15: pnpm forwards a literal `--` into wrapper scripts while
    # npm strips it. The shipped pnpm e2e branch must omit the separator.
    e2e = gate_script(NUXT_CF, "e2e", "Run e2e suite")
    pnpm_start = e2e.index('if [ "$PM" = "pnpm" ]; then')
    npm_start = e2e.index("else", pnpm_start)
    pnpm_branch = e2e[pnpm_start:npm_start]
    npm_branch = e2e[npm_start:]
    total += 1
    ok = 'pnpm run "$SCRIPT" --' not in pnpm_branch and 'npm run "$SCRIPT" --' in npm_branch
    print(("PASS  " if ok else "FAIL  ") + "pnpm e2e args omit literal separator")
    if not ok:
        failures += 1

    # ---- node-library.yml: the original contract, unchanged --------------
    for workflow, job, gate, script_name in NODE_LIB_GATES:
        script = gate_script(workflow, job, gate)
        cases = [
            ("missing + require=false -> warns but PASSES (today's default)",
             {"other": "echo x"}, "false", 0, "::warning::", True),
            ("missing + require=true  -> FAILS loudly",
             {"other": "echo x"}, "true", 1, "::error::", True),
            ("present + require=false -> actually runs the script",
             {script_name: "echo RAN"}, "false", 0, "RAN", False),
            ("present + require=true  -> actually runs the script",
             {script_name: "echo RAN"}, "true", 0, "RAN", False),
            ("declared but EMPTY -> counts as missing, not as a script",
             {script_name: ""}, "true", 1, "::error::", True),
        ]
        for label, fixture, require, want_rc, want_sub, want_sum in cases:
            rc, out, summary = run(script, fixture,
                                   env_extra={"SCRIPT": script_name, "REQUIRE": require})
            total += 1
            if not check(label, gate, rc, out, summary, want_rc, want_sub,
                         want_summary_warning=want_sum):
                failures += 1

    # ---- nuxt-cloudflare.yml: same contract, plus the empty-name opt-out --
    for workflow, job, gate, script_name in NUXT_CF_GATES:
        script = gate_script(workflow, job, gate)
        cases = [
            ("missing + require=false -> warns but PASSES (today's default)",
             {"other": "echo x"}, script_name, "false", 0, "::warning::", True),
            ("missing + require=true  -> FAILS loudly",
             {"other": "echo x"}, script_name, "true", 1, "::error::", True),
            ("present + require=false -> actually runs the script",
             {script_name: "echo RAN"}, script_name, "false", 0, "RAN", False),
            ("present + require=true  -> actually runs the script",
             {script_name: "echo RAN"}, script_name, "true", 0, "RAN", False),
            ("declared but EMPTY value -> counts as missing, not as a script",
             {script_name: ""}, script_name, "true", 1, "::error::", True),
            # The opt-out that makes `require-scripts` adoptable at all: a repo
            # with no web surface and no build script says so, and must not be
            # warned at, summarised, or failed for a lane it declared absent.
            ("EMPTY NAME + require=true -> lane declared absent, silent skip",
             {"other": "echo x"}, "", "true", 0, "declared this lane absent", False),
        ]
        for label, fixture, name, require, want_rc, want_sub, want_sum in cases:
            rc, out, summary = run(script, fixture,
                                   env_extra={"GATE": gate, "SCRIPT": name, "REQUIRE": require})
            total += 1
            if not check(label, gate, rc, out, summary, want_rc, want_sub,
                         want_summary_warning=want_sum):
                failures += 1

    # ---- nuxt-cloudflare.yml's Extra scripts loop -------------------------
    extra = gate_script(NUXT_CF, "build", "Extra scripts")
    extra_cases = [
        ("every entry present -> each one actually runs",
         {"a": "echo RAN_A", "b": "echo RAN_B"}, "a b", "true", 0, "RAN_B", False),
        ("one entry missing + require=false -> warns, others still run",
         {"a": "echo RAN_A"}, "a nope", "false", 0, "::warning::", True),
        ("one entry missing + require=true  -> FAILS loudly",
         {"a": "echo RAN_A"}, "a nope", "true", 1, "::error::", True),
    ]
    for label, fixture, scripts, require, want_rc, want_sub, want_sum in extra_cases:
        rc, out, summary = run(extra, fixture,
                               env_extra={"GATE": "Extra scripts", "SCRIPTS": scripts,
                                          "REQUIRE": require})
        total += 1
        if not check(label, "Extra scripts", rc, out, summary, want_rc, want_sub,
                     want_summary_warning=want_sum):
            failures += 1
    # The "others still run" half of the warn case deserves its own assertion
    # rather than riding on a substring of the same output.
    rc, out, _ = run(extra, {"a": "echo RAN_A"},
                     env_extra={"GATE": "Extra scripts", "SCRIPTS": "nope a",
                                "REQUIRE": "false"})
    total += 1
    ok = rc == 0 and "RAN_A" in out
    print(("PASS  " if ok else "FAIL  ")
          + f"{'Extra scripts':18s} a missing entry does not skip the entries after it")
    if not ok:
        failures += 1
        print(f"      rc={rc}\n{out.strip()[:600]}")

    print(f"\ntest_script_gates: {total} case(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
