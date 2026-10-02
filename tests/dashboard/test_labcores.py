"""Cores on shared partitions (v0.52.1, labcores).

On a shared Slurm partition a job that asks for nothing gets one core, and the cgroup
holds it there. Every Lab job on a shared partition must therefore ask for its cores;
whole-node partitions (highmem, gpul4) keep whole nodes.
"""

import json

import pytest

from deepresearch.dashboard import labcores
from deepresearch.dashboard import lab as labm
from deepresearch.dashboard.lab import build_sbatch, estimate_cost

PLAN = {
    "title": "t",
    "script": "python3 x.py",
    "resources": {"partition": "computehigh", "nodes": 1, "time_limit": "01:00:00"},
    "install": {"modules": [], "conda": [], "pip": []},
}


def target(exclusive=None, cfg_extra=None):
    """An Ursa-shaped target; `exclusive` adds the catalog flag to the shared ones."""
    parts = {
        "standard": {"cpus": 16, "mem_gb": 124, "usd_per_hour": 1.40},
        "computehigh": {"cpus": 22, "mem_gb": 85, "usd_per_hour": 1.87},
        "highmem": {"cpus": 32, "mem_gb": 497, "usd_per_hour": 4.19},
        "gpul4": {"cpus": 8, "mem_gb": 62, "gpus": 1, "usd_per_hour": 1.15},
        "spot": {"cpus": 16, "mem_gb": 124, "usd_per_hour": 0.74, "spot": True},
    }
    if exclusive is not None:
        for name, p in parts.items():
            p["exclusive"] = name in ("highmem", "gpul4") or exclusive
    t = labm.SlurmSSHTarget(
        {
            "name": "u",
            "ssh_host": "h",
            "partitions": parts,
            "default_partition": "computehigh",
            **(cfg_extra or {}),
        }
    )
    return t


def plan(**res):
    return {**PLAN, "resources": {**PLAN["resources"], **res}}


def sbatch_header(s):
    return [ln for ln in s.splitlines() if ln.startswith("#SBATCH")]


# ---- what the job asks for -----------------------------------------------------


def test_no_cores_defaults_to_two_on_a_shared_partition():
    s = build_sbatch(1, plan(), target())
    h = sbatch_header(s)
    assert "#SBATCH --ntasks-per-node=1" in h and "#SBATCH --cpus-per-task=2" in h
    assert "#SBATCH --exclusive" not in h


def test_cores_are_requested_explicitly():
    h = sbatch_header(build_sbatch(1, plan(cores=8), target()))
    assert "#SBATCH --cpus-per-task=8" in h and "#SBATCH --ntasks-per-node=1" in h
    assert not any(x.startswith("#SBATCH --mem") for x in h)  # memory follows cores


def test_cores_are_capped_at_the_node():
    h = sbatch_header(build_sbatch(1, plan(cores=64), target()))
    assert "#SBATCH --cpus-per-task=22" in h


def test_invalid_cores_fall_back_to_the_default():
    for bad in ("all", "8; rm -rf ~", -3, 0, 2.5, None, "x\n#SBATCH --x"):
        s = build_sbatch(1, plan(cores=bad), target())
        assert "#SBATCH --cpus-per-task=2" in s, bad
        assert "rm -rf" not in s and "--x" not in s


def test_mpi_ranks_are_the_core_request():
    h = sbatch_header(build_sbatch(1, plan(nodes=2, ntasks_per_node=16), target()))
    assert "#SBATCH --ntasks-per-node=16" in h and "#SBATCH --cpus-per-task=1" in h
    assert "#SBATCH --nodes=2" in h


def test_hybrid_mpi_threads_when_they_fit_the_node():
    h = sbatch_header(build_sbatch(1, plan(ntasks_per_node=4, cores=5), target()))
    assert "#SBATCH --ntasks-per-node=4" in h and "#SBATCH --cpus-per-task=5" in h
    # 16 ranks x 4 threads does not fit 22 cores: one core per rank
    h = sbatch_header(build_sbatch(1, plan(ntasks_per_node=16, cores=4), target()))
    assert "#SBATCH --ntasks-per-node=16" in h and "#SBATCH --cpus-per-task=1" in h


def test_single_rank_is_a_task_sized_by_cores():
    # older plans set ntasks_per_node 1 for serial work (34 of 102 real plans)
    h = sbatch_header(build_sbatch(1, plan(ntasks_per_node=1, cores=8), target()))
    assert "#SBATCH --ntasks-per-node=1" in h and "#SBATCH --cpus-per-task=8" in h
    h = sbatch_header(build_sbatch(1, plan(ntasks_per_node=1), target()))
    assert "#SBATCH --cpus-per-task=2" in h
    w = labcores.warnings(target(), plan(ntasks_per_node=1))
    assert any("No core count" in x for x in w)


def test_memory_request_is_written_and_capped():
    h = sbatch_header(build_sbatch(1, plan(cores=2, mem_gb=40.2), target()))
    assert "#SBATCH --mem=41G" in h
    h = sbatch_header(build_sbatch(1, plan(cores=2, mem_gb=9999), target()))
    assert "#SBATCH --mem=85G" in h


def test_whole_node_on_request():
    h = sbatch_header(build_sbatch(1, plan(whole_node=True, cores=4), target()))
    assert "#SBATCH --exclusive" in h
    assert not any("cpus-per-task" in x for x in h)
    # only a real true counts: a truthy string is not a whole-node request
    h = sbatch_header(build_sbatch(1, plan(whole_node="yes"), target()))
    assert "#SBATCH --exclusive" not in h and "#SBATCH --cpus-per-task=2" in h


@pytest.mark.parametrize("part", ["highmem", "gpul4"])
def test_whole_node_partitions_keep_whole_nodes(part):
    h = sbatch_header(build_sbatch(1, plan(partition=part, cores=2, gpus=1), target()))
    assert "#SBATCH --exclusive" in h
    assert not any("cpus-per-task" in x or x.startswith("#SBATCH --mem") for x in h)


def test_whole_node_partitions_keep_mpi_layout():
    h = sbatch_header(
        build_sbatch(1, plan(partition="highmem", ntasks_per_node=32), target())
    )
    assert "#SBATCH --exclusive" in h and "#SBATCH --ntasks-per-node=32" in h


def test_catalog_flag_decides_which_partitions_are_whole_node():
    t = target()
    t.partitions["computehigh"]["exclusive"] = True  # cluster says: still whole-node
    t.partitions["highmem"]["exclusive"] = False  # and highmem shares
    assert "#SBATCH --exclusive" in build_sbatch(1, plan(), t)
    assert "#SBATCH --cpus-per-task=2" in build_sbatch(1, plan(partition="highmem"), t)


def test_target_config_can_name_whole_node_partitions():
    t = target(cfg_extra={"whole_node_partitions": ["standard"]})
    assert "#SBATCH --exclusive" in build_sbatch(1, plan(partition="standard"), t)
    assert "#SBATCH --cpus-per-task=2" in build_sbatch(1, plan(partition="highmem"), t)


# ---- cost ------------------------------------------------------------------------


def test_cost_is_whole_node_until_the_cluster_shares():
    # no catalog flag yet: today's cluster is exclusive, every job pays the node
    assert estimate_cost(target(), plan(cores=2)) == pytest.approx(1.87)


def test_cost_is_the_share_of_the_node_once_shared():
    t = target(exclusive=False)
    assert estimate_cost(t, plan(cores=11)) == pytest.approx(0.94, abs=0.01)  # half
    assert estimate_cost(t, plan()) == pytest.approx(1.87 * 2 / 22, abs=0.01)
    # memory share counts when it is the larger one
    assert estimate_cost(t, plan(cores=2, mem_gb=42.5)) == pytest.approx(0.94, abs=0.01)
    # whole node on request and whole-node partitions pay the node
    assert estimate_cost(t, plan(whole_node=True)) == pytest.approx(1.87)
    assert estimate_cost(t, plan(partition="highmem", cores=1)) == pytest.approx(4.19)
    # MPI ranks: two nodes at 16 of 22 cores
    assert estimate_cost(t, plan(nodes=2, ntasks_per_node=16)) == pytest.approx(
        2 * 1.87 * 16 / 22, abs=0.01
    )


def test_share_never_exceeds_a_node_and_unknown_size_pays_the_node():
    t = target(exclusive=False)
    assert labcores.node_share(t, "computehigh", {"cores": 99}) == 1.0
    # memory above the node is capped too, so the share stays at most one node
    assert labcores.node_share(t, "computehigh", {"cores": 2, "mem_gb": 999}) == 1.0
    t.partitions["computehigh"].pop("cpus")
    assert labcores.node_share(t, "computehigh", {"cores": 2}) == 1.0


# ---- pre-flight --------------------------------------------------------------------


def test_preflight_warns_when_no_cores_are_given():
    w = labcores.warnings(target(), plan())
    assert any("No core count" in x and "2 cores" in x for x in w)
    assert labcores.warnings(target(), plan(cores=4)) == []
    assert labcores.warnings(target(), plan(ntasks_per_node=8)) == []
    assert labcores.warnings(target(), plan(whole_node=True)) == []
    assert labcores.warnings(target(), plan(partition="gpul4")) == []


def test_preflight_flags_all_core_idioms_but_not_slurm_counts():
    bad = plan(cores=4)
    bad["script"] = "python3 -c 'import os; n = os.cpu_count()'\nmake -j$(nproc --all)"
    w = labcores.warnings(target(), bad)
    assert any(
        "os.cpu_count()" in x and "nproc --all" in x and "holds 4" in x for x in w
    )
    ok = plan(cores=4)
    ok["script"] = 'make -j"$SLURM_CPUS_ON_NODE"\nOMP_NUM_THREADS=$(nproc) ./a.out'
    assert labcores.warnings(target(), ok) == []


def test_preflight_flags_more_cores_than_the_node_has():
    w = labcores.warnings(target(), plan(cores=40))
    assert any("40 cores per node requested" in x and "have 22" in x for x in w)


def test_lab_preflight_includes_core_warnings(tmp_path):
    tgt = target()
    lab = labm.Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"u": tgt})
    assert any("No core count" in x for x in lab._check(tgt, plan()))


# ---- prompts -----------------------------------------------------------------------


def test_prompts_explain_sharing_and_the_whole_node_partitions():
    txt = target().describe()
    assert "Shared partitions (`standard`, `computehigh`, `spot`)" in txt
    assert "Whole-node partitions (`highmem`, `gpul4`)" in txt
    assert "resources.cores" in txt and "switching" in txt  # not shared yet
    live = target(exclusive=False).describe()
    assert "switching" not in live
    assert "Whole nodes are allocated" not in txt


def test_planner_prompt_asks_for_cores():
    assert '"cores": 2' in labm.PLAN_PROMPT and "whole_node" in labm.PLAN_PROMPT
    assert "never os.cpu_count()" in labm.PLAN_PROMPT


def test_catalog_exclusive_flag_reaches_the_target(tmp_path, monkeypatch):
    t = labm.SlurmSSHTarget(
        {"name": "u", "ssh_host": "h", "catalog_path": "/apps/docs/catalog.json"}
    )
    cat = {
        "schema": "ursa-catalog/1",
        "generated": "x",
        "partitions": [
            {"name": "computehigh", "cpus_per_node": 22, "exclusive": False},
            {"name": "gpul4", "cpus_per_node": 8, "exclusive": True},
            {"name": "standard", "cpus_per_node": 16},
        ],
    }
    monkeypatch.setattr(t, "sh", lambda cmd, stdin=None, timeout=120: json.dumps(cat))
    t.load_catalog(tmp_path)
    assert t.partitions["computehigh"]["exclusive"] is False
    assert t.partitions["gpul4"]["exclusive"] is True
    assert "exclusive" not in t.partitions["standard"]


def test_mpi_shaped_work_without_cores_gets_a_louder_warning(tmp_path):
    tgt = target()
    lab = labm.Lab(str(tmp_path / "h.db"), lambda: None, tmp_path, targets={"u": tgt})
    mpi = plan()
    mpi["script"] = "srun gmx_mpi mdrun -deffnm md"
    assert any(
        x.startswith("Cores: this looks like mpi work") for x in lab._check(tgt, mpi)
    )
    mpi["resources"]["ntasks_per_node"] = 20
    assert not any(x.startswith("Cores:") for x in lab._check(tgt, mpi))
