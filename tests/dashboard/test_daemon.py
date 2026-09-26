"""Daemon start/stop/restart against a real detached process on a free port."""

import socket

import pytest

from deepresearch.dashboard import daemon


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(daemon, "PID_FILE", tmp_path / "dashboard.pid")
    monkeypatch.setattr(daemon, "LOG_FILE", tmp_path / "logs" / "dashboard.log")
    yield tmp_path
    daemon.stop()


def test_start_status_restart_stop(isolated, capsys):
    port = _free_port()
    assert daemon.status() == 3
    assert daemon.start("127.0.0.1", port) == 0
    first = daemon.read_state()
    assert first and first["port"] == port
    assert daemon._probe("127.0.0.1", port)["ok"] is True
    # second start is a no-op
    assert daemon.start("127.0.0.1", port) == 0
    assert daemon.read_state()["pid"] == first["pid"]
    assert daemon.status() == 0
    # restart keeps host/port, new pid
    assert daemon.restart() == 0
    second = daemon.read_state()
    assert second["pid"] != first["pid"] and second["port"] == port
    assert daemon.stop() == 0
    assert daemon.read_state() is None
    assert daemon._probe("127.0.0.1", port) is None
    out = capsys.readouterr().out
    assert "Dashboard started" in out and "Dashboard stopped" in out


def test_stop_when_not_running(isolated, capsys):
    assert daemon.stop() == 0
    assert "not running" in capsys.readouterr().out


def test_stale_pid_file_is_ignored(isolated):
    daemon.PID_FILE.write_text('{"pid": 999999, "host": "127.0.0.1", "port": 1}')
    assert daemon.read_state() is None
