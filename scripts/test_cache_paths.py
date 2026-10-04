#!/usr/bin/env python3
"""Check hosted dependency-cache paths cannot vary with runner HOME/slot.

The path the Resolve step hands to actions/cache must be absolute with no
`.` or `..` segment: actions/cache rejects anything else ("Invalid pattern ...
Relative pathing '.' and '..' is not allowed"), the save step still ends green,
and nothing is ever cached (workflows#177). The step's shipped `run:` text is
executed against a fixture workspace, so this cannot drift from the workflow.
"""

import os
import subprocess
import tempfile
from pathlib import Path

import yaml

WORKFLOWS = tuple(Path(".github/workflows").glob("*.yml"))
RESOLVE = "Resolve dependency cache directory"


def run_resolve(run: str, pm: str) -> str:
    """Execute the Resolve step for `pm`; return an error string or ''."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        work = base / "work" / "repo"
        work.mkdir(parents=True)
        out = base / "output"
        out.touch()
        bin_dir = base / "bin"
        bin_dir.mkdir()
        # pnpm/npm are only used to record the store; stub them out.
        for tool in ("pnpm", "npm"):
            stub = bin_dir / tool
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GITHUB_WORKSPACE": str(work),
            "GITHUB_OUTPUT": str(out),
        }
        body = run.replace("${{ inputs.package-manager }}", pm)
        proc = subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True)
        if proc.returncode:
            return f"step exited {proc.returncode}: {proc.stderr.strip()}"
        lines = [line for line in out.read_text().splitlines() if line.startswith("path=")]
        if len(lines) != 1:
            return f"expected one path= output, got {lines}"
        value = lines[0][len("path="):]
        if not value.startswith("/"):
            return f"path output {value!r} is not absolute"
        if ".." in value.split("/") or "." in value.split("/"):
            return f"path output {value!r} has a relative segment"
        if Path(value).parent != base / "work" / ".ci-cache":
            return f"path output {value!r} is not beside the checkout"
    return ""


def main() -> int:
    sites = 0
    failures = 0
    for path in WORKFLOWS:
        doc = yaml.safe_load(path.read_text())
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if not isinstance(step, dict) or step.get("name") != RESOLVE:
                    continue
                sites += 1
                run = step.get("run", "")
                failed = False
                for pm in ("pnpm", "npm"):
                    err = run_resolve(run, pm)
                    if err:
                        print(f"FAIL {path}:{job_id} ({pm}): {err}")
                        failed = True
                if failed:
                    failures += 1
                else:
                    print(f"PASS {path}:{job_id}: absolute, normalised cache paths outside the checkout")
    print(f"\ntest_cache_paths: {sites} site(s), {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
