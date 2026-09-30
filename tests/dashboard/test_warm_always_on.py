"""computehigh as the default partition and the always-on warm node (v0.48.0)."""

import subprocess
from pathlib import Path

import pytest

from deepresearch.dashboard import lab as labm
from deepresearch.dashboard.lab import Lab, SlurmSSHTarget

WORKER = Path(labm.__file__).parent / "warm_worker.sh"


def _cfg(**extra):
    return {
        "name": "ursa",
        "default_partition": "computehigh",
        "partitions": {"standard": {"cpus": 16}, "computehigh": {"cpus": 22}},
        **extra,
    }


def test_target_default_partition_wins_over_the_catalog_default():
    t = SlurmSSHTarget(_cfg())
    t.catalog = {"partitions": [{"name": "standard", "default": True, "cpus_per_node": 16},
                                {"name": "computehigh", "cpus_per_node": 22}]}  # fmt: skip
    t._apply_catalog_partitions()
    assert t.default_partition == "computehigh"
    # with no choice in lab_targets.json the catalog's default still applies
    t2 = SlurmSSHTarget({k: v for k, v in _cfg().items() if k != "default_partition"})
    t2.catalog = t.catalog
    t2._apply_catalog_partitions()
    assert t2.default_partition == "standard"


def test_always_on_settings_are_read():
    t = SlurmSSHTarget(
        _cfg(warm={"partition": "computehigh", "idle_min": 0, "always_on": True})
    )
    assert t.warm["always_on"] is True and t.warm["idle_min"] == 0
    assert SlurmSSHTarget(_cfg()).warm["always_on"] is False


class FakeWarmTarget:
    name = "ursa"
    label = "Ursa"
    default_partition = "computehigh"
    partitions = {"computehigh": {"cpus": 22}}
    warm = {"partition": "computehigh", "always_on": True, "idle_min": 0}

    def __init__(self):
        self.ensured = 0
        self.stopped = 0

    def ensure_warm(self):
        self.ensured += 1
        return "running:42"

    def warm_stop(self):
        self.stopped += 1

    def warm_status(self):
        return {"workers": []}


@pytest.fixture
def lab(tmp_path):
    t = FakeWarmTarget()
    lb = Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"ursa": t})
    lb.ensure_watcher = lambda: None
    lb.tgt = t  # type: ignore[attr-defined]
    labm._WARM_PAUSED.clear()
    yield lb
    labm._WARM_PAUSED.clear()


def test_keeper_keeps_a_worker_running_until_manually_stopped(lab):
    assert lab.keep_warm_once() == {"ursa": "running:42"} and lab.tgt.ensured == 1
    out = lab.warm_stop()
    assert out["keeper_paused"] is True and lab.tgt.stopped == 1
    assert lab.keep_warm_once() == {} and lab.tgt.ensured == 1  # paused: no restart
    assert lab.warm_status()["keeper_paused"] is True
    lab.warm_start()
    assert lab.tgt.ensured == 2 and lab.keep_warm_once() == {"ursa": "running:42"}


def test_keeper_survives_an_unreachable_cluster(lab):
    def boom():
        raise RuntimeError("ssh 255")

    lab.tgt.ensure_warm = boom
    assert lab.keep_warm_once()["ursa"].startswith("error: ssh 255")


def test_targets_without_always_on_get_no_keeper(tmp_path):
    t = FakeWarmTarget()
    t.warm = dict(t.warm, always_on=False)
    lb = Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"ursa": t})
    assert lb.keep_warm_once() == {}


def _worker(tmp_path, idle_sec: int, secs: float) -> subprocess.CompletedProcess:
    env = {"WARM": str(tmp_path), "IDLE_SEC": str(idle_sec), "POLL": "0.2", "PATH": "/usr/bin:/bin",
           "SLURM_JOB_ID": "7", "WARM_LIMIT_SEC": "3600"}  # fmt: skip
    return subprocess.run(["timeout", str(secs), "bash", str(WORKER)], env=env,
                          capture_output=True, text=True)  # fmt: skip


def test_worker_with_idle_zero_does_not_exit_when_idle(tmp_path):
    p = _worker(tmp_path, 0, 2.5)
    assert p.returncode == 124, p.stdout  # still running when timeout killed it
    assert "exiting" not in p.stdout


def test_worker_with_an_idle_limit_still_exits(tmp_path):
    p = _worker(tmp_path, 1, 10)
    assert p.returncode == 0 and "idle for 1s, exiting" in p.stdout


def test_single_node_cpu_plans_are_pointed_at_the_always_on_warm_partition():
    from deepresearch.dashboard.lab import suggest_partition

    t = SlurmSSHTarget(
        _cfg(warm={"partition": "computehigh", "always_on": True, "idle_min": 0})
    )
    plan = {
        "resources": {"partition": "standard", "nodes": 1, "gpus": 0},
        "script": "python x.py",
    }
    part, why = suggest_partition(t, plan)
    assert part == "computehigh" and "warm node" in why
    # multi-node jobs and targets without an always-on node keep their choice
    assert (
        suggest_partition(t, {**plan, "resources": {**plan["resources"], "nodes": 4}})[
            1
        ]
        == ""
    )
    t2 = SlurmSSHTarget(_cfg())
    assert suggest_partition(t2, plan) == ("standard", "")
