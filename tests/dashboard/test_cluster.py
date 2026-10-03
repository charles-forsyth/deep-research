"""The cluster layer on its own (v0.52.0, K20): `dashboard/cluster.py` without the Lab,
the database or a model. A fake `ssh` (subprocess.run) answers each command, so these
tests pin the shell commands and the parsing of their output."""

import base64
import io
import subprocess
import tarfile
import threading

import pytest

from deepresearch.dashboard import cluster as C

CFG = {
    "name": "ursa-major",
    "label": "Ursa Major",
    "ssh_host": "ursa",
    "remote_root": "~/deep-research-lab",
    "partitions": {"computehigh": {"cpus": 88}, "standard": {"cpus": 60}},
    "default_partition": "computehigh",
}


class FakeSSH:
    """Records every remote command; replies from a list of (match, rc, stdout)."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.commands: list[str] = []

    def __call__(self, argv, input=None, capture_output=True, timeout=None, **kw):
        cmd = argv[-1]
        self.commands.append(cmd)
        for i, (match, rc, out) in enumerate(self.replies):
            if match in cmd:
                self.replies.pop(i)
                return subprocess.CompletedProcess(argv, rc, out, b"err text")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


@pytest.fixture
def target():
    t = C.SlurmSSHTarget(dict(CFG))
    t._argv, t._dest = ["ssh"], "ursa"  # skip connection setup
    return t


def test_config_and_defaults():
    t = C.SlurmSSHTarget(dict(CFG))
    assert t.name == "ursa-major" and t.label == "Ursa Major"
    assert (
        t.default_partition == "computehigh"
        and t.job_dir(7) == "~/deep-research-lab/run_7"
    )
    bare = C.SlurmSSHTarget({"name": "x", "partitions": {"a": {}}})
    assert bare.default_partition == "a" and bare.remote_root == "~/deep-research-lab"


def test_plain_ssh_host_gets_a_shared_control_connection(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    t = C.SlurmSSHTarget(dict(CFG))
    argv, dest = t._base()
    assert dest == "ursa" and argv[0] == "ssh"
    joined = " ".join(argv)
    assert "ControlMaster=auto" in joined and "BatchMode=yes" in joined
    assert f"ControlPath={tmp_path}/dr-lab-%C" in joined
    assert t._base()[0] is argv  # built once, reused


def test_ssh_exit_255_means_the_command_never_ran(target, monkeypatch):
    fake = FakeSSH([("true", 255, b"")])
    monkeypatch.setattr(C.subprocess, "run", fake)
    with pytest.raises(C.NotSubmitted, match="cannot reach the cluster"):
        target.run("true")
    assert target._argv is None  # rebuilt next time (expired token or tunnel)


def test_timeout_is_a_target_error_not_a_hang(target, monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("ssh", 5)

    monkeypatch.setattr(C.subprocess, "run", slow)
    with pytest.raises(C.TargetError, match="timed out after 5s"):
        target.run("sleep 99", timeout=5)


def test_sh_raises_with_the_remote_error(target, monkeypatch):
    monkeypatch.setattr(C.subprocess, "run", FakeSSH([("scancel", 1, b"")]))
    with pytest.raises(C.TargetError, match="scancel 42"):
        target.cancel("42")


def test_expired_gcloud_login_is_named_plainly():
    msg = C._gcloud_error(
        "ERROR: Reauthentication failed. cannot prompt during non-interactive"
    )
    assert msg == C.GCLOUD_LOGIN_HINT


def test_status_parses_one_round_trip(target, monkeypatch):
    out = (
        b"COMPLETED|00:04:10|c3-node-1|0:0|2026-10-01T10:00:00\n"
        b"::SQ::\n::NF::\n2\n::ST::\nRunning\n::SZ::\n12345\n"
    )
    fake = FakeSSH([("sacct -j 260", 0, out)])
    monkeypatch.setattr(C.subprocess, "run", fake)
    st = target.status(5, "260")
    assert st["slurm_state"] == "COMPLETED" and st["elapsed"] == "00:04:10"
    assert st["node"] == "c3-node-1" and st["exit_code"] == "0:0"
    assert (
        st["stage"] == "Running" and st["log_size"] == 12345 and st["node_fails"] == 2
    )
    assert "~/deep-research-lab/run_5/stage.txt" in fake.commands[0]


def test_status_prefers_the_live_queue_line(target, monkeypatch):
    out = b"\n::SQ::\nPENDING|Resources|0:00|\n::NF::\n0\n::ST::\n\n::SZ::\n0\n"
    monkeypatch.setattr(C.subprocess, "run", FakeSSH([("sacct", 0, out)]))
    st = target.status(5, "261")
    assert st["slurm_state"] == "PENDING" and st["reason"] == "Resources"


def test_warm_jobs_are_cancelled_on_the_spool_not_with_scancel(target, monkeypatch):
    fake = FakeSSH()
    monkeypatch.setattr(C.subprocess, "run", fake)
    target.cfg["warm"] = {"partition": "computehigh"}
    target.cancel("warm:full-5")
    assert fake.commands and "scancel" not in fake.commands[0]
    assert "full-5" in fake.commands[0]


def test_warm_task_reads_where_a_task_is(target, monkeypatch):
    out = b"where=done\nrc=0\nstarted=100\nfinished=160\nnode=c3-1\nnow=170\n"
    monkeypatch.setattr(
        C.subprocess, "run", FakeSSH([("for d in done running", 0, out)])
    )
    target.cfg["warm"] = {"partition": "computehigh"}
    t = target.warm_task("smoke-5-1")
    assert t == {"where": "done", "rc": 0, "started": 100, "finished": 160,
                 "node": "c3-1", "now": 170}  # fmt: skip
    monkeypatch.setattr(C.subprocess, "run", FakeSSH([("for d in", 0, b"")]))
    assert target.warm_task("gone")["where"] == "missing"


def test_warm_status_reports_the_idle_setting(target, monkeypatch):
    """The Lab page said "stops after 20 idle minutes" whatever the setting was."""
    monkeypatch.setattr(
        C.subprocess, "run", FakeSSH([("::SQ::", 0, b"::SQ::\n::HB::\n::Q::\n0\n0\n")])
    )
    target.cfg["warm"] = {"partition": "computehigh", "idle_min": 60}
    assert target.warm_status()["idle_min"] == 60
    monkeypatch.setattr(
        C.subprocess, "run", FakeSSH([("::SQ::", 0, b"::SQ::\n::HB::\n::Q::\n0\n0\n")])
    )
    target.cfg["warm"] = {"partition": "computehigh"}
    assert target.warm_status()["idle_min"] == 20


def test_warm_banner_reads_the_setting_not_a_fixed_number():
    from pathlib import Path

    js = (Path(C.__file__).parent / "static" / "lab.js").read_text()
    assert "stops after 20 idle minutes" not in js and "w.idle_min" in js


def _tgz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_fetch_copies_outputs_and_skips_what_is_too_big(target, monkeypatch, tmp_path):
    listing = (
        b"5\toutputs/verdict.json\n4\tjob.log\n"
        + str(C.MAX_FILE_BYTES + 1).encode()
        + b"\toutputs/huge.bin\n"
        b"3\t../escape.txt\n"
    )
    tgz = _tgz({"outputs/verdict.json": b"{}", "job.log": b"done", "../evil": b"x"})
    fake = FakeSSH([("find outputs", 0, listing), ("tar czf", 0, tgz)])
    monkeypatch.setattr(C.subprocess, "run", fake)
    files = target.fetch(5, tmp_path / "run_5")
    got = {f["path"]: f for f in files}
    assert (tmp_path / "run_5/outputs/verdict.json").read_bytes() == b"{}"
    assert got["outputs/huge.bin"]["skipped"] is True
    assert "../escape.txt" not in got and "../evil" not in got
    assert not (tmp_path / "evil").exists()
    assert "huge.bin" not in fake.commands[1]  # never transferred


def test_upload_sends_a_tarball_into_the_run_folder(target, monkeypatch):
    seen = {}

    def run(argv, input=None, **k):
        seen["cmd"], seen["stdin"] = argv[-1], input
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(C.subprocess, "run", run)
    target.upload(9, {"run.sbatch": "#!/bin/bash\necho hi\n", "plan.json": "{}"})
    assert "~/deep-research-lab/run_9" in seen["cmd"] and "tar xzf" in seen["cmd"]
    with tarfile.open(
        fileobj=io.BytesIO(base64.b64decode(seen["stdin"])), mode="r:gz"
    ) as t:
        names = {m.name: m.mode for m in t.getmembers()}
    assert names == {"run.sbatch": 0o755, "plan.json": 0o644}


def test_a_workspace_view_never_touches_mains_run_folders(target, monkeypatch):
    ws = C.ScopedTarget(target, "ws-demo")
    assert ws.job_dir(1) == "~/deep-research-lab/ws-demo/run_1" != target.job_dir(1)
    assert C.task_prefix(ws) == "demo-" and C.task_prefix(target) == ""
    fake = FakeSSH([("tail -c", 0, b"hello")])
    monkeypatch.setattr(C.subprocess, "run", fake)
    assert ws.read_file(1, "job.log") == "hello"
    assert "ws-demo/run_1" in fake.commands[0]
    assert ws.base is target and ws.name == "ursa-major"  # shares connection and config


def test_load_targets(tmp_path):
    assert C.load_targets(tmp_path) == {}
    (tmp_path / "lab_targets.json").write_text(
        '{"targets": [{"name": "a", "ssh_host": "h"}, {"name": "b", "type": "other"}]}'
    )
    got = C.load_targets(tmp_path)
    assert list(got) == ["a"] and isinstance(got["a"], C.SlurmSSHTarget)


def test_lab_still_exports_the_cluster_names():
    """Callers and older code import these from lab.py; they are the same objects."""
    from deepresearch.dashboard import lab

    for name in ("SlurmSSHTarget", "ScopedTarget", "TargetError", "NotSubmitted",
                 "load_targets", "task_prefix", "GCLOUD_LOGIN_HINT"):  # fmt: skip
        assert getattr(lab, name) is getattr(C, name)


def test_the_cluster_module_has_no_model_or_database_code():
    src = (C.__file__ and open(C.__file__).read()) or ""
    for word in ("sqlite3", "genai", "generate_content", "lab_runs"):
        assert word not in src, word
    assert isinstance(C.SlurmSSHTarget(dict(CFG))._lock, type(threading.Lock()))
