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
    inp = trigger["workflow_call"]["inputs"]["e2e-full-paths"]
    assert inp["required"] is False
    assert inp["type"] == "string"
    assert inp["default"] == ""
    print("PASS  e2e-full-paths input declared, optional, default empty")


def check_job_wiring() -> None:
    doc = load_doc()
    jobs = doc["jobs"]

    plan = jobs["e2e-plan"]
    assert plan["outputs"]["skipped"] == "${{ steps.skip.outputs.skipped }}"
    assert plan["outputs"]["full"] == "${{ steps.skip.outputs.full }}"
    plan_step = named_step(plan, "Compute shard list")
    assert plan_step["env"]["FULL"] == "${{ steps.skip.outputs.full }}", plan_step["env"]
    # workflows#59: this job must never request a permission its callers do
    # not grant. `pull-requests: read` here fails EVERY caller's run at
    # startup with zero jobs and no annotation.
    assert plan["permissions"] == {"contents": "read"}, plan["permissions"]
    skip_step = named_step(plan, "Decide whether E2E can be skipped")
    api_calls = [
        line.strip()
        for line in skip_step["run"].splitlines()
        if "gh api" in line and not line.lstrip().startswith("#")
    ]
    assert api_calls, "the skip step makes no GitHub API call"
    assert all("/compare/" in call for call in api_calls), api_calls
    assert not any("/pulls/" in call for call in api_calls), api_calls

    e2e = jobs["e2e"]
    assert e2e["if"] == "inputs.run-e2e && needs.e2e-plan.outputs.skipped != 'true'"
    assert "e2e-plan" in e2e["needs"]

    report = jobs["e2e-report"]
    assert "needs.e2e-plan.result == 'success'" in report["if"]
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
    """A `gh` stub that answers `gh api .../compare/BASE...HEAD --paginate --jq ...`
    with one filename per line, mirroring the real command's output shape.

    The step reads the changed-file list through the COMPARE endpoint, not
    `pulls/<n>/files`: comparing two commits is a Contents read and needs only
    the `contents: read` every caller grants, where `pulls/<n>/files` needs
    `pull-requests: read` — an escalation that fails the caller's whole run at
    startup (workflows#59)."""
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
    base_sha: str,
    head_sha: str,
    skip_patterns: str,
    files: list[str],
    full_patterns: str = "",
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
        env["BASE_SHA"] = base_sha
        env["HEAD_SHA"] = head_sha
        env["SKIP_PATTERNS"] = skip_patterns
        env["FULL_PATTERNS"] = full_patterns
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
    base, head = "a" * 40, "b" * 40
    cases = [
        (
            "empty e2e-skip-paths never skips",
            dict(
                event_name="pull_request",
                base_sha=base,
                head_sha=head,
                skip_patterns="",
                files=["README.md"],
            ),
            "false",
        ),
        (
            "push event never skips even with matching patterns",
            dict(
                event_name="push",
                base_sha="",
                head_sha="",
                skip_patterns="**/*.md",
                files=["README.md"],
            ),
            "false",
        ),
        (
            "a pull request with no base/head sha in context -> do not skip",
            dict(
                event_name="pull_request",
                base_sha="",
                head_sha="",
                skip_patterns="**/*.md",
                files=["README.md"],
            ),
            "false",
        ),
        (
            "all changed files match -> skipped",
            dict(
                event_name="pull_request",
                base_sha=base,
                head_sha=head,
                skip_patterns="**/*.md design/** docs/**",
                files=["README.md", "docs/agents/foo.md", "design/x/y.png"],
            ),
            "true",
        ),
        (
            "one changed .vue file among md changes -> not skipped",
            dict(
                event_name="pull_request",
                base_sha=base,
                head_sha=head,
                skip_patterns="**/*.md design/** docs/**",
                files=["README.md", "app/components/Foo.vue"],
            ),
            "false",
        ),
        (
            "CSS-only diff is never matched by a docs/markdown pattern set",
            dict(
                event_name="pull_request",
                base_sha=base,
                head_sha=head,
                skip_patterns="**/*.md design/** docs/** .lane-evidence/** LICENSE",
                files=["app/assets/css/main.css"],
            ),
            "false",
        ),
        (
            "no changed files reported -> safe default, do not skip",
            dict(
                event_name="pull_request",
                base_sha=base,
                head_sha=head,
                skip_patterns="**/*.md",
                files=[],
            ),
            "false",
        ),
    ]
    cases += _compare_cap_cases()
    cases += _full_paths_cases()
    failures = _run_cases(cases)
    failures += _check_missing_gh_degrades_safely()
    failures += _check_failing_gh_degrades_safely()
    if failures:
        raise SystemExit(f"{failures} case(s) failed")


def _compare_cap_cases() -> list:
    """The compare endpoint's `.files` array stops at 300 entries SILENTLY —
    no `Link` header, no truncation flag, and `?page=2` paginates commits
    rather than files, so `--paginate` cannot recover the rest. A truncated
    list can only make more files look matched than really are, so the cap
    must fail CLOSED: at or above 300 entries the answer is "cannot
    determine", which means run the full suite."""
    base, head = "a" * 40, "b" * 40
    just_under = [f"docs/page-{i:04d}.md" for i in range(299)]
    at_cap = [f"docs/page-{i:04d}.md" for i in range(300)]
    return [
        (
            "299 changed files, all matching -> still allowed to skip",
            dict(
                event_name="pull_request",
                base_sha=base,
                head_sha=head,
                skip_patterns="docs/**",
                files=just_under,
            ),
            "true",
        ),
        (
            "300 changed files at the compare cap -> fail closed, run the full suite",
            dict(
                event_name="pull_request",
                base_sha=base,
                head_sha=head,
                skip_patterns="docs/**",
                files=at_cap,
            ),
            "false",
        ),
    ]


def _full_paths_cases() -> list:
    """`e2e-full-paths`: a pull request touching any listed glob runs the
    full suite (`full=true`) instead of the pull-request tier. When the
    changed files cannot be determined, an opted-in caller gets the full
    suite; a caller that did not opt in keeps today's behavior."""
    base, head = "a" * 40, "b" * 40
    pr = dict(event_name="pull_request", base_sha=base, head_sha=head)
    return [
        (
            "full-paths match on a PR -> full=true",
            dict(pr, skip_patterns="", full_patterns="server/database/** drizzle/**",
                 files=["app/pages/index.vue", "server/database/schema.ts"]),
            {"skipped": "false", "full": "true"},
        ),
        (
            "full-paths no match on a PR -> full=false (pull-request tier)",
            dict(pr, skip_patterns="", full_patterns="server/database/**",
                 files=["app/pages/index.vue"]),
            {"skipped": "false", "full": "false"},
        ),
        (
            "full-paths and skip-paths together: docs-only PR still skips",
            dict(pr, skip_patterns="**/*.md", full_patterns="server/database/**",
                 files=["README.md"]),
            {"skipped": "true", "full": "false"},
        ),
        (
            "full-paths match beats skip-paths: no skip, full=true",
            dict(pr, skip_patterns="**/*.md server/**", full_patterns="server/database/**",
                 files=["server/database/schema.ts"]),
            {"skipped": "false", "full": "true"},
        ),
        (
            "full-paths opted in, changed files unknown (empty list) -> full=true",
            dict(pr, skip_patterns="", full_patterns="server/database/**", files=[]),
            {"skipped": "false", "full": "true"},
        ),
        (
            "full-paths opted in, missing head sha -> full=true",
            dict(event_name="pull_request", base_sha=base, head_sha="",
                 skip_patterns="", full_patterns="server/database/**", files=["x"]),
            {"skipped": "false", "full": "true"},
        ),
        (
            "full-paths on a push -> unaffected, full=false",
            dict(event_name="push", base_sha=base, head_sha=head, skip_patterns="",
                 full_patterns="server/database/**", files=["server/database/schema.ts"]),
            {"skipped": "false", "full": "false"},
        ),
    ]


def parse_outputs(text: str) -> dict:
    """The step writes one `key=value` line per output (`skipped`, `full`)."""
    return dict(line.split("=", 1) for line in text.strip().splitlines() if "=" in line)


def _run_cases(cases: list) -> int:
    """`expected` is the `skipped` value alone (then `full` must be "false"),
    or a dict of every output the case asserts."""
    failures = 0
    for label, kwargs, expected in cases:
        result, output = run_skip_step(**kwargs)
        want = expected if isinstance(expected, dict) else {"skipped": expected, "full": "false"}
        ok = result.returncode == 0 and parse_outputs(output) == want
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
        env["BASE_SHA"] = "a" * 40
        env["HEAD_SHA"] = "b" * 40
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
        ok = result.returncode == 0 and parse_outputs(output) == {"skipped": "false", "full": "false"}
        label = "gh CLI absent -> degrades to running the full suite, never fails"
        if ok:
            print(f"PASS  {label}")
            return 0
        print(f"FAIL  {label}: rc={result.returncode} output={output!r} stderr={result.stderr!r}")
        return 1


def _check_failing_gh_degrades_safely() -> int:
    """A compare call that errors (rate limit, 404, network) must run the full
    suite rather than fail the E2E plan job."""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        script = bin_dir / "gh"
        script.write_text("#!/usr/bin/env bash\necho 'gh: Not Found (HTTP 404)' >&2\nexit 1\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        output_path = root / "github_output"
        output_path.write_text("")
        summary_path = root / "github_step_summary"
        summary_path.write_text("")
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["GH_TOKEN"] = "fake"
        env["REPO"] = "narduk-enterprises/operator-portal"
        env["EVENT_NAME"] = "pull_request"
        env["BASE_SHA"] = "a" * 40
        env["HEAD_SHA"] = "b" * 40
        env["SKIP_PATTERNS"] = "**/*.md"
        env["GITHUB_OUTPUT"] = str(output_path)
        env["GITHUB_STEP_SUMMARY"] = str(summary_path)
        result = subprocess.run(
            ["bash", "-c", skip_step_script()], capture_output=True, text=True, env=env
        )
        output = output_path.read_text().strip()
        label = "compare API failure -> degrades to running the full suite, never fails"
        if result.returncode == 0 and parse_outputs(output) == {"skipped": "false", "full": "false"}:
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
            # The step also resolves the effective pull-request shard/args
            # override (workflows#83). This case is about the skip path only,
            # so both overrides are UNSET and the event is a push — the
            # combination that must leave `e2e-shards` in force.
            env["PR_TOTAL"] = "0"
            env["ARGS"] = ""
            env["PR_ARGS"] = ""
            env["EVENT_NAME"] = "push"
            env["GITHUB_OUTPUT"] = str(output_path)
            result = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, env=env
            )
            written = output_path.read_text().strip()
            # Assert the `shards=` assignment specifically rather than the
            # whole file: this step writes the effective shard total and args
            # alongside it, and those belong to test_e2e_pr_subset.py.
            lines = [line for line in written.splitlines() if line.startswith("shards=")]
            ok = result.returncode == 0 and lines == [expect]
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
