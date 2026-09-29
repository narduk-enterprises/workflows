#!/usr/bin/env python3
"""Behaviour tests for `nuxt-cloudflare.yml`'s `quality-level` gates.

Same discipline as `test_foundation_check.py`: nothing here tests a copy of
the logic. The shipped `run:` blocks ("Resolve quality gates", "Performance
budget", and the two security-headers probes) are extracted from the workflow
YAML and executed under bash against stubbed `pnpm` / `npx` binaries, so an
edit to the callable either still passes these tests or fails them.

What is pinned:
  * `legacy` is the default and resolves every new gate off, so a pin bump
    changes nothing for an app that did not write `quality-level: standard`;
  * an opt-out needs a reason, names a known check, and is echoed with it;
  * the three jobs run the identical resolver text;
  * the performance budget fails closed on a missing build or report;
  * the security-headers probe propagates FAIL and UNKNOWN as failures.

Run: python3 scripts/test_quality_gates.py
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

import yaml

NUXT_CF = pathlib.Path(".github/workflows/nuxt-cloudflare.yml")
DOC = yaml.safe_load(NUXT_CF.read_text())
TRIGGER = DOC.get("on", DOC.get(True))
INPUTS = TRIGGER["workflow_call"]["inputs"]


def step(job: str, name: str) -> dict:
    for candidate in DOC["jobs"][job]["steps"]:
        if candidate.get("name") == name:
            return candidate
    raise SystemExit(f"::error::no step named {name!r} in {NUXT_CF} job {job!r}")


RESOLVER = step("build", "Resolve quality gates")
PERF = step("build", "Performance budget")
PREVIEW_PROBE = step("preview", "Check preview security headers")
SMOKE_PROBE = step("journey-smoke", "Check post-deploy security headers")

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def run_bash(script: str, env: dict[str, str], cwd: str) -> subprocess.CompletedProcess:
    full_env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
    full_env.update(env)
    return subprocess.run(
        ["bash", "-c", script], cwd=cwd, env=full_env, capture_output=True, text=True, timeout=60
    )


def resolve(**overrides: str) -> tuple[subprocess.CompletedProcess, dict[str, str], str]:
    env = {
        "CONTEXT": "build",
        "LEVEL": "legacy",
        "OPT_OUT": "",
        "EXPLICIT_FOUNDATION": "false",
        "PREVIEW_CHECKS": "og",
        "BUILD_SCRIPT": "build",
        "PERF_ARGS": "",
        "HEADER_PATHS": "",
        "HEADER_URL": "",
    }
    env.update(overrides)
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp, "out")
        summary = pathlib.Path(tmp, "summary")
        out.touch()
        summary.touch()
        # A file a glob in a reason would expand to, if the resolver ever
        # word-split the reason unquoted.
        pathlib.Path(tmp, "GLOBBED-FILE").touch()
        env["GITHUB_OUTPUT"] = str(out)
        env["GITHUB_STEP_SUMMARY"] = str(summary)
        result = run_bash(RESOLVER["run"], env, tmp)
        outputs = dict(
            line.split("=", 1) for line in out.read_text().splitlines() if "=" in line
        )
        return result, outputs, summary.read_text()


def gates(outputs: dict[str, str]) -> tuple[str, str, str]:
    return (
        outputs.get("foundation-check", ""),
        outputs.get("performance-budget", ""),
        outputs.get("security-headers", ""),
    )


# --- interface ---------------------------------------------------------------
check(INPUTS["quality-level"]["default"] == "legacy", "quality-level must default to legacy")
for name in ("quality-opt-out", "performance-budget-args", "security-headers-paths", "security-headers-url"):
    check(INPUTS[name]["default"] == "", f"{name} must default to empty")
    check(INPUTS[name]["required"] is False, f"{name} must be optional")
check(INPUTS["foundation-check"]["default"] is False, "foundation-check must stay default false")

for job in ("preview", "journey-smoke"):
    other = step(job, "Resolve quality gates")
    check(other["run"] == RESOLVER["run"], f"{job} must run the anchored resolver text")
    check(other["env"]["CONTEXT"] == job, f"{job} resolver must set CONTEXT={job}")
    check(
        {k: v for k, v in other["env"].items() if k != "CONTEXT"}
        == {k: v for k, v in RESOLVER["env"].items() if k != "CONTEXT"},
        f"{job} resolver env must match the Build job's",
    )
check(RESOLVER["env"]["CONTEXT"] == "build", "Build resolver must set CONTEXT=build")
check(PREVIEW_PROBE["run"] == SMOKE_PROBE["run"], "both security-headers probes must run the anchored text")

conditions = {
    ("build", "Run web-foundation conformance check"): "steps.quality.outputs.foundation-check == 'true' && inputs.mode != 'e2e'",
    ("build", "Evaluate web-foundation conformance check"): "always() && steps.quality.outputs.foundation-check == 'true' && inputs.mode != 'e2e'",
    ("build", "Performance budget"): "steps.quality.outputs.performance-budget == 'true' && inputs.mode != 'e2e'",
}
for (job, name), expected in conditions.items():
    check(str(step(job, name).get("if", "")).strip() == expected, f"{job}/{name} condition drifted")
for probe in (PREVIEW_PROBE, SMOKE_PROBE):
    check("steps.quality.outputs.security-headers == 'true'" in probe["if"], "probe must be gated on the resolver")
check("steps.preview.outputs.url != ''" in PREVIEW_PROBE["if"], "preview probe must need a resolved preview URL")
check(
    "inputs.security-headers-url != '' && inputs.security-headers-url || inputs.journey-smoke-url"
    in SMOKE_PROBE["env"]["TARGET_URL"],
    "post-deploy probe must fall back to journey-smoke-url",
)
comment = step("preview", "Post or update the sticky preview comment")
check(comment["env"].get("HEADERS_OUTCOME") == "${{ steps.security-headers.outcome }}", "sticky comment must report the header probe")

e2e_key = step("e2e-plan", "Find equivalent successful PR E2E proof")["with"]["script"]
for fragment in ("'quality-level': 'legacy'", "'quality-opt-out': ''", "'performance-budget-args': ''",
                 "'security-headers-paths': ''", "'security-headers-url': ''"):
    check(fragment in e2e_key, f"E2E proof key must drop {fragment} at its default")

# --- resolver ----------------------------------------------------------------
r, o, _ = resolve()
check(r.returncode == 0 and gates(o) == ("false", "false", "false"), f"legacy default must resolve all off: {o} {r.stderr}")

r, o, _ = resolve(EXPLICIT_FOUNDATION="true")
check(r.returncode == 0 and gates(o) == ("true", "false", "false"), f"explicit foundation-check must still work at legacy: {o}")

r, o, _ = resolve(LEVEL="standard")
check(r.returncode == 0 and gates(o) == ("true", "true", "true"), f"standard must resolve all on: {o} {r.stdout}")

r, o, summary = resolve(LEVEL="standard", OPT_OUT="performance-budget=static docs site with no Nuxt build * yet")
check(r.returncode == 0 and gates(o) == ("true", "false", "true"), f"opt-out must turn only that gate off: {o}")
check(
    "::warning::quality gate 'performance-budget' is opted out at quality-level standard: static docs site with no Nuxt build * yet"
    in r.stdout,
    f"opt-out must be echoed verbatim with its reason (no glob expansion): {r.stdout}",
)
check("GLOBBED-FILE" not in r.stdout + summary, "a reason must never be glob-expanded")
check("performance-budget opted out: static docs site" in summary, f"opt-out must reach the job summary: {summary}")

r, o, _ = resolve(LEVEL="standard", OPT_OUT=" foundation-check = mid-migration on item 5 , security-headers=no preview yet ")
check(r.returncode == 0 and gates(o) == ("false", "true", "false"), f"two padded opt-outs must both apply: {o} {r.stdout}")

for bad, why in (
    ("performance-budget", "no reason"),
    ("performance-budget=", "empty reason"),
    ("performance-budget=   ", "blank reason"),
    ("lint=because", "unknown check"),
    ("security-headers=a,security-headers=b", "duplicate"),
    ("performance-budget=first, second", "a comma inside the reason"),
):
    r, _, _ = resolve(LEVEL="standard", OPT_OUT=bad)
    check(r.returncode != 0, f"opt-out with {why} ({bad!r}) must fail")

r, o, _ = resolve(LEVEL="standard", OPT_OUT="security-headers=one\ntwo")
check(r.returncode != 0, "a multi-line opt-out must fail")

r, o, _ = resolve(OPT_OUT="performance-budget=not needed")
check(r.returncode == 0 and gates(o) == ("false", "false", "false"), "an opt-out at legacy must be a no-op")
check("has no effect at quality-level legacy" in r.stdout, "an opt-out at legacy must warn it does nothing")

r, _, _ = resolve(LEVEL="standard", EXPLICIT_FOUNDATION="true", OPT_OUT="foundation-check=nope")
check(r.returncode != 0, "explicit foundation-check plus its opt-out must fail as a contradiction")

r, _, _ = resolve(LEVEL="strict")
check(r.returncode != 0, "an unknown quality-level must fail")

r, _, _ = resolve(LEVEL="standard", PREVIEW_CHECKS="none")
check(r.returncode != 0 and "preview-checks is 'none'" in r.stdout, "standard with no preview must fail in Build")
r, o, _ = resolve(LEVEL="standard", PREVIEW_CHECKS="none", OPT_OUT="security-headers=no Workers Builds preview")
check(r.returncode == 0 and o.get("security-headers") == "false", "the header opt-out must clear the no-preview error")
r, _, _ = resolve(LEVEL="standard", PREVIEW_CHECKS="none", CONTEXT="journey-smoke")
check(r.returncode == 0, "the preview consistency rule belongs to the Build job only")

r, _, _ = resolve(LEVEL="standard", BUILD_SCRIPT="")
check(r.returncode != 0 and "build-script is empty" in r.stdout, "standard with no build must fail")

for args in ("--report-only", "--app-dir apps/web --json", "--json=out.json"):
    r, _, _ = resolve(LEVEL="standard", PERF_ARGS=args)
    check(r.returncode != 0, f"performance-budget-args {args!r} must be rejected")
r, _, _ = resolve(LEVEL="standard", PERF_ARGS="--app-dir apps/web --css-budget-kb 50")
check(r.returncode == 0, "ordinary performance-budget-args must pass")

r, _, _ = resolve(HEADER_PATHS="/ /login")
check(r.returncode == 0, "absolute header paths must pass")
r, _, _ = resolve(HEADER_PATHS="login")
check(r.returncode != 0, "a relative header path must fail")
for escape in ("//evil.example", "/\\evil.example", "/ //evil.example/login"):
    r, _, _ = resolve(HEADER_PATHS=escape)
    check(r.returncode != 0, f"header path {escape!r} resolves to another origin and must fail")
r, _, _ = resolve(HEADER_URL="http://example.com")
check(r.returncode != 0, "a non-https security-headers-url must fail")
r, _, _ = resolve(HEADER_URL="https://example.com")
check(r.returncode == 0, "an https security-headers-url must pass")


# --- stubs -------------------------------------------------------------------
def write_stub(bin_dir: pathlib.Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


TOOL_STUB = """echo "$0 $*" >> "$STUB_LOG"
if [ -n "${STUB_STDOUT_FILE:-}" ]; then cat "$STUB_STDOUT_FILE"; fi
exit "${STUB_EXIT:-0}"
"""


NODE = shutil.which("node")
if NODE is None:
    sys.exit("test_quality_gates: node is required on PATH")


def run_step(
    body: dict, pm: str, extra_env: dict[str, str], stdout: str | None, exit_code: int, make_output: bool,
    expect_invoked: bool = True,
) -> tuple[subprocess.CompletedProcess, str]:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name in ("pnpm", "npx"):
            write_stub(bin_dir, name, TOOL_STUB)
        node_dir = root / "node-bin"
        node_dir.mkdir()
        (node_dir / "node").symlink_to(NODE)
        app = root / "app"
        app.mkdir()
        if make_output:
            (app / ".output" / "public").mkdir(parents=True)
        log = root / "log"
        log.touch()
        env = {
            # Only the stubs and node: a real pnpm/npx on the host PATH must
            # never answer in place of a stub (it made negative checks vacuous).
            "PATH": f"{bin_dir}:{node_dir}:/usr/bin:/bin",
            "PM": pm,
            "STUB_LOG": str(log),
            "STUB_EXIT": str(exit_code),
            "RUNNER_TEMP": str(root),
            "GITHUB_STEP_SUMMARY": str(root / "summary"),
        }
        if stdout is not None:
            out = root / "stdout.json"
            out.write_text(stdout.replace("@APP@", str(app)))
            env["STUB_STDOUT_FILE"] = str(out)
        env.update(extra_env)
        result = run_bash(body["run"], env, tmp)
        text = log.read_text()
        if expect_invoked and not text:
            check(False, f"stub not invoked (the step never reached the tool): {result.stdout} {result.stderr}")
        return result, text


def report(violations: list[dict] | None = None) -> str:
    return json.dumps({
        "appDir": "@APP@",
        "budgets": {},
        "checked": {"cssFiles": 2, "fontFiles": 1, "imageFiles": 3},
        "violations": violations or [],
        "warnings": ["hero image is lazy"],
    })


r, log = run_step(PERF, "pnpm", {"PERF_ARGS": "--app-dir apps/web"}, report(), 0, True)
check(r.returncode == 0, f"a clean budget must pass: {r.stdout} {r.stderr}")
check("pnpm exec narduk-app performance-budget --json --app-dir apps/web" in log, f"budget args must pass through: {log}")
check("::warning::performance-budget: hero image is lazy" in r.stdout, "tool warnings must surface")

r, log = run_step(PERF, "npm", {"PERF_ARGS": ""}, report(), 0, True)
check(r.returncode == 0 and "npx --no-install narduk-app performance-budget --json" in log, f"npm callers must use npx: {log}")

r, _ = run_step(PERF, "pnpm", {"PERF_ARGS": ""},
                report([{"path": "_nuxt/app.css", "message": "CSS is 40 KiB, budget is 35 KiB."}]), 1, True)
check(r.returncode != 0 and "_nuxt/app.css: CSS is 40 KiB" in r.stdout, f"a violation must fail and name the file: {r.stdout}")

r, _ = run_step(PERF, "pnpm", {"PERF_ARGS": ""}, report(), 0, False, expect_invoked=False)
check(r.returncode != 0 and "no build output" in r.stdout, "a missing .output/public must fail closed")

r, _ = run_step(PERF, "pnpm", {"PERF_ARGS": ""}, "not json", 0, True)
check(r.returncode != 0 and "no readable report" in r.stdout, "an unreadable report must fail closed")

r, _ = run_step(PERF, "pnpm", {"PERF_ARGS": ""}, None, 127, True)
check(r.returncode != 0, "a missing binary must fail closed")

r, _ = run_step(PERF, "pnpm", {"PERF_ARGS": ""}, report(), 3, True)
check(r.returncode != 0 and "exited 3" in r.stdout, "a non-zero exit with no violation must fail")

for pm, prefix in (("pnpm", "pnpm exec"), ("npm", "npx --no-install")):
    r, log = run_step(PREVIEW_PROBE, pm,
                      {"TARGET_URL": "https://pr-1-app.example.workers.dev", "HEADER_PATHS": "/login /app"}, None, 0, False)
    check(r.returncode == 0, f"{pm} probe PASS must pass: {r.stderr}")
    check(
        f"{prefix} narduk-app foundation:check:security-headers --base-url https://pr-1-app.example.workers.dev --path /login --path /app"
        in log,
        f"{pm} probe must pass the URL and each path: {log}",
    )
r, log = run_step(PREVIEW_PROBE, "pnpm", {"TARGET_URL": "https://x.example", "HEADER_PATHS": ""}, None, 0, False)
check("--base-url https://x.example\n" in log, f"no paths must probe the bare origin: {log!r}")
for code, verdict in ((1, "FAIL"), (2, "UNKNOWN")):
    r, _ = run_step(PREVIEW_PROBE, "pnpm", {"TARGET_URL": "https://x.example", "HEADER_PATHS": ""}, None, code, False)
    check(r.returncode != 0, f"probe {verdict} (exit {code}) must fail the step")

if failures:
    for message in failures:
        print(f"::error::{message}")
    raise SystemExit(1)
print("test_quality_gates: all checks passed")
