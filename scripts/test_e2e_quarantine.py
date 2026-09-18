#!/usr/bin/env python3
"""The non-blocking E2E quarantine lane (`e2e-quarantine-args`).

A caller tags flaky tests `@quarantine` and removes them from its gating
Playwright projects. This lane keeps running them, so a test builds the pass
history it needs to leave quarantine (buoys#210: "10 consecutive" green runs).
Its whole point is that it can NEVER fail the gate. This test pins that down
in the workflow's structure and in the linter:

- the job is `continue-on-error: true`, `Required` does not list it, and no
  job `needs:` it;
- it runs `e2e`'s exact steps (a YAML alias), so it cannot drift from the
  gate's fail-closed Playwright setup;
- the quarantine leg runs ONLY the caller's quarantine arguments, unsharded,
  and the gate's shards are unchanged;
- its artifact name stays outside `E2E report`'s `playwright-evidence-*`
  merge pattern;
- `lint_callables.py` accepts exactly this shape and rejects each way of
  weakening it (the NON_GATING_JOBS exemption is fail-capable).

Run: python3 scripts/test_e2e_quarantine.py
"""

from __future__ import annotations

import copy
import fnmatch
import os
import pathlib
import stat
import subprocess
import sys
import tempfile

import yaml

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lint_callables as lint  # noqa: E402

PATH = pathlib.Path(".github/workflows/nuxt-cloudflare.yml")


def load() -> dict:
    return yaml.safe_load(PATH.read_text())


def named_step(job: dict, name: str) -> dict:
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    raise SystemExit(f"FAIL  no step named {name!r}")


def check_input() -> None:
    doc = load()
    inp = doc[True]["workflow_call"]["inputs"]["e2e-quarantine-args"]
    assert inp["required"] is False and inp["type"] == "string" and inp["default"] == ""
    print("PASS  e2e-quarantine-args declared, optional, default empty")


def check_job_shape() -> None:
    doc = load()
    jobs = doc["jobs"]
    q, e2e = jobs["e2e-quarantine"], jobs["e2e"]
    assert q["continue-on-error"] is True
    assert "inputs.e2e-quarantine-args != ''" in q["if"], q["if"]
    assert "inputs.run-e2e" in q["if"], q["if"]
    assert "needs.e2e-plan.outputs.skipped != 'true'" in q["if"], q["if"]
    assert q["strategy"]["matrix"]["shard"] == ["quarantine"]
    assert q["steps"] == e2e["steps"], "quarantine lane must run e2e's exact steps"
    assert q["runs-on"] == e2e["runs-on"]
    assert q["permissions"] == e2e["permissions"]
    for jid, job in jobs.items():
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        assert "e2e-quarantine" not in needs, f"{jid} needs e2e-quarantine"
    print("PASS  e2e-quarantine: continue-on-error, never needed, e2e's exact steps and route")


def check_artifact_name() -> None:
    doc = load()
    step = named_step(doc["jobs"]["e2e"], "Upload Playwright evidence")
    expr = step["with"]["name"]
    assert "'playwright-quarantine'" in expr, expr
    assert not fnmatch.fnmatch("playwright-quarantine", "playwright-evidence-*")
    report = doc["jobs"]["e2e-report"]
    patterns = [s["with"]["pattern"] for s in report["steps"] if "pattern" in (s.get("with") or {})]
    assert patterns == ["playwright-evidence-*"], patterns
    print("PASS  quarantine artifact stays outside E2E report's merge pattern")


def run_suite_step(shard: str, total: str, extra: str, quarantine: str) -> list[str]:
    script = named_step(load()["jobs"]["e2e"], "Run e2e suite")["run"]
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        log = root / "argv"
        stubs = {
            "npm": '#!/usr/bin/env bash\nif [ "$1" = pkg ]; then echo \'"playwright test"\'; exit 0; fi\n',
            "pnpm": f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {log}\n',
        }
        for name, body in stubs.items():
            f = bin_dir / name
            f.write_text(body)
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        env = dict(os.environ)
        env.update(
            PATH=f"{bin_dir}:{env['PATH']}",
            GATE="Run e2e suite",
            SCRIPT="test:e2e",
            PM="pnpm",
            SHARD=shard,
            TOTAL=total,
            EXTRA_ARGS=extra,
            QUARANTINE_ARGS=quarantine,
            GITHUB_STEP_SUMMARY=str(root / "summary"),
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, cwd=tmp)
        assert result.returncode == 0, result.stderr
        return log.read_text().split("\n")[:-1]


def check_arguments() -> None:
    q = "--project=quarantine --retries=0"
    argv = run_suite_step("quarantine", "3", "--project=web", q)
    assert argv == ["run", "test:e2e", "--project=quarantine", "--retries=0"], argv
    print("PASS  quarantine leg: only the quarantine args, unsharded, no blob reporter")
    argv = run_suite_step("2", "3", "--project=web", q)
    assert argv == ["run", "test:e2e", "--shard=2/3", "--reporter=blob", "--project=web"], argv
    print("PASS  gate shard: unchanged by e2e-quarantine-args")


def lint_findings(doc: dict) -> list[str]:
    f = lint.Findings()
    lint.check_required_job(PATH, doc, f)
    lint.check_fail_closed_playwright(PATH, doc, f)
    return f.items


def check_linter() -> None:
    doc = load()
    assert lint_findings(doc) == [], lint_findings(doc)
    print("PASS  linter accepts the shipped non-gating lane")

    def mutated(fn) -> dict:
        d = copy.deepcopy(doc)
        fn(d["jobs"])
        return d

    cases = {
        "continue-on-error removed": lambda j: j["e2e-quarantine"].pop("continue-on-error"),
        "continue-on-error false": lambda j: j["e2e-quarantine"].__setitem__("continue-on-error", False),
        "Required needs it": lambda j: j["required"]["needs"].append("e2e-quarantine"),
        "another job needs it": lambda j: j["e2e-report"]["needs"].append("e2e-quarantine"),
        "undeclared job with continue-on-error": lambda j: j["e2e"].__setitem__("continue-on-error", True),
        "undeclared job outside Required": lambda j: j.__setitem__("stray", {"steps": []}),
    }
    for label, fn in cases.items():
        items = lint_findings(mutated(fn))
        assert items, f"linter accepted: {label}"
        print(f"PASS  linter rejects: {label}")


def main() -> None:
    check_input()
    check_job_shape()
    check_artifact_name()
    check_arguments()
    check_linter()
    print("\ne2e quarantine lane contract passed")


if __name__ == "__main__":
    main()
