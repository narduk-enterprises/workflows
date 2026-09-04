#!/usr/bin/env python3
"""e2e-skip-paths: docs-only PR diffs skip E2E; everything else still runs it.

(workflows#49) Adds a callable input so a caller can declare path globs that
never need the browser suite. When `run-e2e` is true, the event is a pull
request, and EVERY changed file matches at least one glob, `E2E plan` must
emit an empty shard list and `skipped=true`; the `E2E` and `E2E report` jobs
then skip via their own `if:`, and `Required` still gates them (skipped is
the required outcome, not an absent check).

This is a pure source contract over the shipped YAML: extract and execute
the "Decide whether E2E can be skipped" step's exact script (with a stub
`gh` on PATH standing in for the GitHub API), and separately assert the
job-level `if:` wiring and the `Required` gating logic reference the new
`needs.e2e-plan.outputs.skipped` output.

Run: python3 scripts/test_e2e_skip_paths.py
"""

from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import tempfile

import yaml

WORKFLOW = pathlib.Path(".github/workflows/nuxt-cloudflare.yml")


def load_doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def named_step(job: dict, name: str) -> dict:
    matches = [step for step in job.get("steps", []) if step.get("name") == name]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {name!r} step")
    return matches[0]


def check_input_declared() -> None:
    doc = load_doc()
    trigger = doc.get(True, doc.get("on", {}))
    inp = trigger["workflow_call"]["inputs"]["e2e-skip-paths"]
    assert inp["required"] is False
    assert inp["type"] == "string"
    assert inp["default"] == ""
    print("PASS  e2e-skip-paths input declared, optional, default empty")


def check_job_wiring() -> None:
    doc = load_doc()
    jobs = doc["jobs"]

    plan = jobs["e2e-plan"]
    assert plan["outputs"]["skipped"] == "${{ steps.skip.outputs.skipped }}"

    e2e = jobs["e2e"]
    assert e2e["if"] == "inputs.run-e2e && needs.e2e-plan.outputs.skipped != 'true'"
    assert "e2e-plan" in e2e["needs"]

    report = jobs["e2e-report"]
    assert "needs.e2e-plan.outputs.skipped != 'true'" in report["if"]
    assert "e2e-plan" in report["needs"]

    required = jobs["required"]
    step = required["steps"][0]
    assert step["env"]["E2E_PLAN_SKIPPED"] == "${{ needs.e2e-plan.outputs.skipped }}"
    assert "E2E_PLAN_SKIPPED" in step["run"]
    print("PASS  E2E / E2E report / Required all gate on e2e-plan's skipped output")


def skip_step_script() -> str:
    doc = load_doc()
    return named_step(doc["jobs"]["e2e-plan"], "Decide whether E2E can be skipped")["run"]


def plan_step_script() -> str:
    doc = load_doc()
    return named_step(doc["jobs"]["e2e-plan"], "Compute shard list")["run"]


def make_stub_gh(bin_dir: pathlib.Path, files: list[str]) -> None:
    """A `gh` stub that answers `gh api .../pulls/N/files --paginate --jq ...`
    with one filename per line, mirroring the real command's output shape."""
    script = bin_dir / "gh"
    body = "#!/usr/bin/env bash\nset -euo pipefail\n"
    for f in files:
        body += f"printf '%s\\n' {shell_quote(f)}\n"
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def run_skip_step(
    *,
    event_name: str,
    pr_number: str,
    skip_patterns: str,
    files: list[str],
) -> tuple[subprocess.CompletedProcess, str]:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        make_stub_gh(bin_dir, files)
        output_path = root / "github_output"
        output_path.write_text("")
        summary_path = root / "github_step_summary"
        summary_path.write_text("")
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["GH_TOKEN"] = "fake"
        env["REPO"] = "narduk-enterprises/operator-portal"
        env["EVENT_NAME"] = event_name
        env["PR_NUMBER"] = pr_number
        env["SKIP_PATTERNS"] = skip_patterns
        env["GITHUB_OUTPUT"] = str(output_path)
        env["GITHUB_STEP_SUMMARY"] = str(summary_path)
        result = subprocess.run(
            ["bash", "-c", skip_step_script()],
            capture_output=True,
            text=True,
            env=env,
        )
        return result, output_path.read_text()


def check_behavior() -> None:
    cases = [
        (
            "empty e2e-skip-paths never skips",
            dict(event_name="pull_request", pr_number="9", skip_patterns="", files=["README.md"]),
            "false",
        ),
        (
            "push event never skips even with matching patterns",
            dict(event_name="push", pr_number="", skip_patterns="**/*.md", files=["README.md"]),
            "false",
        ),
        (
            "all changed files match -> skipped",
            dict(
                event_name="pull_request",
                pr_number="9",
                skip_patterns="**/*.md design/** docs/**",
                files=["README.md", "docs/agents/foo.md", "design/x/y.png"],
            ),
            "true",
        ),
        (
            "one changed .vue file among md changes -> not skipped",
            dict(
                event_name="pull_request",
                pr_number="9",
                skip_patterns="**/*.md design/** docs/**",
                files=["README.md", "app/components/Foo.vue"],
            ),
            "false",
        ),
        (
            "CSS-only diff is never matched by a docs/markdown pattern set",
            dict(
                event_name="pull_request",
                pr_number="9",
                skip_patterns="**/*.md design/** docs/** .lane-evidence/** LICENSE",
                files=["app/assets/css/main.css"],
            ),
            "false",
        ),
        (
            "no changed files reported -> safe default, do not skip",
            dict(event_name="pull_request", pr_number="9", skip_patterns="**/*.md", files=[]),
            "false",
        ),
    ]
    failures = _run_cases(cases)
    failures += _check_missing_gh_degrades_safely()
    if failures:
        raise SystemExit(f"{failures} case(s) failed")


def _run_cases(cases: list) -> int:
    failures = 0
    for label, kwargs, expected in cases:
        result, output = run_skip_step(**kwargs)
        ok = result.returncode == 0 and output.strip() == f"skipped={expected}"
        if not ok:
            failures += 1
            print(
                f"FAIL  {label}: rc={result.returncode} output={output!r} "
                f"stderr={result.stderr!r}"
            )
        else:
            print(f"PASS  {label}")
    return failures


def _check_missing_gh_degrades_safely() -> int:
    """Self-hosted runners provision their own toolchains; `gh` must never
    be assumed present. Its absence must degrade to "run the full suite",
    never fail the whole E2E plan job."""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        output_path = root / "github_output"
        output_path.write_text("")
        summary_path = root / "github_step_summary"
        summary_path.write_text("")
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "PATH"
        }
        # A PATH with no `gh` anywhere on it — deliberately not the stub bin
        # dir used by the other cases.
        env["PATH"] = "/usr/bin:/bin"
        env["GH_TOKEN"] = "fake"
        env["REPO"] = "narduk-enterprises/operator-portal"
        env["EVENT_NAME"] = "pull_request"
        env["PR_NUMBER"] = "9"
        env["SKIP_PATTERNS"] = "**/*.md"
        env["GITHUB_OUTPUT"] = str(output_path)
        env["GITHUB_STEP_SUMMARY"] = str(summary_path)
        result = subprocess.run(
            ["bash", "-c", skip_step_script()],
            capture_output=True,
            text=True,
            env=env,
        )
        output = output_path.read_text().strip()
        ok = result.returncode == 0 and output == "skipped=false"
        label = "gh CLI absent -> degrades to running the full suite, never fails"
        if ok:
            print(f"PASS  {label}")
            return 0
        print(f"FAIL  {label}: rc={result.returncode} output={output!r} stderr={result.stderr!r}")
        return 1


def check_shard_list_empties_on_skip() -> None:
    script = plan_step_script()
    for skipped, expect in (("true", "shards=[]"), ("false", "shards=[1,2,3]")):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = pathlib.Path(tmp) / "github_output"
            output_path.write_text("")
            env = dict(os.environ)
            env["TOTAL"] = "3"
            env["SKIPPED"] = skipped
            env["GITHUB_OUTPUT"] = str(output_path)
            result = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, env=env
            )
            written = output_path.read_text().strip()
            ok = result.returncode == 0 and written == expect
            label = f"Compute shard list with SKIPPED={skipped}"
            if ok:
                print(f"PASS  {label} -> {expect}")
            else:
                raise SystemExit(
                    f"FAIL  {label}: rc={result.returncode} output={written!r} "
                    f"stderr={result.stderr!r}"
                )


def main() -> None:
    check_input_declared()
    check_job_wiring()
    check_behavior()
    check_shard_list_empties_on_skip()
    print("\ne2e-skip-paths contract passed")


if __name__ == "__main__":
    main()
