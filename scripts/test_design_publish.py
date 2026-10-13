#!/usr/bin/env python3
"""Shape and behaviour tests for `design-publish.yml` (operator-portal#727, design-portal-sync phase 2).

The callable uploads a caller's `design/**` mirror to the designs Worker with the
run's own OIDC token. Properties worth locking down:

  * No secrets and no `Required` job: it is a delivery step, never a gate.
  * `id-token: write` is the one permission beyond `contents: read`, at the
    workflow's documented caller grant and on the job itself.
  * The publish script comes from THIS repository at `job.workflow_sha`, sparse and
    without credentials, never from the caller's tree.
  * The shipped `run:` text refuses any ref but `refs/heads/main` before node runs.
  * `scripts/publish_designs.mjs` lists a fixture's design folders and files (the
    `--dry-run`), skipping `archive/` and a folder with no `canvas.json`, and names
    nothing as published without an OIDC token.

Run: python3 scripts/test_design_publish.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "design-publish.yml"
SCRIPT = ROOT / "scripts" / "publish_designs.mjs"
TOOL_DIR = ".design-publish-workflows"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok  " if ok else "FAIL") + f" {name}" + (f": {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def main() -> int:
    doc = yaml.safe_load(WORKFLOW.read_text())
    on = doc.get("on", doc.get(True))
    call = on["workflow_call"]
    job = doc["jobs"]["publish"]
    steps = job["steps"]

    check("one job, never a Required gate", list(doc["jobs"]) == ["publish"] and "Required" not in WORKFLOW.read_text().split("jobs:")[1])
    check("declares no secrets", "secrets" not in call)
    check("workflow grant is contents: read", doc["permissions"] == {"contents": "read"})
    check("job grant is contents: read + id-token: write", job["permissions"] == {"contents": "read", "id-token": "write"})
    check("origin defaults to the designs Worker", call["inputs"]["origin"]["default"] == "https://designs.nardukenterprises.com")
    check("design-dir defaults to design", call["inputs"]["design-dir"]["default"] == "design")

    script_checkout = next(s for s in steps if s.get("with", {}).get("repository") == "narduk-enterprises/workflows")
    check("script comes from this repo at job.workflow_sha, sparse, credential-free",
          script_checkout["with"]["ref"] == "${{ job.workflow_sha }}"
          and script_checkout["with"]["path"] == TOOL_DIR
          and script_checkout["with"]["sparse-checkout"] == "scripts/publish_designs.mjs"
          and script_checkout["with"]["persist-credentials"] is False)
    check("the caller checkout keeps no credentials", steps[0]["with"]["persist-credentials"] is False)

    publish = next(s for s in steps if "publish_designs.mjs" in s.get("run", ""))
    run = publish["run"]
    check("passes the origin and dir by env, not interpolated into the script",
          publish["env"] == {"DESIGNS_ORIGIN": "${{ inputs.origin }}", "DESIGN_DIR": "${{ inputs.design-dir }}"} and "${{" not in run)
    check("runs the script from the tool checkout", f"node {TOOL_DIR}/scripts/publish_designs.mjs" in run)

    for ref, expect in [("refs/heads/main", True), ("refs/heads/feature", False), ("refs/pull/1/merge", False)]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            node = bin_dir / "node"
            node.write_text('#!/bin/sh\necho "node $*" >> "$NODE_LOG"\n')
            node.chmod(0o755)
            log = tmp_path / "node.log"
            env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "GITHUB_REF": ref, "NODE_LOG": str(log),
                   "DESIGNS_ORIGIN": "https://designs.example.test", "DESIGN_DIR": "design"}
            result = subprocess.run(["bash", "-c", run], env=env, cwd=tmp, capture_output=True, text=True)
            ran = log.exists() and "publish_designs.mjs --design-dir design" in log.read_text()
            check(f"{ref}: {'publishes' if expect else 'refuses before node runs'}",
                  (result.returncode == 0) == expect and ran == expect, result.stdout + result.stderr)

    with tempfile.TemporaryDirectory() as tmp:
        design = Path(tmp) / "design"
        for name, files in {"pages": ["canvas.json", "Main.dc.html", "assets/a.png"], "archive": ["canvas.json"], "empty": ["notes.md"]}.items():
            for f in files:
                path = design / name / f
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
        dry = subprocess.run(["node", str(SCRIPT), "--dry-run", "--design-dir", str(design)], capture_output=True, text=True)
        check("--dry-run lists only the design folder with a canvas.json", dry.returncode == 0 and dry.stdout.strip().startswith("pages: 3 files") and "archive" not in dry.stdout and "empty" not in dry.stdout, dry.stdout + dry.stderr)
        no_token = subprocess.run(["node", str(SCRIPT), "--design-dir", str(design)], capture_output=True, text=True,
                                  env={"PATH": os.environ["PATH"], "DESIGNS_ORIGIN": "https://designs.example.test", "GITHUB_SHA": "a" * 40, "GITHUB_REPOSITORY": "narduk-enterprises/x"})
        check("without an OIDC token it fails and names the permission", no_token.returncode == 1 and "id-token: write" in no_token.stdout + no_token.stderr, no_token.stdout + no_token.stderr)

    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\ntest_design_publish: all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
