#!/usr/bin/env python3
"""Prove Apple routing stays split and the Xcode job cannot become echo-only.

Also proves the agent-infrastructure#965 lease: extracts the "Wait for Apple
build lease", "Build", and "Test" steps' real `run:` text straight out of the
workflow YAML -- like every other scripts/test_*.py in this repo -- and
executes it under bash against a real stand-in lock tool. If someone edits
the embedded lease logic or the steps around it, these tests either still
pass against the new text or they fail; there is no third option where the
test passes while the shipped callable silently stopped leasing.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
import subprocess
import tempfile
import time

import yaml


WORKFLOW = Path(".github/workflows/apple.yml")

# A minimal stand-in for the installed apple_build_lock.py, matching only its
# exit-code contract (0 = available, 75 = busy, 2 = hard error) -- none of
# its production security hardening, which has its own coverage in
# agent-infrastructure skills/apple-release-pipeline/tests. This proves the
# WORKFLOW's own wait/timeout/exit-forwarding orchestration.
STUB_LOCK_TOOL_SOURCE = '''\
import argparse
import fcntl
import os
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock-path", required=True)
    parser.add_argument("--timeout", type=float, default=0.0)
    subparsers = parser.add_subparsers(dest="action")
    subparsers.add_parser("audit")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    action = args.action or "audit"

    fd = os.open(args.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + max(args.timeout, 0)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print(f"BUSY: {args.lock_path}")
                    return 75
                time.sleep(0.05)

        if action == "audit":
            print(f"AVAILABLE: {args.lock_path}")
            return 0

        command = args.command
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            print("ERROR: run requires a command after --", file=sys.stderr)
            return 2
        child = subprocess.Popen(command)
        return child.wait()
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
'''

HOLDER_SOURCE = '''\
import fcntl
import os
import sys
import time

lock_path, ready_path, hold_seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
with open(ready_path, "w", encoding="utf-8") as handle:
    handle.write("ready")
time.sleep(hold_seconds)
fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
'''


def step(document: dict, name: str) -> dict:
    matches = [
        item for item in document["jobs"]["xcode"]["steps"] if item.get("name") == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one xcode/{name} step")
    return matches[0]


def run_gate(
    document: dict,
    fake_bin: str,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            step(document, "Validate Apple toolchain and gate configuration")["run"],
        ],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "BUILD_COMMAND": "swift build",
            "RUN_BUILD": "true",
            "RUN_TESTS": "true",
            "TEST_COMMAND": "swift test",
            **overrides,
        },
        capture_output=True,
        text=True,
    )


def write_executable(path: Path, source: str, *, shebang: bool = True) -> Path:
    text = f"#!{sys.executable}\n{source}" if shebang else source
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def run_named_step(
    document: dict,
    name: str,
    env: dict,
    *,
    timeout: float = 30,
    substitutions: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    text = step(document, name)["run"]
    for expr, value in (substitutions or {}).items():
        text = text.replace(expr, value)
    return subprocess.run(
        ["bash", "-c", text],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def read_github_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def test_lease(document: dict) -> None:
    """The Wait/Build/Test steps prove out agent-infrastructure#965 end to end."""
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        lock_tool = write_executable(root / "stub_lock_tool.py", STUB_LOCK_TOOL_SOURCE)
        holder_script = write_executable(root / "holder.py", HOLDER_SOURCE, shebang=False)
        lock_path = root / "apple-build.lock"
        marker = root / "marker.log"
        runner_temp = root / "runner-temp"
        runner_temp.mkdir()

        def base_env(**overrides: str) -> dict:
            github_env = root / f"github-env-{overrides.get('_tag', 'default')}"
            github_env.write_text("")
            merged = {
                **os.environ,
                "APPLE_BUILD_LOCK_TOOL": str(lock_tool),
                "APPLE_BUILD_LOCK_PATH": str(lock_path),
                "APPLE_BUILD_LEASE_POLL_SECONDS": "1",
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_ENV": str(github_env),
            }
            merged.update({k: v for k, v in overrides.items() if not k.startswith("_")})
            return merged

        marker_command = f'echo built >> "{marker}"'

        # 1. Refuses to run unleased when the lease tool is not installed.
        env = base_env(_tag="missing", APPLE_BUILD_LOCK_TOOL=str(root / "absent.py"))
        result = run_named_step(document, "Wait for Apple build lease", env)
        assert result.returncode == 1, result.stderr
        assert "not installed" in result.stdout
        assert "refusing to run build-command/test-command unleased" in result.stdout

        # 2. Free lease: no waiting evidence, Build actually runs the command.
        env = base_env(_tag="free")
        wait_result = run_named_step(document, "Wait for Apple build lease", env)
        assert wait_result.returncode == 0, wait_result.stderr
        assert "WAITING ON LEASE" not in wait_result.stdout
        env.update(read_github_env(Path(env["GITHUB_ENV"])))
        build_result = run_named_step(
            document,
            "Build",
            env,
            substitutions={"${{ inputs.build-command }}": marker_command},
        )
        assert build_result.returncode == 0, build_result.stderr
        assert "RUNNING UNDER LEASE" in build_result.stdout
        assert marker.exists()
        marker.unlink()

        # 3. Busy-then-free: visible waiting evidence precedes acquisition.
        ready = root / "holder-ready"
        holder = subprocess.Popen(
            [sys.executable, str(holder_script), str(lock_path), str(ready), "2.5"]
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.exists():
                if time.monotonic() >= deadline:
                    raise AssertionError("lock holder did not signal readiness")
                time.sleep(0.05)
            env = base_env(_tag="waits", APPLE_BUILD_LEASE_TIMEOUT_SECONDS="20")
            wait_result = run_named_step(document, "Wait for Apple build lease", env, timeout=30)
        finally:
            holder.wait(timeout=10)
        assert wait_result.returncode == 0, wait_result.stderr
        assert "WAITING ON LEASE" in wait_result.stdout
        assert "LEASE ACQUIRED" in wait_result.stdout

        # 4. Timeout: busy forever times out visibly and never runs the command.
        ready = root / "holder-ready-2"
        holder = subprocess.Popen(
            [sys.executable, str(holder_script), str(lock_path), str(ready), "30"]
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.exists():
                if time.monotonic() >= deadline:
                    raise AssertionError("lock holder did not signal readiness")
                time.sleep(0.05)
            env = base_env(_tag="timeout", APPLE_BUILD_LEASE_TIMEOUT_SECONDS="2")
            wait_result = run_named_step(document, "Wait for Apple build lease", env, timeout=15)
        finally:
            holder.kill()
            holder.wait(timeout=10)
        assert wait_result.returncode == 75, wait_result.stderr
        assert "WAITING ON LEASE" in wait_result.stdout
        assert "still busy after" in wait_result.stdout
        assert "refusing to run unleased" in wait_result.stdout
        assert not marker.exists()

        # 5. Broker-held marker short-circuits the wait loop entirely.
        env = base_env(_tag="broker", APPLE_BUILD_LOCK_HELD="1")
        wait_result = run_named_step(document, "Wait for Apple build lease", env)
        assert wait_result.returncode == 0, wait_result.stderr
        assert "WAITING ON LEASE" not in wait_result.stdout
        env.update(read_github_env(Path(env["GITHUB_ENV"])))
        test_result = run_named_step(
            document,
            "Test",
            env,
            substitutions={"${{ inputs.test-command }}": marker_command},
        )
        assert test_result.returncode == 0, test_result.stderr
        assert marker.exists()

    print("Apple lease wait/timeout/broker-held contract passed (agent-infrastructure#965)")


def test_dependency_auth(document: dict) -> None:
    # Execute the shipped setup and both successful/failing child exits.
    # Verify HTTPS rewriting is owner-scoped and the temporary key disappears.
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        credential = "fake-read-only-deploy-key"
        for name in ("Build", "Test"):
            setup = step(document, name)["run"].split('lock_tool=')[0]
            for value in ("", credential):
                for code in (0, 17):
                    script = setup + "\ngit config --get url.git@github.com:example/.insteadOf || :\n"
                    if value:
                        script += 'test "$(cat "$auth_dir/key")" = "fake-read-only-deploy-key"\npython3 -c \'import os,stat,sys; assert stat.S_IMODE(os.stat(sys.argv[1]).st_mode) == 0o600\' "$auth_dir/key"\n'
                    script += f"exit {code}\n"
                    env = {**os.environ, "HOME": temp, "RUNNER_TEMP": temp,
                           "GIT_CONFIG_GLOBAL": str(root / "global"), "GIT_CONFIG_NOSYSTEM": "1",
                           "DEPENDENCY_OWNER": "example", "DEPENDENCY_SSH_KEY": value}
                    for key in list(env):
                        if key.startswith("GIT_CONFIG_") and key not in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
                            del env[key]
                    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
                    assert result.returncode == code, result.stderr
                    assert credential not in result.stdout + result.stderr
                    assert result.stdout == ("https://github.com/example/\n" if value else "")
                    assert not list(root.glob("swiftpm-auth.*"))
                    assert not (root / "global").exists()
        for job in ("lint", "required"):
            assert "DEPENDENCY_SSH_KEY" not in str(document["jobs"][job])
    print("Private SwiftPM auth passed (owner scope, opt-in, success/failure cleanup)")


def main() -> None:
    document = yaml.safe_load(WORKFLOW.read_text())
    test_dependency_auth(document)
    call = document.get("on", document.get(True))["workflow_call"]
    assert call["secrets"] == {"DEPENDENCY_SSH_KEY": {"description": "Optional read-only GitHub deploy key for a private SwiftPM dependency in the caller's organization.", "required": False}}
    assert call["inputs"]["apple-runner"]["required"] is True
    assert document["jobs"]["lint"]["runs-on"] == "${{ fromJSON(inputs.lint-runner) }}"
    assert document["jobs"]["xcode"]["runs-on"] == "${{ fromJSON(inputs.apple-runner) }}"
    assert document["jobs"]["required"]["runs-on"] == "${{ fromJSON(vars.CI_LIGHTWEIGHT_RUNNER || (inputs.lint-runner)) }}"
    assert document["jobs"]["required"]["needs"] == ["lint", "xcode"]
    install_swiftlint = next(
        item
        for item in document["jobs"]["lint"]["steps"]
        if item.get("name") == "Install SwiftLint (official Linux release binary)"
    )["run"]
    assert "sha256sum --check --strict" in install_swiftlint
    assert "python3 -m zipfile -e" in install_swiftlint
    assert 'find "$dest" -type f -name swiftlint -print -quit' in install_swiftlint
    assert "/usr/lib/libsourcekitdInProc.so" in install_swiftlint
    assert "LINUX_SOURCEKIT_LIB_PATH=" in install_swiftlint
    swiftlint = next(
        item
        for item in document["jobs"]["lint"]["steps"]
        if item.get("name") == "SwiftLint"
    )["run"]
    assert '${RUNNER_TEMP}/swiftlint-cache' in swiftlint
    assert '--cache-path "$cache_path"' in swiftlint

    validation = step(
        document, "Validate Apple toolchain and gate configuration"
    )["run"]
    assert "|| true" not in validation
    assert "xcodebuild -version" in validation
    assert "swift --version" in validation
    text = WORKFLOW.read_text().lower()
    for forbidden in ("codesign", "keychain", "notarize", "testflight", "sparkle"):
        # Scope comments explain why release/signing is excluded; no command may
        # invoke it.
        assert all(
            forbidden not in str(item.get("run", "")).lower()
            for job in document["jobs"].values()
            for item in job.get("steps", [])
        )

    with tempfile.TemporaryDirectory() as temp:
        fake_bin = Path(temp)
        for command in ("xcodebuild", "swift"):
            path = fake_bin / command
            path.write_text("#!/bin/sh\nprintf '%s\\n' fake-toolchain\n")
            path.chmod(0o755)

        assert run_gate(document, temp).returncode == 0
        for case in (
            {"RUN_BUILD": "false", "RUN_TESTS": "false"},
            {"RUN_BUILD": "true", "BUILD_COMMAND": ""},
            {"RUN_TESTS": "true", "TEST_COMMAND": ""},
        ):
            result = run_gate(document, temp, **case)
            assert result.returncode != 0, f"Apple gate mutation passed: {case}"

    print("Apple gate passed (per-job routing; echo-only and empty commands fail)")

    test_lease(document)


if __name__ == "__main__":
    main()
