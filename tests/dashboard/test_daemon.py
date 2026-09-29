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


def test_children_run_isolated_from_cwd():
    """A shadow ./deepresearch in the cwd must not be imported by children."""
    from deepresearch.dashboard.server import _cli_cmd

    assert _cli_cmd()[1] == "-I"


def test_start_from_dir_with_shadow_package(isolated, tmp_path, monkeypatch):
    shadow = tmp_path / "cwd" / "deepresearch"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("raise SystemExit('shadow package imported')\n")
    monkeypatch.chdir(tmp_path / "cwd")
    port = _free_port()
    assert daemon.start("127.0.0.1", port) == 0
    assert daemon._probe("127.0.0.1", port)["ok"] is True


def test_start_refuses_non_loopback_without_allow_remote(isolated, capsys):
    assert daemon.start("0.0.0.0", _free_port()) == 2
    assert daemon.read_state() is None
    assert "--allow-remote" in capsys.readouterr().out
    assert daemon.is_loopback("127.0.0.1") and daemon.is_loopback("::1")
    assert daemon.is_loopback("localhost") and not daemon.is_loopback("192.168.1.35")


def test_restart_brings_an_old_remote_dashboard_back_local(
    isolated, monkeypatch, capsys
):
    calls = []
    monkeypatch.setattr(
        daemon, "read_state", lambda: {"pid": 1, "host": "0.0.0.0", "port": 7420}
    )
    monkeypatch.setattr(daemon, "stop", lambda: 0)
    monkeypatch.setattr(
        daemon, "start", lambda h, p, r=False: calls.append((h, p, r)) or 0
    )
    assert daemon.restart() == 0
    assert calls == [("127.0.0.1", 7420, False)]
    assert "restarting on 127.0.0.1" in capsys.readouterr().out
    calls.clear()
    assert daemon.restart("0.0.0.0", None, True) == 0
    assert calls == [("0.0.0.0", 7420, True)]
