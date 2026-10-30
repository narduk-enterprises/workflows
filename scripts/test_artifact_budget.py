#!/usr/bin/env python3
"""R13: GitHub artifact uploads are short-lived, non-fatal and surfaced.

GitHub's org artifact storage quota failed stonx run 41102439037's `ci / Build`
(2026-10-30) at `Publish exact production output`: a storage refusal, not a
build or test result, turned the required check red. This test pins down:

- the shipped callables pass R13 and R11 (the one upload is a registered
  handoff: retention 1, `continue-on-error: true`, its outcome read later);
- the linter rejects each way of making an upload long-lived, fatal, silent
  or unconditional, and R11 still refuses `continue-on-error` everywhere else;
- the shipped `Report unstored production output` step warns, writes the job
  summary and exits 0, while `production-artifact` stays set so the caller's
  upload job fails closed and nothing is promoted.

Run: python3 scripts/test_artifact_budget.py
"""

from __future__ import annotations

import copy
import os
import pathlib
import subprocess
import sys
import tempfile

import yaml

sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import lint_callables as lint  # noqa: E402

WORKFLOWS = pathlib.Path(".github/workflows")
NUXT = WORKFLOWS / "nuxt-cloudflare.yml"
UPLOAD = WORKFLOWS / "cloudflare-version-upload.yml"
PUBLISH = "Publish exact production output"
REPORT = "Report unstored production output"


def load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


def named_step(job: dict, name: str) -> dict:
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    raise SystemExit(f"FAIL  no step named {name!r}")


def findings(path: pathlib.Path, doc: dict) -> list[str]:
    f = lint.Findings()
    lint.check_fail_closed_playwright(path, doc, f)
    lint.check_artifact_budget(path, doc, f)
    return f.items


def check_shipped() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        items = findings(path, load(path))
        assert items == [], items
    print("PASS  every shipped workflow passes R11 and R13")

    build = load(NUXT)["jobs"]["build"]
    publish = named_step(build, PUBLISH)
    assert publish["id"] == "publish-production"
    assert publish["continue-on-error"] is True
    assert publish["with"]["retention-days"] == 1
    assert publish["with"]["if-no-files-found"] == "error"
    assert publish["if"] == "steps.production-output.outputs.artifact != ''"
    report = named_step(build, REPORT)
    assert report["if"] == "steps.publish-production.outcome == 'failure'"
    # The handoff name is published from Prepare, never from the upload, so a
    # refused upload cannot look like a path-skip and silently skip deploy.
    outputs = load(NUXT)["jobs"]["build"]["outputs"]
    assert outputs["production-artifact"] == "${{ steps.production-output.outputs.artifact }}"
    # The consumer still fails closed on a missing artifact.
    download = next(s for s in load(UPLOAD)["jobs"]["upload"]["steps"]
                    if str(s.get("uses", "")).startswith("actions/download-artifact@"))
    assert "continue-on-error" not in download
    print("PASS  production handoff: one day, non-fatal, surfaced, output kept, consumer fail-closed")


def check_linter() -> None:
    doc = load(NUXT)

    def mutated(fn) -> dict:
        d = copy.deepcopy(doc)
        fn(d["jobs"]["build"])
        return d

    def publish(job: dict) -> dict:
        return named_step(job, PUBLISH)

    def add_report(job: dict, **overrides) -> None:
        step = {
            "name": "Upload test report",
            "if": "failure()",
            "continue-on-error": True,
            "uses": "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
            "with": {"name": "report", "path": "report", "retention-days": 3},
        }
        for key, value in overrides.items():
            if key == "retention":
                step["with"]["retention-days"] = value
            elif value is None:
                step.pop(key)
            else:
                step[key] = value
        job["steps"].append(step)

    assert findings(NUXT, mutated(lambda j: add_report(j))) == []
    assert findings(NUXT, mutated(lambda j: add_report(j, **{"if": "${{ failure() && inputs.x }}"}))) == []
    print("PASS  linter accepts a failure-only, non-fatal, three-day report upload")

    cases = {
        "handoff retention removed": lambda j: publish(j)["with"].pop("retention-days"),
        "handoff retention 2": lambda j: publish(j)["with"].__setitem__("retention-days", 2),
        "handoff retention from an expression": lambda j: publish(j)["with"].__setitem__(
            "retention-days", "${{ inputs.retention }}"),
        "handoff fatal again": lambda j: publish(j).pop("continue-on-error"),
        "handoff continue-on-error false": lambda j: publish(j).__setitem__("continue-on-error", False),
        "handoff continue-on-error from an expression": lambda j: publish(j).__setitem__(
            "continue-on-error", "${{ true }}"),
        "handoff renamed out of ARTIFACT_HANDOFFS": lambda j: publish(j).__setitem__("id", "other"),
        "handoff refusal swallowed": lambda j: named_step(j, REPORT).__setitem__("if", "always()"),
        "report step removed": lambda j: j["steps"].remove(named_step(j, REPORT)),
        "report uploads on success": lambda j: add_report(j, **{"if": "always()"}),
        "report uploads unconditionally": lambda j: add_report(j, **{"if": None}),
        "report widened with always()": lambda j: add_report(j, **{"if": "always() || failure()"}),
        "report widened with !cancelled()": lambda j: add_report(j, **{"if": "failure() || !cancelled()"}),
        "report keeps 7 days": lambda j: add_report(j, retention=7),
        "report keeps 0 days (repo default)": lambda j: add_report(j, retention=0),
        "report has no retention": lambda j: j["steps"].append(
            {"name": "r", "if": "failure()", "continue-on-error": True,
             "uses": "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a", "with": {"path": "r"}}),
        "report fatal": lambda j: add_report(j, **{"continue-on-error": None}),
        "continue-on-error on a non-upload step": lambda j: named_step(j, "Prepare production artifact").__setitem__(
            "continue-on-error", True),
        "continue-on-error on a job": lambda j: j.__setitem__("continue-on-error", True),
    }
    for label, fn in cases.items():
        items = findings(NUXT, mutated(fn))
        assert items, f"linter accepted: {label}"
        print(f"PASS  linter rejects: {label}")

    upload = load(UPLOAD)
    download = next(s for s in upload["jobs"]["upload"]["steps"]
                    if str(s.get("uses", "")).startswith("actions/download-artifact@"))
    download["continue-on-error"] = True
    assert findings(UPLOAD, upload), "linter accepted: continue-on-error on a download"
    print("PASS  linter rejects: continue-on-error on a download")


def check_report_step() -> None:
    script = named_step(load(NUXT)["jobs"]["build"], REPORT)["run"]
    with tempfile.TemporaryDirectory() as tmp:
        summary = pathlib.Path(tmp) / "summary.md"
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", script], text=True, capture_output=True,
            env={**os.environ, "ARTIFACT": "production-" + "a" * 40 + "-42-1-0123456789ab",
                 "GITHUB_STEP_SUMMARY": str(summary)})
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("::warning title=Production output not stored::"), result.stdout
        assert "production-" + "a" * 40 in result.stdout
        assert "fails closed" in result.stdout and "nothing is promoted" in result.stdout
        assert "Production output not stored" in summary.read_text()
    print("PASS  report step warns, writes the summary and exits 0")


def main() -> None:
    check_shipped()
    check_linter()
    check_report_step()
    print("\nartifact budget contract passed")


if __name__ == "__main__":
    main()
