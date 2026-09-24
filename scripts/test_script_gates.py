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

COVERS BOTH LIVE NODE CALLABLES. `node-library.yml` got the mechanism in
workflows#14; `nuxt-cloudflare.yml` followed in workflows#16. The files differ
in shape (node-library has a workspace `FILTER`, nuxt-cloudflare lets a caller
pass an EMPTY script name to declare a lane absent), so each gets the cases
its own contract promises. The legacy `reusable-node-ci.yml` retained eight
fail-open paths (four gates times pnpm and npm) until D-8; it had zero live
callers estate-wide (re-verified via `gh search code`/`gh api search/code`
across narduk-enterprises, narduk-incubator and narduk-enterprises-clients,
2026-09-24) and was deleted in narduk-reboot P3-C2 / O-D8, so its gate cases
were deleted with it rather than kept against a file that no longer exists.

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
    (NUXT_CF, "checks", "Typecheck Worker", "typecheck"),
    (NUXT_CF, "checks", "Typecheck Nuxt", "web:typecheck"),
    (NUXT_CF, "checks", "Unit tests", "test"),
    (NUXT_CF, "build", "Build", "build"),
]


def gate_script(workflow: pathlib.Path, job: str, name: str) -> str:
    doc = yaml.safe_load(workflow.read_text())
    for step in doc["jobs"][job]["steps"]:
        if step.get("name") == name:
            return step["run"]
    raise SystemExit(f"::error::no step named {name!r} in {workflow} job {job!r}")


def input_default(workflow: pathlib.Path, name: str) -> object:
    doc = yaml.safe_load(workflow.read_text())
    trigger = doc.get("on", doc.get(True))
    return trigger["workflow_call"]["inputs"][name]["default"]


def run(script: str, fixture: dict, *, env_extra: dict[str, str]) -> tuple[int, str, str]:
    """Execute the shipped gate text against a real package.json fixture."""
    with tempfile.TemporaryDirectory() as tmp:
        pathlib.Path(tmp, "package.json").write_text(json.dumps({"name": "f", "scripts": fixture}))
        summary = pathlib.Path(tmp, "summary")
        summary.touch()
        # The repository's own CI does not provision pnpm because it has no
        # Node project to install. Give behavior tests a deterministic shim
        # that exercises the shipped `pnpm run <script>` branch while using
        # npm only as the local package-script executor. Any other pnpm shape
        # fails, so the shim cannot accidentally bless a malformed command.
        bin_dir = pathlib.Path(tmp, "bin")
        bin_dir.mkdir()
        pnpm = bin_dir / "pnpm"
        pnpm.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'if [ "$#" -lt 2 ] || [ "$1" != "run" ]; then\n'
            '  echo "unexpected pnpm invocation: $*" >&2\n'
            "  exit 64\n"
            "fi\n"
            "shift\n"
            'exec npm run "$@"\n'
        )
        pnpm.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
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

    total += 1
    ok = input_default(NUXT_CF, "typecheck-web-script") == ""
    print(("PASS  " if ok else "FAIL  ")
          + "Nuxt web typecheck is an honest opt-in, not a missing default")
    if not ok:
        failures += 1

    for workflow in (NODE_LIB, NUXT_CF):
        total += 1
        ok = input_default(workflow, "require-scripts") is True
        print(("PASS  " if ok else "FAIL  ")
              + f"{workflow.name} require-scripts defaults fail-closed")
        if not ok:
            failures += 1

    # ---- node-library.yml: standard lanes and extra scripts ---------------
    for workflow, job, gate, script_name in NODE_LIB_GATES:
        script = gate_script(workflow, job, gate)
        cases = [
            ("missing + require=false -> warns but PASSES (explicit opt-out)",
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

    node_extra = gate_script(NODE_LIB, "package", "Extra scripts")
    node_extra_cases = [
        ("every entry present -> each one actually runs",
         {"a": "echo RAN_A", "b": "echo RAN_B"}, "a b", "true", 0, "RAN_B", False),
        ("one entry missing + require=false -> warns, others still run",
         {"a": "echo RAN_A"}, "nope a", "false", 0, "RAN_A", True),
        ("one entry missing + require=true  -> FAILS loudly",
         {"a": "echo RAN_A"}, "a nope", "true", 1, "::error::", True),
    ]
    for label, fixture, scripts, require, want_rc, want_sub, want_sum in node_extra_cases:
        rc, out, summary = run(
            node_extra,
            fixture,
            env_extra={
                "GATE": "Extra scripts",
                "LANE_SCRIPTS_JSON": "null",
                "LEGACY_SCRIPTS": scripts,
                "REQUIRE": require,
            },
        )
        total += 1
        if not check(
            label,
            "Node extra scripts",
            rc,
            out,
            summary,
            want_rc,
            want_sub,
            want_summary_warning=want_sum,
        ):
            failures += 1

    # A matrix entry's field wins even when it is empty. Run the exact shipped
    # step twice with a shared fallback that names lane A's gate: lane A owns
    # and runs it; lane B explicitly owns no extras and must neither inherit,
    # warn about, nor fail on A's requirement.
    rc_a, out_a, summary_a = run(
        node_extra,
        {"lane:a": "echo RAN_LANE_A"},
        env_extra={
            "GATE": "Extra scripts",
            "LANE_SCRIPTS_JSON": json.dumps("lane:a"),
            "LEGACY_SCRIPTS": "lane:a",
            "REQUIRE": "true",
        },
    )
    rc_b, out_b, summary_b = run(
        node_extra,
        {},
        env_extra={
            "GATE": "Extra scripts",
            "LANE_SCRIPTS_JSON": json.dumps(""),
            "LEGACY_SCRIPTS": "lane:a",
            "REQUIRE": "true",
        },
    )
    total += 1
    ok = (
        rc_a == 0
        and "RAN_LANE_A" in out_a
        and not summary_a
        and rc_b == 0
        and "declared no extra scripts" in out_b
        and "::warning::" not in out_b
        and not summary_b
    )
    print(("PASS  " if ok else "FAIL  ")
          + "Node extra scripts lane A requirement cannot leak into lane B")
    if not ok:
        failures += 1
        print(
            f"      lane A rc={rc_a}, summary={summary_a!r}, out={out_a[:300]!r}\n"
            f"      lane B rc={rc_b}, summary={summary_b!r}, out={out_b[:300]!r}"
        )

    # ---- nuxt-cloudflare.yml: same contract, plus the empty-name opt-out --
    for workflow, job, gate, script_name in NUXT_CF_GATES:
        script = gate_script(workflow, job, gate)
        cases = [
            ("missing + require=false -> warns but PASSES (explicit opt-out)",
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

    # run-e2e=true is not an optional package-script lane. The caller spent an
    # isolated browser slot and asserted the suite exists, so neither an empty
    # input nor a missing script may inherit require-scripts' warn-and-pass
    # migration behavior.
    e2e_cases = [
        ("missing + require=false -> FAILS closed",
         {"other": "echo x"}, "test:e2e", "false", 1, "::error::", False),
        ("missing + require=true -> FAILS closed",
         {"other": "echo x"}, "test:e2e", "true", 1, "::error::", False),
        ("empty e2e-script -> FAILS closed",
         {"test:e2e": "echo RAN"}, "", "false", 1, "::error::", False),
        ("present -> actually runs the suite",
         {"test:e2e": "echo RAN"}, "test:e2e", "false", 0, "RAN", False),
    ]
    for label, fixture, name, require, want_rc, want_sub, want_sum in e2e_cases:
        rc, out, summary = run(
            e2e,
            fixture,
            env_extra={
                "GATE": "Run e2e suite",
                "SCRIPT": name,
                "REQUIRE": require,
            },
        )
        total += 1
        if not check(
            label,
            "Run e2e suite",
            rc,
            out,
            summary,
            want_rc,
            want_sub,
            want_summary_warning=want_sum,
        ):
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
