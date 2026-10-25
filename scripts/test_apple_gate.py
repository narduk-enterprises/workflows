#!/usr/bin/env python3
"""Prove Apple routing stays split and the Xcode job cannot become echo-only.

Also proves the agent-infrastructure#965 lease: extracts the "Build and test
under Apple build lease" step's real `run:` text straight out of the
workflow YAML -- like every other scripts/test_*.py in this repo -- and
executes it under bash against a real stand-in lock tool. If someone edits
the embedded lease logic or the steps around it, these tests either still
pass against the new text or they fail; there is no third option where the
test passes while the shipped callable silently stopped leasing.
"""

from __future__ import annotations

import os
import signal
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
import signal
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
        # Same process model as the real run_child: the command leads its own
        # tty-less session, and INT/TERM/HUP are forwarded to its group.
        child = subprocess.Popen(command, start_new_session=True)

        def forward(signum, _frame):
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, forward)
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


LEASED_STEP = "Build and test under Apple build lease"
BUILD_EXPR = "${{ inputs.build-command }}"
TEST_EXPR = "${{ inputs.test-command }}"


def wait_ready(ready: Path) -> None:
    deadline = time.monotonic() + 5
    while not ready.exists():
        if time.monotonic() >= deadline:
            raise AssertionError("lock holder did not signal readiness")
        time.sleep(0.05)


def test_lease(document: dict) -> None:
    """The leased step proves out agent-infrastructure#965 end to end."""
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        lock_tool = write_executable(root / "stub_lock_tool.py", STUB_LOCK_TOOL_SOURCE)
        holder_script = write_executable(root / "holder.py", HOLDER_SOURCE, shebang=False)
        lock_path = root / "apple-build.lock"
        marker = root / "marker.log"
        runner_temp = root / "runner-temp"
        runner_temp.mkdir()

        def env_for(**overrides: str) -> dict:
            return {
                **os.environ,
                "APPLE_BUILD_LOCK_TOOL": str(lock_tool),
                "APPLE_BUILD_LOCK_PATH": str(lock_path),
                "APPLE_BUILD_LEASE_HEARTBEAT_SECONDS": "1",
                "RUNNER_TEMP": str(runner_temp),
                "RUN_BUILD": "true",
                "RUN_TESTS": "true",
                "LEASE_WAIT_MINUTES": "60",
                "WORK_TIMEOUT_MINUTES": "45",
                "DEPENDENCY_SSH_KEY": "",
                "DEPENDENCY_OWNER": "example",
                **overrides,
            }

        def run_leased(env: dict, build: str, test: str, timeout: float = 30):
            return run_named_step(
                document, LEASED_STEP, env, timeout=timeout,
                substitutions={BUILD_EXPR: build, TEST_EXPR: test},
            )

        def hold(seconds: str, tag: str) -> subprocess.Popen:
            ready = root / f"holder-ready-{tag}"
            holder = subprocess.Popen(
                [sys.executable, str(holder_script), str(lock_path), str(ready), seconds]
            )
            wait_ready(ready)
            return holder

        build_ok = f'echo build >> "{marker}"'
        test_ok = f'echo test >> "{marker}"'

        # 1. Refuses to run unleased when the lease tool is not installed.
        result = run_leased(env_for(APPLE_BUILD_LOCK_TOOL=str(root / "absent.py")), build_ok, test_ok)
        assert result.returncode == 1, result.stderr
        assert "refusing to run build-command/test-command unleased" in result.stdout
        assert not marker.exists()

        # 2. Free lease: no queue evidence; build then test run under ONE lease.
        result = run_leased(env_for(), build_ok, test_ok)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "QUEUED" not in result.stdout
        assert result.stdout.count("LEASE ACQUIRED") == 1
        assert "RUNNING UNDER LEASE: build-command" in result.stdout
        assert "RUNNING UNDER LEASE: test-command" in result.stdout
        assert marker.read_text() == "build\ntest\n"
        marker.unlink()

        # 3. run-tests false stages only the build command.
        result = run_leased(env_for(RUN_TESTS="false"), build_ok, test_ok)
        assert result.returncode == 0, result.stderr
        assert marker.read_text() == "build\n"
        marker.unlink()

        # 4. Busy-then-free: one QUEUED line, a heartbeat, then acquisition.
        #    The queue (3 s) uses the whole work budget (3 s) and 2 s of work
        #    still succeeds: queue time never eats the build's own budget.
        holder = hold("3", "waits")
        try:
            result = run_leased(env_for(APPLE_BUILD_WORK_TIMEOUT_SECONDS="3"), f"sleep 2; {build_ok}", test_ok)
        finally:
            holder.wait(timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.count("QUEUED: another Apple build holds") == 1
        assert "QUEUED: still waiting" in result.stdout
        assert "LEASE ACQUIRED after" in result.stdout
        assert len(result.stdout.splitlines()) < 12, result.stdout
        assert marker.read_text() == "build\ntest\n"
        marker.unlink()

        # 5. Queue budget exhausted: fails visibly with 75, never runs work.
        holder = hold("30", "timeout")
        try:
            result = run_leased(env_for(APPLE_BUILD_LEASE_TIMEOUT_SECONDS="2"), build_ok, test_ok, timeout=15)
        finally:
            holder.kill()
            holder.wait(timeout=10)
        assert result.returncode == 75, result.stderr
        assert "still busy after 2s" in result.stdout
        assert "refusing to run unleased" in result.stdout
        assert not marker.exists()

        # 6. Work budget exhausted after acquisition: the whole command tree is
        #    stopped, the step exits 124 and test-command never runs.
        pid_file = root / "sleeper.pid"
        hung = f'sleep 60 & echo $! > "{pid_file}"; wait'
        started = time.monotonic()
        result = run_leased(env_for(APPLE_BUILD_WORK_TIMEOUT_SECONDS="2"), hung, test_ok, timeout=20)
        assert result.returncode == 124, result.stdout + result.stderr
        assert time.monotonic() - started < 15
        assert "exceeded the 2s work budget" in result.stdout
        assert not marker.exists()
        sleeper = int(pid_file.read_text())
        time.sleep(0.2)
        try:
            os.kill(sleeper, 0)
            raise AssertionError("work-budget timeout left the build's child running")
        except ProcessLookupError:
            pass

        # 6b. Cancellation: the runner signals the lock tool, which forwards to
        #     the leased script; the whole build tree stops and nothing leaks.
        pid_file.unlink()
        process = subprocess.Popen(
            ["bash", "-c", step(document, LEASED_STEP)["run"].replace(BUILD_EXPR, hung).replace(TEST_EXPR, test_ok)],
            env=env_for(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.monotonic() + 10
        while not pid_file.exists() or not pid_file.read_text().strip():
            if time.monotonic() >= deadline:
                raise AssertionError("build-command never started")
            time.sleep(0.05)
        tool = subprocess.run(["pgrep", "-f", str(lock_tool)], capture_output=True, text=True).stdout.split()
        assert len(tool) == 1, tool
        os.kill(int(tool[0]), signal.SIGTERM)
        output, _ = process.communicate(timeout=15)
        assert process.returncode != 0, output
        sleeper = int(pid_file.read_text())
        time.sleep(0.2)
        try:
            os.kill(sleeper, 0)
            raise AssertionError("cancellation left the build's child running")
        except ProcessLookupError:
            pass
        assert not marker.exists(), output

        # 7. A failing build-command forwards its exit code and skips tests.
        result = run_leased(env_for(), "exit 17", test_ok)
        assert result.returncode == 17, result.stderr
        assert not marker.exists()

        # 8. The caller's command text is staged verbatim, not re-parsed.
        result = run_leased(env_for(RUN_TESTS="false"), f"echo \"quoted $HOME\" '$X' >> \"{marker}\"", test_ok)
        assert result.returncode == 0, result.stderr
        assert marker.read_text() == f"quoted {os.environ['HOME']} $X\n"
        marker.unlink()

        assert not list(runner_temp.iterdir()), "leased step left its stage directory behind"

    print("Apple lease queue/work-budget/timeout contract passed (agent-infrastructure#965)")


def test_dependency_auth(document: dict) -> None:
    # Execute the shipped step and both successful/failing child exits.
    # Verify HTTPS rewriting is owner-scoped and the temporary key disappears.
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        lock_tool = write_executable(root / "stub_lock_tool.py", STUB_LOCK_TOOL_SOURCE)
        runner_temp = root / "runner-temp"
        runner_temp.mkdir()
        credential = "fake-read-only-deploy-key"
        for value in ("", credential):
            for code in (0, 17):
                command = "git config --get url.git@github.com:example/.insteadOf || :\n"
                if value:
                    command += 'key="$(dirname "$0")/swiftpm-auth/key"\ntest "$(cat "$key")" = "fake-read-only-deploy-key"\npython3 -c \'import os,stat,sys; assert stat.S_IMODE(os.stat(sys.argv[1]).st_mode) == 0o600\' "$key"\n'
                command += f"exit {code}"
                env = {**os.environ, "HOME": temp, "RUNNER_TEMP": str(runner_temp),
                       "GIT_CONFIG_GLOBAL": str(root / "global"), "GIT_CONFIG_NOSYSTEM": "1",
                       "APPLE_BUILD_LOCK_TOOL": str(lock_tool),
                       "APPLE_BUILD_LOCK_PATH": str(root / "apple-build.lock"),
                       "RUN_BUILD": "true", "RUN_TESTS": "false",
                       "LEASE_WAIT_MINUTES": "1", "WORK_TIMEOUT_MINUTES": "1",
                       "DEPENDENCY_OWNER": "example", "DEPENDENCY_SSH_KEY": value}
                for key in list(env):
                    if key.startswith("GIT_CONFIG_") and key not in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM"):
                        del env[key]
                result = run_named_step(document, LEASED_STEP, env, substitutions={BUILD_EXPR: command})
                assert result.returncode == code, result.stdout + result.stderr
                assert credential not in result.stdout + result.stderr
                rewrite = "https://github.com/example/\n"
                assert (rewrite in result.stdout) == bool(value), result.stdout
                assert not list(runner_temp.iterdir())
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
    # Empty lint-runner resolves by caller visibility (row 18 Q3); behaviour is
    # evaluated in scripts/test_runner_default.py.
    lint_default = 'inputs.lint-runner || github.event.repository.private == true && \'{"group":"linux-ci","labels":["self-hosted","Linux","X64","proxmox","linux-ci"]}\' || \'"ubuntu-latest"\''
    assert document["jobs"]["lint"]["runs-on"] == "${{ fromJSON(" + lint_default + ") }}"
    assert document["jobs"]["xcode"]["runs-on"] == "${{ fromJSON(inputs.apple-runner) }}"
    assert document["jobs"]["required"]["runs-on"] == "${{ fromJSON(vars.CI_LIGHTWEIGHT_RUNNER || (" + lint_default + ")) }}"
    assert document["jobs"]["required"]["needs"] == ["lint", "xcode", "ci-admission"]
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
