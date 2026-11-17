#!/usr/bin/env python3
#
# tests/test_docker_contract.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Contract between the Compose files, the Dockerfile, the watchdog entrypoint and the shutdown budget."""
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.fritzbox.handler import SHUTDOWN_DRAIN_TIMEOUT
from app.fritzbox.tr064.deadline import MAX_REQUEST_SECONDS
from app.influxdb.handler import BUFFER_FLUSH_TIMEOUT
from app.main import QUEUE_DRAIN_TIMEOUT, SHUTDOWN_RESERVE

DOCKER_DIR = Path(__file__).parents[1] / "docker"
ENTRYPOINT = DOCKER_DIR / "entrypoint.sh"
COMPOSE_FILES = sorted(DOCKER_DIR.glob("docker-compose.*.yml"))
PID_FILE = Path("/tmp/fritzfluxdb.pid")

needs_bash_and_setsid = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("setsid") is None, reason="bash and setsid are required"
)


@pytest.fixture
def daemon_files(tmp_path, monkeypatch):
    """Point the watchdog at private PID and stop-flag files, so parallel suite runs cannot collide."""
    pid_file, stop_flag = tmp_path / "fritzfluxdb.pid", tmp_path / "fritzfluxdb.stop"
    monkeypatch.setenv("FRITZFLUXDB_PID_FILE", str(pid_file))
    monkeypatch.setenv("FRITZFLUXDB_STOP_FLAG", str(stop_flag))
    return pid_file, stop_flag


def _healthcheck_test(compose: Path) -> str:
    match = re.search(r"^\s+test: (\[.*\])$", compose.read_text(), re.MULTILINE)
    assert match, f"no healthcheck test in {compose.name}"
    return match.group(1)


def _watchdog_default() -> int:
    match = re.search(r'WATCHDOG_SHUTDOWN_TIMEOUT:-(\d+)\}', ENTRYPOINT.read_text())
    assert match
    return int(match.group(1))


def test_there_are_three_compose_variants():
    assert [f.name for f in COMPOSE_FILES] == [
        "docker-compose.influx1.yml", "docker-compose.influx2.yml", "docker-compose.questdb.yml",
    ]


def test_healthchecks_are_identical_and_probe_the_daemon_pid():
    checks = {_healthcheck_test(f) for f in COMPOSE_FILES}
    assert len(checks) == 1
    check = checks.pop()
    assert "pgrep" not in check
    assert str(PID_FILE) in check
    assert f"{{FRITZFLUXDB_PID_FILE:-{PID_FILE}}}" in ENTRYPOINT.read_text()


def test_healthcheck_follows_the_daemon_pid(tmp_path):
    pid_file = tmp_path / "daemon.pid"
    command = _healthcheck_test(COMPOSE_FILES[0])
    # the Compose YAML escapes `$` as `$$`
    command = command.replace("$$", "$").replace(str(PID_FILE), str(pid_file))
    command = re.search(r'"CMD-SHELL", "(.*)"\]$', command).group(1).replace('\\"', '"')

    def healthy() -> bool:
        return subprocess.run(["sh", "-c", command], check=False, timeout=10).returncode == 0

    assert not healthy()  # no PID file
    daemon = subprocess.Popen(["sleep", "30"])
    try:
        pid_file.write_text(f"{daemon.pid}\n")
        assert healthy()
    finally:
        daemon.kill()
        daemon.wait()
    assert not healthy()  # dead daemon, stale PID file


def test_questdb_is_not_published_on_the_host():
    compose = (DOCKER_DIR / "docker-compose.questdb.yml").read_text()
    assert not re.search(r"^\s*ports:", compose, re.MULTILINE)
    assert not re.search(r'^\s*-\s*"?\d+:\d+', compose, re.MULTILINE)


def test_shutdown_budget_is_ordered():
    app_budget = SHUTDOWN_DRAIN_TIMEOUT + QUEUE_DRAIN_TIMEOUT + BUFFER_FLUSH_TIMEOUT + SHUTDOWN_RESERVE
    dockerfile = re.search(r"WATCHDOG_SHUTDOWN_TIMEOUT=(\d+)", (DOCKER_DIR / "Dockerfile").read_text())
    assert dockerfile
    assert int(dockerfile.group(1)) == _watchdog_default()
    assert app_budget < _watchdog_default()
    # A request started just before the signal ends within twice its deadline (see deadline.py).
    assert 2 * MAX_REQUEST_SECONDS + SHUTDOWN_RESERVE < _watchdog_default()
    for compose in COMPOSE_FILES:
        grace = re.search(r"stop_grace_period: (\d+)s", compose.read_text())
        assert grace, compose.name
        assert _watchdog_default() < int(grace.group(1)), compose.name


@needs_bash_and_setsid
def test_watchdog_enforces_process_deadline_after_python_cleanup_with_a_hung_thread(tmp_path, daemon_files):
    pid_file, _ = daemon_files
    ready, cleaned = tmp_path / "ready", tmp_path / "cleaned"
    script = tmp_path / "hung.py"
    script.write_text('''import asyncio, signal, sys, threading
from pathlib import Path
async def run():
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    work = asyncio.create_task(asyncio.to_thread(threading.Event().wait))
    Path(sys.argv[1]).touch()
    await stop.wait()
    work.cancel()
    await asyncio.gather(work, return_exceptions=True)
    Path(sys.argv[2]).touch()
asyncio.run(run())
''')
    proc = subprocess.Popen(
        ["bash", str(ENTRYPOINT), sys.executable, str(script), str(ready), str(cleaned)],
        env={**os.environ, "WATCHDOG_SHUTDOWN_TIMEOUT": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "child did not start before the process-test deadline"
        child_pid = int(pid_file.read_text().strip())
        proc.send_signal(signal.SIGTERM)
        output, _ = proc.communicate(timeout=6)
        assert proc.returncode == 0
        assert cleaned.exists(), "Python cleanup must run before watchdog escalation"
        assert "sending SIGKILL" in output
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _run_entrypoint(env_overrides: dict[str, str], *command: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_overrides}
    return subprocess.run(
        ["bash", str(ENTRYPOINT), *command], env=env, capture_output=True, text=True, timeout=30, check=False
    )


@needs_bash_and_setsid
@pytest.mark.parametrize("value", ["0", "00", "000", "-1", "1x"])
def test_watchdog_rejects_non_positive_values(value, daemon_files):
    result = _run_entrypoint({"WATCHDOG_RESTART_DELAY": value}, "true")
    assert result.returncode == 2
    assert "Invalid WATCHDOG_RESTART_DELAY" in result.stderr


@needs_bash_and_setsid
def test_watchdog_reads_leading_zeros_as_decimal(daemon_files):
    # "010" would be octal 8 in shell arithmetic; the max check proves it is read as 10
    result = _run_entrypoint({"WATCHDOG_RESTART_DELAY": "010", "WATCHDOG_MAX_RESTART_DELAY": "9"}, "true")
    assert result.returncode == 0
    assert "delay=9s" in result.stdout


def _start_parked_watchdog(tmp_path: Path, pid_file: Path) -> subprocess.Popen:
    """Start a watchdog whose daemon exits 78 (each start appended to a file) and wait until it is parked."""
    counter = tmp_path / "starts"
    pid_file.unlink(missing_ok=True)
    watchdog = subprocess.Popen(
        ["bash", str(ENTRYPOINT), "sh", "-c", f'echo x >> "{counter}"; exit 78'],
        stdout=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 10
    while watchdog.poll() is None and time.monotonic() < deadline:
        if counter.exists() and not pid_file.exists():
            time.sleep(0.3)  # let the watchdog reach the park loop
            break
        time.sleep(0.05)
    return watchdog


@needs_bash_and_setsid
def test_watchdog_parks_on_a_configuration_error(tmp_path, daemon_files):
    pid_file, _ = daemon_files
    watchdog = _start_parked_watchdog(tmp_path, pid_file)
    try:
        time.sleep(1.5)  # a restart would show up as a second daemon start
        assert watchdog.poll() is None  # alive, so the restart policy does not loop
        assert (tmp_path / "starts").read_text().count("x") == 1
        assert not pid_file.exists()  # healthcheck reports unhealthy
    finally:
        watchdog.terminate()
        out, _ = watchdog.communicate(timeout=10)
    assert out.count("launching daemon") == 1
    assert out.count("permanent configuration error") == 1
    assert "restart 1/" not in out


@needs_bash_and_setsid
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_parked_watchdog_stops_quickly_and_cleanly_on_signal(tmp_path, daemon_files, sig):
    pid_file, _ = daemon_files
    watchdog = _start_parked_watchdog(tmp_path, pid_file)
    try:
        assert watchdog.poll() is None
        children = subprocess.run(
            ["pgrep", "-P", str(watchdog.pid)], capture_output=True, text=True, check=False, timeout=10
        ).stdout.split()
        watchdog.send_signal(sig)
        started = time.monotonic()
        watchdog.communicate(timeout=10)
        elapsed = time.monotonic() - started
    finally:
        # A failed check must not leave the watchdog and its parked loop running after the test.
        if watchdog.poll() is None:
            watchdog.kill()
            watchdog.communicate()
    assert elapsed < 5
    assert watchdog.returncode == 0
    assert not pid_file.exists()
    for child in children:
        assert not Path(f"/proc/{child}").exists() or Path(f"/proc/{child}/stat").read_text().split()[2] == "Z"


@needs_bash_and_setsid
def test_watchdog_keeps_backoff_for_other_failures(daemon_files):
    result = _run_entrypoint({"WATCHDOG_RESTART_DELAY": "1", "WATCHDOG_MAX_RESTARTS": "2"}, "sh", "-c", "exit 1")
    assert result.returncode == 1
    assert result.stdout.count("launching daemon") == 2
    assert "restart 1/2 in 1s" in result.stdout


@needs_bash_and_setsid
def test_pid_file_disappears_when_the_daemon_dies_while_the_watchdog_lives(daemon_files):
    pid_file, _ = daemon_files
    env = {
        **os.environ,
        "WATCHDOG_RESTART_DELAY": "5",
    }
    pid_file.unlink(missing_ok=True)
    watchdog = subprocess.Popen(
        ["bash", str(ENTRYPOINT), "sh", "-c", "sleep 1; exit 1"],
        env=env, stdout=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        daemon_pid = int(pid_file.read_text())
        os.kill(daemon_pid, 0)  # daemon alive while it is registered

        while pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not pid_file.exists()
        assert watchdog.poll() is None  # the watchdog itself keeps running during the restart delay
    finally:
        # bash defers its TERM trap until the restart-delay sleep ends, so do not wait for it
        watchdog.kill()
        watchdog.wait(timeout=30)
    assert not pid_file.exists()


@needs_bash_and_setsid
@pytest.mark.parametrize("daemon_exit", ["exit 0", "kill -TERM $$"])
def test_watchdog_parks_without_restarting_when_the_cli_stopped_the_daemon(tmp_path, daemon_files, daemon_exit):
    pid_file, stop_flag = daemon_files
    # The real daemon exits 0 after its own SIGTERM handling; a raw signal exit gives 143.
    counter = tmp_path / "starts"
    pid_file.unlink(missing_ok=True)
    watchdog = subprocess.Popen(
        ["bash", str(ENTRYPOINT), "sh", "-c", f'echo x >> "{counter}"; touch "{stop_flag}"; {daemon_exit}'],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        time.sleep(2)
        assert watchdog.poll() is None
        assert counter.read_text().count("x") == 1
        assert not stop_flag.exists()
    finally:
        watchdog.send_signal(signal.SIGTERM)
        output, _ = watchdog.communicate(timeout=10)
    assert "stopped by fritzflux-cli; parked" in output
    assert watchdog.returncode == 0
