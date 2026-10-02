"""Cores and memory for Lab jobs on shared partitions (v0.52.1).

Ursa Major is moving to shared nodes: every partition except the whole-node ones
(`highmem`, `gpul4`) lets several jobs share a node. On a shared partition a job gets
only the cores and memory it asks for, and the cgroup holds it there. A job that asks
for nothing gets one core.

So every Lab job on a shared partition asks for its cores explicitly:

- `resources.cores`: CPU cores per node. Missing or invalid -> `DEFAULT_CORES` (2).
  Capped at the partition's cores per node.
- `resources.ntasks_per_node` (MPI): one core per rank. With `cores` too, each rank
  gets `cores` threads (hybrid MPI+OpenMP) when ranks x cores fits the node. A single
  rank (`ntasks_per_node: 1`, which older plans set for serial work) is not MPI: it
  means one task holding `cores` cores.
- `resources.mem_gb` (optional): memory per node. Without it Slurm gives
  `DefMemPerCPU` per core, which on Ursa Major is the node's memory divided by its
  cores (a fair share of the node).
- `resources.whole_node: true`: the whole node (`--exclusive`), for work that needs it.

Whole-node partitions always give the whole node; there the request lines are not
written and the cost is the whole node, as before.

Which partitions are whole-node: the catalog's per-partition `exclusive` flag when the
cluster publishes one, else the target config's `whole_node_partitions`, else
`WHOLE_NODE_PARTITIONS`. The request lines are written for every other partition, ahead
of the cluster change: measured before it (job 324, 2026-10-02, OverSubscribe=EXCLUSIVE),
`--cpus-per-task=2` still received all 22 cores (nproc, os.cpu_count,
SLURM_CPUS_ON_NODE all 22), so the lines change nothing until the partitions are shared.

Cost follows what the cluster really does: the share of the node only when the catalog
says the partition is shared (`exclusive: false`); otherwise the whole node, because
until then every job is billed for whole nodes.
"""

from __future__ import annotations

import math
import re
from typing import Any

DEFAULT_CORES = 2
WHOLE_NODE_PARTITIONS = frozenset({"highmem", "gpul4"})


def _int(v: Any) -> int | None:
    """A positive whole number from a plan value ('4', 4, 4.0), else None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != int(f) or f < 1:
        return None
    return int(f)


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 and math.isfinite(f) else None


def is_whole_node(target: Any, partition: str) -> bool:
    """True when `partition` always hands out whole nodes."""
    part = (getattr(target, "partitions", None) or {}).get(partition) or {}
    if isinstance(part.get("exclusive"), bool):  # published by the cluster catalog
        return part["exclusive"]
    cfg = getattr(target, "cfg", None) or {}
    names = cfg.get("whole_node_partitions") if isinstance(cfg, dict) else None
    if isinstance(names, list):
        return partition in names
    return partition in WHOLE_NODE_PARTITIONS


def sharing_live(target: Any, partition: str) -> bool:
    """True only when the cluster's catalog says the partition shares nodes now."""
    part = (getattr(target, "partitions", None) or {}).get(partition) or {}
    return part.get("exclusive") is False


def request(target: Any, partition: str, resources: dict) -> dict:
    """What the job asks for per node.

    Returns {whole_node, cores, ranks, per_rank, mem_gb, node_cores, node_mem_gb,
    defaulted}: `cores` is the core count per node the job holds (whole node: every
    core), `ranks` the MPI ranks per node (None for a single task), `per_rank` the cores
    per rank, `defaulted` True when the plan named no cores and DEFAULT_CORES was used.
    """
    part = (getattr(target, "partitions", None) or {}).get(partition) or {}
    node_cores = _int(part.get("cpus"))
    node_mem = _num(part.get("mem_gb"))
    whole = is_whole_node(target, partition) or resources.get("whole_node") is True
    ranks = _int(resources.get("ntasks_per_node"))
    if whole:
        return {
            "whole_node": True,
            "cores": node_cores,
            "ranks": ranks,
            "mem_gb": node_mem,
            "node_cores": node_cores,
            "node_mem_gb": node_mem,
            "defaulted": False,
        }
    cores = _int(resources.get("cores"))
    defaulted = False
    if ranks == 1:
        ranks = None  # one task: a serial or threaded job, sized by `cores`
    per_rank = 1
    if ranks:
        if cores and node_cores and ranks * cores <= node_cores:
            per_rank = cores  # hybrid MPI + threads
        if node_cores:
            ranks = min(ranks, node_cores)
        cores = ranks * per_rank
    elif cores is None:
        cores, defaulted = DEFAULT_CORES, True
    if node_cores:
        cores = min(cores, node_cores)
    mem = _num(resources.get("mem_gb"))
    if mem is not None and node_mem:
        mem = min(mem, node_mem)
    return {
        "whole_node": False,
        "cores": cores,
        "ranks": ranks,
        "per_rank": per_rank,
        "mem_gb": mem,
        "node_cores": node_cores,
        "node_mem_gb": node_mem,
        "defaulted": defaulted,
    }


def sbatch_lines(target: Any, partition: str, resources: dict) -> list[str]:
    """The #SBATCH lines that size the job on its partition."""
    q = request(target, partition, resources)
    if q["whole_node"]:
        # whole-node partitions already give the whole node; keep the MPI layout
        out = ["#SBATCH --exclusive"]
        if q["ranks"]:
            out.append(f"#SBATCH --ntasks-per-node={q['ranks']}")
        return out
    if q["ranks"]:
        out = [
            f"#SBATCH --ntasks-per-node={q['ranks']}",
            f"#SBATCH --cpus-per-task={q['per_rank']}",
        ]
    else:
        # one task per node holding `cores` cores (works for --nodes > 1 too)
        out = ["#SBATCH --ntasks-per-node=1", f"#SBATCH --cpus-per-task={q['cores']}"]
    if q["mem_gb"] is not None:
        out.append(f"#SBATCH --mem={max(1, math.ceil(q['mem_gb']))}G")
    return out


def node_share(target: Any, partition: str, resources: dict) -> float:
    """Share of each node the job is billed for: max(cores share, memory share) on a
    partition the catalog marks shared; 1.0 for whole nodes, for partitions not yet
    shared, or when the node size is unknown."""
    q = request(target, partition, resources)
    if q["whole_node"] or not q["node_cores"] or not sharing_live(target, partition):
        return 1.0
    share = q["cores"] / q["node_cores"]
    if q["mem_gb"] is not None and q["node_mem_gb"]:
        share = max(share, q["mem_gb"] / q["node_mem_gb"])
    return min(1.0, share)


_ALL_CORE_IDIOMS = [
    (re.compile(r"os\.cpu_count\(\)|multiprocessing\.cpu_count\(\)"), "os.cpu_count()"),
    (re.compile(r"\bnproc\s+--all\b"), "nproc --all"),
    (re.compile(r"/proc/cpuinfo"), "/proc/cpuinfo"),
]
# Not listed: $SLURM_CPUS_ON_NODE and plain `nproc` report the cores the job was
# given (Slurm's count and the cgroup's CPU set), so they are right on shared nodes.


def warnings(target: Any, plan: dict) -> list[str]:
    """Pre-flight advice about cores on a shared partition (never blocks)."""
    if not target or not getattr(target, "partitions", None):
        return []
    r = plan.get("resources") or {}
    part = str(r.get("partition") or getattr(target, "default_partition", ""))
    if part not in target.partitions:
        return []
    q = request(target, part, r)
    if q["whole_node"]:
        return []
    warns: list[str] = []
    asked = _int(r.get("ntasks_per_node")) or _int(r.get("cores"))
    if asked and q["node_cores"] and asked > q["node_cores"]:
        warns.append(
            f"{asked} cores per node requested; '{part}' nodes have {q['node_cores']} "
            f"(the job will ask for {q['node_cores']})"
        )
    if q["defaulted"]:
        warns.append(
            f"No core count: '{part}' is a shared partition, so the job will get "
            f"{DEFAULT_CORES} cores. Set resources.cores to what the work can use "
            f"(up to {q['node_cores'] or '?'}), or resources.whole_node = true"
        )
    script = str(plan.get("script") or "")
    seen = [label for rx, label in _ALL_CORE_IDIOMS if rx.search(script)]
    if seen:
        warns.append(
            f"The script sizes threads from {', '.join(seen)}, which counts every core "
            f"on the node, but on shared '{part}' the job holds {q['cores']}. Use "
            "$SLURM_CPUS_PER_TASK or $SLURM_CPUS_ON_NODE (or $SLURM_NTASKS for MPI ranks)"
        )
    return warns


def describe(target: Any) -> str:
    """One paragraph for planner prompts: which partitions share nodes and how to size."""
    parts = list((getattr(target, "partitions", None) or {}).keys())
    if not parts:
        return ""
    whole = [p for p in parts if is_whole_node(target, p)]
    shared = [p for p in parts if p not in whole]
    if not shared:
        return "Every partition gives whole nodes."
    live = all(sharing_live(target, p) for p in shared)
    return (
        "Shared partitions ("
        + ", ".join(f"`{p}`" for p in shared)
        + "): a job gets only the cores and memory it asks for, and cannot use more"
        + (
            ". "
            if live
            else " (the cluster is switching to this; until then these partitions still "
            "give whole nodes, but plan the cores now). "
        )
        + "Set resources.cores to the cores the work can really use (default "
        f"{DEFAULT_CORES}; serial Python or R: 1-2; threaded or multiprocessing code: the "
        "worker count), resources.mem_gb only when it needs more memory than its share "
        "of the node (memory scales with cores), or resources.whole_node = true for "
        "work that needs every core. MPI: resources.ntasks_per_node ranks, one core "
        "each (with resources.cores, that many threads per rank). Size threads from $SLURM_CPUS_ON_NODE (or $SLURM_CPUS_PER_TASK), never "
        "os.cpu_count() or nproc --all. Cost is the share of the node held."
        + (
            " Whole-node partitions ("
            + ", ".join(f"`{p}`" for p in whole)
            + "): every job gets its whole nodes and pays for them."
            if whole
            else ""
        )
    )
