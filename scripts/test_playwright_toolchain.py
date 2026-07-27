#!/usr/bin/env python3
"""Execute the shipped isolated Playwright guards against fake image trees.

The workflow step is the product. These tests extract and execute its exact
shell text, so a version mismatch, missing executable, job-local browser path,
or installer opt-in has to be observed returning non-zero before the callable
can ship.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile

import yaml

WORKFLOW = pathlib.Path(".github/workflows/nuxt-cloudflare.yml")
VERSION = "1.61.1"
BROWSERS = {
    "comment": "fixture",
    "browsers": [
        {
            "name": "chromium-headless-shell",
            "revision": "1228",
            "browserVersion": "149.0.7827.55",
            "installByDefault": True,
        },
        {
            "name": "webkit",
            "revision": "2311",
            "browserVersion": "26.5",
            "installByDefault": True,
        },
    ],
}


def step_script(name: str) -> str:
    doc = yaml.safe_load(WORKFLOW.read_text())
    for step in doc["jobs"]["e2e"]["steps"]:
        if step.get("name") == name:
            return step["run"]
    raise SystemExit(f"::error::no e2e step named {name!r}")


def write_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def make_fixture(root: pathlib.Path) -> dict[str, pathlib.Path]:
    root.mkdir(parents=True)
    project = root / "project"
    image = root / "image"
    visible = root / "visible-browsers"
    runner_temp = root / "runner-temp"
    project.mkdir()
    visible.mkdir()
    runner_temp.mkdir()

    write_json(
        project / "package.json",
        {
            "name": "fixture",
            "devDependencies": {"@playwright/test": VERSION},
        },
    )
    write_json(
        project / "node_modules/@playwright/test/package.json",
        {"name": "@playwright/test", "version": VERSION, "main": "index.js"},
    )
    write_json(
        project / "node_modules/@playwright/test/node_modules/playwright/package.json",
        {"name": "playwright", "version": VERSION},
    )
    write_json(
        project
        / "node_modules/@playwright/test/node_modules/playwright/node_modules/playwright-core/package.json",
        {"name": "playwright-core", "version": VERSION},
    )
    write_json(
        project
        / "node_modules/@playwright/test/node_modules/playwright/node_modules/playwright-core/browsers.json",
        BROWSERS,
    )

    fake_module = r"""
const path = require('node:path')
function engine(directory, executable) {
  return {
    executablePath() {
      return path.join(process.env.PLAYWRIGHT_BROWSERS_PATH, directory, executable)
    },
    async launch() {
      return {
        async newPage() {
          return {
            async setContent() {},
            async title() { return 'playwright-isolated-canary' },
          }
        },
        async close() {},
      }
    },
  }
}
module.exports = {
  chromium: engine('chromium_headless_shell-1228', 'chrome-linux/headless_shell'),
  webkit: engine('webkit-2311', 'pw_run.sh'),
}
"""
    (project / "node_modules/@playwright/test/index.js").write_text(fake_module)

    write_json(
        image / "npm/node_modules/playwright/package.json",
        {"name": "playwright", "version": VERSION},
    )
    write_json(image / "npm/node_modules/playwright-core/browsers.json", BROWSERS)

    executables = [
        ("chromium_headless_shell-1228", "chrome-linux/headless_shell"),
        ("webkit-2311", "pw_run.sh"),
    ]
    for directory, executable in executables:
        target = image / "browsers" / directory
        selected = target / executable
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_text("#!/bin/sh\nexit 0\n")
        selected.chmod(0o755)
        (visible / directory).symlink_to(target, target_is_directory=True)

    summary = root / "summary.md"
    summary.touch()
    return {
        "project": project,
        "image": image,
        "visible": visible,
        "runner_temp": runner_temp,
        "summary": summary,
    }


def run_toolchain(
    fixture: dict[str, pathlib.Path],
    *,
    env_overrides: dict[str, str] | None = None,
) -> tuple[int, str]:
    env = {
        **os.environ,
        "GITHUB_STEP_SUMMARY": str(fixture["summary"]),
        "GITHUB_WORKSPACE": str(fixture["project"]),
        "PLAYWRIGHT_ALLOWED_BROWSER_PREFIX": str(fixture["visible"].parent),
        "PLAYWRIGHT_BROWSERS_PATH": str(fixture["visible"]),
        "PLAYWRIGHT_TOOLCHAIN_OWNER_UID": str(os.getuid()),
        "PLAYWRIGHT_TOOLCHAIN_ROOT": str(fixture["image"]),
        "REQUIRED_BROWSERS": "chromium webkit",
        "RUNNER_TEMP": str(fixture["runner_temp"]),
        **(env_overrides or {}),
    }
    result = subprocess.run(
        ["bash", "-c", step_script("Assert isolated Playwright toolchain")],
        cwd=fixture["project"],
        env=env,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout + result.stderr + fixture["summary"].read_text()


def check(label: str, rc: int, output: str, want_rc: int, needle: str) -> bool:
    ok = rc == want_rc and needle in output
    print(("PASS  " if ok else "FAIL  ") + label)
    if not ok:
        print(f"      rc={rc} (wanted {want_rc}); needle={needle!r}\n{output[-1200:]}")
    return ok


def main() -> int:
    failed = 0
    total = 0
    with tempfile.TemporaryDirectory() as temp:
        root = pathlib.Path(temp)

        fixture = make_fixture(root / "green")
        rc, output = run_toolchain(fixture)
        total += 1
        failed += not check("exact image package, revisions and executables pass", rc, output, 0, "launch canary | `passed`")

        fixture = make_fixture(root / "wrong-version")
        write_json(
            fixture["project"] / "node_modules/@playwright/test/package.json",
            {"name": "@playwright/test", "version": "1.59.1", "main": "index.js"},
        )
        rc, output = run_toolchain(fixture)
        total += 1
        failed += not check("wrong installed Playwright version fails", rc, output, 1, "must be pinned exactly")

        fixture = make_fixture(root / "range-pin")
        manifest = json.loads((fixture["project"] / "package.json").read_text())
        manifest["devDependencies"]["@playwright/test"] = "^1.61.1"
        write_json(fixture["project"] / "package.json", manifest)
        rc, output = run_toolchain(fixture)
        total += 1
        failed += not check("floating consumer pin fails", rc, output, 1, "must be pinned exactly")

        fixture = make_fixture(root / "wrong-revision")
        changed = json.loads(json.dumps(BROWSERS))
        changed["browsers"][0]["revision"] = "9999"
        write_json(
            fixture["project"]
            / "node_modules/@playwright/test/node_modules/playwright/node_modules/playwright-core/browsers.json",
            changed,
        )
        rc, output = run_toolchain(fixture)
        total += 1
        failed += not check("browser revision drift fails", rc, output, 1, "browser manifest mismatch")

        fixture = make_fixture(root / "missing-browser")
        (fixture["image"] / "browsers/chromium_headless_shell-1228/chrome-linux/headless_shell").unlink()
        rc, output = run_toolchain(fixture)
        total += 1
        failed += not check("missing selected browser fails", rc, output, 1, "executable is missing")

        fixture = make_fixture(root / "job-local")
        local_browsers = fixture["runner_temp"] / "playwright-browsers"
        local_browsers.mkdir()
        rc, output = run_toolchain(
            fixture,
            env_overrides={"PLAYWRIGHT_BROWSERS_PATH": str(local_browsers)},
        )
        total += 1
        failed += not check("RUNNER_TEMP browser path fails", rc, output, 1, "job-local PLAYWRIGHT_BROWSERS_PATH is forbidden")

        guard = step_script("Guard isolated Playwright route")
        guard_cases = [
            (
                "isolated route rejects installer opt-in",
                {"ROUTE": '{"group":"playwright-isolated"}', "INSTALL_BROWSERS": "true", "BROWSERS_OVERRIDE": ""},
                1,
                "must be false",
            ),
            (
                "isolated route rejects path override",
                {"ROUTE": '["proxmox-playwright-x64"]', "INSTALL_BROWSERS": "false", "BROWSERS_OVERRIDE": "/tmp/browsers"},
                1,
                "must be empty",
            ),
            (
                "isolated route accepts locked inputs",
                {"ROUTE": '{"group":"playwright-isolated"}', "INSTALL_BROWSERS": "false", "BROWSERS_OVERRIDE": ""},
                0,
                "isolated route locked",
            ),
        ]
        for label, extra, want_rc, needle in guard_cases:
            result = subprocess.run(
                ["bash", "-c", guard],
                env={**os.environ, **extra},
                capture_output=True,
                text=True,
            )
            total += 1
            failed += not check(label, result.returncode, result.stdout + result.stderr, want_rc, needle)

    print(f"\ntest_playwright_toolchain: {total} case(s), {failed} failure(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
