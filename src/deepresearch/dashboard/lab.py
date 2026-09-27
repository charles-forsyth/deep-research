"""Lab runs: turn a question in a report into a real computational job on an HPC cluster.

Flow: suggestions -> plan (AI, reviewed and editable) -> submit -> watch -> fetch -> write-up.

Nothing here fakes science. If software will not install or a job fails, the run is marked
failed with the log that says why.

Cluster access goes through a Target (Slurm over SSH today). Job state lives on the
cluster (the job folder and Slurm accounting); the local database is a mirror that the
watcher keeps in step, so a closed browser, a restarted dashboard or a sleeping laptop
never loses a job.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import threading
import time

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

PLAN_MODEL = "gemini-3.8-flash"  # default; GEMINI_FOLLOWUP_MODEL overrides it
# gemini-3.8-flash list prices, USD per 1M tokens (thinking billed as output).
FLASH_IN_1M = 0.75
FLASH_CACHED_1M = 0.075
FLASH_OUT_1M = 3.75
# Google Search grounding per 1,000 queries. The 5,000 free queries a month are shared
# with every other Gemini use and cannot be seen from here, so this is the worst case.
SEARCH_PER_1K = 14.00
MAX_FETCH_TRIES = 5  # fetch attempts before a finished job is marked failed

STATES = (
    "planning",  # AI is writing the plan
    "plan_failed",
    "draft",  # plan ready for review
    "submitting",
    "queued",  # accepted by Slurm, waiting for a node
    "running",  # the batch script is running (installing or computing)
    "fetching",  # copying results back
    "analyzing",  # AI is writing the results note
    "completed",
    "failed",
    "cancelled",
)
ACTIVE = ("submitting", "queued", "running", "fetching", "analyzing")
FINAL = ("completed", "failed", "cancelled")
SLURM_DONE = {
    "COMPLETED": "completed",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
    "TIMEOUT": "failed",
    "NODE_FAIL": "failed",
    "OUT_OF_MEMORY": "failed",
    "PREEMPTED": "failed",
    "BOOT_FAIL": "failed",
    "DEADLINE": "failed",
}
MAX_FETCH_BYTES = 200 * 1024 * 1024  # whole outputs folder
MAX_FILE_BYTES = 50 * 1024 * 1024  # any single file


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _cost(usage, searches: int = 0) -> float | None:
    if not usage and not searches:
        return None
    inp = getattr(usage, "prompt_token_count", 0) or 0
    cached = min(getattr(usage, "cached_content_token_count", 0) or 0, inp)
    out = (getattr(usage, "candidates_token_count", 0) or 0) + (
        getattr(usage, "thoughts_token_count", 0) or 0
    )
    tool = getattr(usage, "tool_use_prompt_token_count", 0) or 0
    return round(
        (inp - cached + tool) / 1e6 * FLASH_IN_1M
        + cached / 1e6 * FLASH_CACHED_1M
        + out / 1e6 * FLASH_OUT_1M
        + searches / 1000 * SEARCH_PER_1K,
        4,
    )


def _search_count(resp) -> int:
    """Google Search queries a generate_content reply ran (each one is billed)."""
    n = 0
    for cand in getattr(resp, "candidates", None) or []:
        gm = getattr(cand, "grounding_metadata", None)
        n += len(getattr(gm, "web_search_queries", None) or [])
    return n


# A backslash sequence inside a JSON string: group 1 = valid escape (kept as is),
# otherwise an invalid one (its backslash gets doubled). Matching valid pairs first
# keeps an already-correct `\\\\d` from being split into `\\` + `\\d`.
_ESCAPE = re.compile(r'\\(\\|["/bfnrt]|u[0-9a-fA-F]{4})|\\')


def _loads_lenient(body: str) -> Any:
    """json.loads that survives what models put inside long string values.

    Plans embed whole bash/Python scripts in a JSON string. Models often leave regex or
    LaTeX backslashes unescaped (`\\d`, `\\alpha`, `\\(`) and sometimes raw newlines or
    tabs; strict JSON rejects both ("Invalid \\escape", "Invalid control character").
    Try strict first, then allow control characters, then double every backslash that
    does not start a valid JSON escape, which is what the model meant.
    """
    try:
        return json.loads(body)
    except ValueError:
        pass
    try:
        return json.loads(body, strict=False)
    except ValueError:
        pass
    fixed = _ESCAPE.sub(lambda m: m.group(0) if m.group(1) else "\\\\", body)
    return json.loads(fixed, strict=False)


def extract_json(text: str) -> Any:
    """First JSON object or array in a model reply (fenced or bare), parsed leniently."""
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text or "", re.S)
    body = m.group(1) if m else (text or "")
    try:
        return _loads_lenient(body)
    except ValueError:
        pass
    start = min([i for i in (body.find("{"), body.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("no JSON in model reply")
    opener = body[start]
    closer = "}" if opener == "{" else "]"
    end = body.rfind(closer)
    return _loads_lenient(body[start : end + 1])


# --------------------------------------------------------------------------- targets


class TargetError(RuntimeError):
    pass


class EmptyReply(ValueError):
    """The model returned no text (for example it stopped on a tool-call limit)."""

    def __init__(self, finish: str, cost: float | None):
        super().__init__(f"model returned no text (finish reason: {finish})")
        self.finish = finish
        self.cost = cost


class SlurmSSHTarget:
    """A Slurm cluster reached over SSH (gcloud IAP or a plain ssh host).

    One multiplexed SSH connection is reused for every call (status checks take about
    0.3 s instead of 5 s through a fresh IAP tunnel).
    """

    kind = "slurm-ssh"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg.get("name", "cluster")
        self.label = cfg.get("label", self.name)
        self.remote_root = cfg.get("remote_root", "~/deep-research-lab")
        self.partitions: dict[str, dict] = cfg.get("partitions", {})
        self.default_partition = cfg.get("default_partition") or next(
            iter(self.partitions), "standard"
        )
        self.modules: list[str] = cfg.get("modules", [])
        self.software_notes: str = cfg.get("software_notes", "")
        # Machine-readable description published by the cluster (ursa-catalog).
        # Fetched over the same SSH connection, cached on disk, refreshed on demand.
        self.catalog_path: str = cfg.get("catalog_path", "")
        self.catalog: dict | None = None
        self.catalog_fetched: str = ""
        self._argv: list[str] | None = None
        self._dest = ""
        self._lock = threading.Lock()

    # ---- connection ------------------------------------------------------
    def _base(self) -> tuple[list[str], str]:
        with self._lock:
            if self._argv is None:
                if self.cfg.get("gcloud"):
                    g = self.cfg["gcloud"]
                    out = subprocess.run(
                        [
                            "gcloud",
                            "compute",
                            "ssh",
                            g["instance"],
                            f"--zone={g['zone']}",
                            f"--project={g['project']}",
                            "--tunnel-through-iap",
                            "--dry-run",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    if out.returncode != 0 or not out.stdout.strip():
                        raise TargetError(
                            "gcloud could not build the SSH command: "
                            + (out.stderr.strip().splitlines() or ["no output"])[-1]
                        )
                    argv = [a for a in shlex.split(out.stdout.strip()) if a != "-t"]
                    self._dest = argv.pop()
                else:
                    argv = ["ssh"]
                    self._dest = self.cfg["ssh_host"]
                sock = Path(os.getenv("XDG_RUNTIME_DIR") or "/tmp") / "dr-lab-%C"
                argv += [
                    "-o",
                    "ControlMaster=auto",
                    "-o",
                    f"ControlPath={sock}",
                    "-o",
                    "ControlPersist=900",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ServerAliveInterval=30",
                    "-o",
                    "ConnectTimeout=30",
                ]
                self._argv = argv
            return self._argv, self._dest

    def run(
        self, command: str, stdin: bytes | None = None, timeout: int = 120
    ) -> subprocess.CompletedProcess:
        argv, dest = self._base()
        try:
            r = subprocess.run(
                argv + [dest, command],
                input=stdin,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise TargetError(f"cluster command timed out after {timeout}s") from e
        if r.returncode == 255:  # ssh itself failed
            with self._lock:
                self._argv = None  # rebuild next time (token or tunnel expired)
            raise TargetError(
                "cannot reach the cluster: "
                + (
                    r.stderr.decode("utf-8", "replace").strip().splitlines()
                    or ["ssh error"]
                )[-1]
            )
        return r

    def sh(self, command: str, stdin: bytes | None = None, timeout: int = 120) -> str:
        r = self.run(command, stdin, timeout)
        if r.returncode != 0:
            raise TargetError(
                f"`{command[:60]}` failed: "
                + r.stderr.decode("utf-8", "replace").strip()[-400:]
            )
        return r.stdout.decode("utf-8", "replace")

    # ---- operations ------------------------------------------------------
    def job_dir(self, run_id: int) -> str:
        return f"{self.remote_root}/run_{run_id}"

    def submit(self, run_id: int, files: dict[str, str]) -> str:
        """Upload the run's files and sbatch run.sbatch. Returns the Slurm job id."""
        d = self.job_dir(run_id)
        import base64
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, text in files.items():
                data = text.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = int(time.time())
                info.mode = 0o755 if name.endswith((".sh", ".sbatch")) else 0o644
                tar.addfile(info, io.BytesIO(data))
        payload = base64.b64encode(buf.getvalue())
        out = self.sh(
            f"set -e; mkdir -p {d}/outputs; cd {d}; base64 -d | tar xzf -; "
            f"sbatch --parsable run.sbatch",
            stdin=payload,
            timeout=180,
        )
        job = out.strip().split(";")[0]
        if not job.isdigit():
            raise TargetError(f"sbatch returned {out.strip()!r}")
        return job

    def status(self, run_id: int, job_id: str) -> dict:
        """One round trip: Slurm state, last stage marker, log tail and size."""
        d = self.job_dir(run_id)
        cmd = (
            f"sacct -j {job_id} -X -n -P -o State,Elapsed,NodeList,ExitCode,Start 2>/dev/null | head -1; "
            f"echo '::SQ::'; squeue -h -j {job_id} -o '%T|%r|%M|%N' 2>/dev/null; "
            f"echo '::ST::'; tail -n 1 {d}/stage.txt 2>/dev/null; "
            f"echo '::SZ::'; stat -c %s {d}/job.log 2>/dev/null || echo 0"
        )
        out = self.sh(cmd, timeout=60)
        acct, rest = (out.split("::SQ::", 1) + [""])[:2]
        sq, rest = (rest.split("::ST::", 1) + [""])[:2]
        stage, size = (rest.split("::SZ::", 1) + ["0"])[:2]
        a = (acct.strip().split("|") + [""] * 5)[:5]
        q = (sq.strip().split("|") + [""] * 4)[:4]
        state = (q[0] or a[0] or "").split()[0] if (q[0] or a[0]) else ""
        return {
            "slurm_state": state,
            "reason": q[1] if q[0] else "",
            "elapsed": q[2] or a[1],
            "node": q[3] or a[2],
            "exit_code": a[3],
            "started": a[4] if a[4] not in ("Unknown", "None") else "",
            "stage": stage.strip(),
            "log_size": int((size.strip() or "0").split()[0] or 0),
        }

    def log(
        self, run_id: int, offset: int = 0, limit: int = 200_000
    ) -> tuple[str, int]:
        d = self.job_dir(run_id)
        out = self.run(
            f"f={d}/job.log; s=$(stat -c %s $f 2>/dev/null || echo 0); echo $s; "
            f"tail -c +{max(offset, 0) + 1} $f 2>/dev/null | head -c {limit}",
            timeout=60,
        ).stdout
        head, _, body = out.partition(b"\n")
        try:
            size = int(head.strip() or 0)
        except ValueError:
            size = 0
        return body.decode("utf-8", "replace"), size

    def cancel(self, job_id: str) -> None:
        self.sh(f"scancel {job_id}", timeout=60)

    def fetch(self, run_id: int, dest: Path) -> list[dict]:
        """Copy outputs/ plus the log, plan and script back. Returns the file list."""
        import io
        import tarfile

        d = self.job_dir(run_id)
        listing = self.sh(
            f"cd {d} && find outputs job.log run.sbatch plan.json stage.txt "
            f"-maxdepth 3 -type f -printf '%s\\t%p\\n' 2>/dev/null || true",
            timeout=60,
        )
        keep, total, skipped = [], 0, []
        for line in listing.splitlines():
            size_s, _, path = line.partition("\t")
            if not path or ".." in path:
                continue
            size = int(size_s or 0)
            if size > MAX_FILE_BYTES or total + size > MAX_FETCH_BYTES:
                skipped.append({"path": path, "size": size, "skipped": True})
                continue
            keep.append(path)
            total += size
        dest.mkdir(parents=True, exist_ok=True)
        files: list[dict] = []
        if keep:
            r = self.run(
                f"cd {d} && tar czf - " + " ".join(shlex.quote(p) for p in keep),
                timeout=600,
            )
            if r.returncode != 0:
                raise TargetError("fetch failed: " + r.stderr.decode()[-300:])
            with tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r:gz") as tar:
                for m in tar.getmembers():
                    if not m.isfile() or m.name.startswith("/") or ".." in m.name:
                        continue
                    target = (dest / m.name).resolve()
                    if not target.is_relative_to(dest.resolve()):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    src = tar.extractfile(m)
                    if src:
                        target.write_bytes(src.read())
                        files.append({"path": m.name, "size": m.size})
        return files + skipped

    # ---- cluster catalog ---------------------------------------------------
    def load_catalog(
        self, cache_dir: Path | None, refresh: bool = False
    ) -> dict | None:
        """The cluster's published catalog (partitions, modules, recipes, tools).

        Read from the local cache unless `refresh`; on refresh (or no cache) fetch it
        over SSH. A fetch failure keeps the cached copy: stale beats nothing.
        """
        if not self.catalog_path:
            return None
        cache = (cache_dir / f"catalog-{self.name}.json") if cache_dir else None
        if not refresh and self.catalog is None and cache and cache.exists():
            try:
                data = json.loads(cache.read_text())
                self.catalog, self.catalog_fetched = (
                    data.get("catalog"),
                    data.get("fetched", ""),
                )
            except ValueError:
                self.catalog = None
        if refresh or self.catalog is None:
            raw = self.sh(f"cat {shlex.quote(self.catalog_path)}", timeout=60)
            cat = json.loads(raw)
            if not isinstance(cat, dict) or not str(cat.get("schema", "")).startswith(
                "ursa-catalog/"
            ):
                raise TargetError("cluster catalog has an unknown format")
            self.catalog, self.catalog_fetched = cat, _now()
            if cache:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(
                    json.dumps({"fetched": self.catalog_fetched, "catalog": cat})
                )
            self._apply_catalog_partitions()
        elif self.catalog:
            self._apply_catalog_partitions()
        return self.catalog

    def _apply_catalog_partitions(self) -> None:
        """Catalog partitions replace the hand-written table (prices, cores, GPUs)."""
        cat = self.catalog or {}
        parts: dict[str, dict] = {}
        for p in cat.get("partitions") or []:
            if not isinstance(p, dict) or not p.get("name"):
                continue
            old = self.partitions.get(p["name"], {})
            parts[p["name"]] = {
                "machine": old.get("machine") or p.get("use_for", "")[:60],
                "cpus": p.get("cpus_per_node"),
                "mem_gb": p.get("mem_gb_per_node"),
                "gpus": p.get("gpus_per_node") or 0,
                "gpu_type": p.get("gpu_type"),
                "usd_per_hour": p.get("usd_per_node_hour", old.get("usd_per_hour")),
                "max_nodes": p.get("max_nodes"),
                "spot": bool(p.get("spot")),
                "use_for": p.get("use_for", ""),
            }
            if p.get("default"):
                self.default_partition = p["name"]
        if parts:
            self.partitions = parts

    def known_modules(self) -> tuple[set[str], dict[str, str]]:
        """(every loadable module incl. bare names, module -> MPI it needs)."""
        mods = (self.catalog or {}).get("modules") or {}
        have: set[str] = set()
        needs: dict[str, str] = {}
        for m in mods.get("core") or []:
            have |= {m, m.split("/")[0]}
        for mpi, tier in (mods.get("mpi_dependent") or {}).items():
            for m in tier.get("modules") or []:
                for k in (m, m.split("/")[0]):
                    if k not in have:
                        needs.setdefault(k, mpi)
                have |= {m, m.split("/")[0]}
        return have, needs

    def describe_brief(self) -> str:
        """Capabilities overview for suggestions: what is possible, compactly."""
        cat = self.catalog
        if not cat:
            return self.describe()
        s = cat.get("summary") or {}
        gpu = s.get("gpu") or {}
        lines = [f"Target: {self.label} (Slurm). {cat.get('cluster', '')}".strip()]
        lines.append(self._partitions_text())
        lines.append(
            f"Installed: {s.get('modules', '?')} environment modules built from "
            f"{s.get('spack_packages', '?')} Spack packages; GPU driver "
            f"{gpu.get('driver', '?')} (CUDA <= {gpu.get('cuda_max', '?')})."
        )
        by_field: dict[str, list[str]] = {}
        for r in cat.get("recipes") or []:
            by_field.setdefault(r.get("field", "other"), []).append(r.get("name", ""))
        if by_field:
            lines.append("Ready-to-use applications by field:")
            lines += [f"- {f}: {', '.join(n)}" for f, n in sorted(by_field.items())]
        core = (cat.get("modules") or {}).get("core") or []
        mpi = (cat.get("modules") or {}).get("mpi_dependent") or {}
        names = sorted(
            {m.split("/")[0] for m in core}
            | {m.split("/")[0] for t in mpi.values() for m in t.get("modules") or []}
        )
        lines.append("All installed packages (module names): " + ", ".join(names))
        lines.append(
            "Anything else can be installed inside a job (uv/pip, Pixi/conda, "
            "Apptainer containers). Capabilities worth using: multi-node MPI, GPUs, "
            "~500 GB memory nodes, cheap spot nodes for parameter sweeps."
        )
        return "\n".join(lines)

    def describe_full(self) -> str:
        """Everything a job planner needs: modules, recipes, rules, tools."""
        cat = self.catalog
        if not cat:
            return self.describe()
        mods = cat.get("modules") or {}
        gpu = (cat.get("summary") or {}).get("gpu") or {}
        out = [
            f"Target: {self.label} (Slurm). Catalog generated {cat.get('generated', '?')}.",
            self._partitions_text(),
            f"GPU: {gpu.get('gpu', 'NVIDIA L4')}, driver {gpu.get('driver', '?')}, "
            f"runs CUDA <= {gpu.get('cuda_max', '?')}.",
            "Rules:",
            *[f"- {r}" for r in cat.get("rules") or []],
            "How to load: " + str(cat.get("how_to_load", "")),
            "Modules loadable directly: " + ", ".join(mods.get("core") or []),
        ]
        for mpi, t in (mods.get("mpi_dependent") or {}).items():
            out.append(
                f"Modules after `{t.get('requires', 'module load ' + mpi)}`: "
                + ", ".join(t.get("modules") or [])
            )
        broken = sorted((cat.get("module_health") or {}).get("broken") or {})
        if broken:
            out.append(
                "Broken modules (installed but fail to run; never load these, use "
                "conda instead): " + ", ".join(broken)
            )
        out.append("Tested recipes (use these load lines exactly):")
        for r in cat.get("recipes") or []:
            load = (
                " && ".join(f"module load {m}" for m in r.get("load") or []) or "(none)"
            )
            out.append(
                f"- {r.get('name')}: {load}; run: {r.get('run')}; partition "
                f"{r.get('partition')}" + (f"; {r['notes']}" if r.get("notes") else "")
            )
        out.append("Install tools for anything not installed:")
        out += [
            f"- {t.get('name')} ({t.get('how')}): {t.get('use')}"
            for t in cat.get("install_tools") or []
        ]
        if cat.get("containers"):
            out.append(
                "Prebuilt Apptainer images (use the path directly, no pull needed): "
                + ", ".join(c["path"] for c in cat["containers"])
            )
        jh = cat.get("job_header") or {}
        if jh.get("path"):
            out.append(
                f"The harness already sources the site job header {jh['path']} "
                "(TMPDIR, SCRATCH, caches); do not redo that in the script."
            )
        if self.software_notes:
            out.append("Site notes: " + self.software_notes)
        return "\n".join(out)

    def _partitions_text(self) -> str:
        rows = []
        for p, v in self.partitions.items():
            rows.append(
                f"- `{p}`{' (default)' if p == self.default_partition else ''}: "
                f"{v.get('cpus', '?')} cores, {v.get('mem_gb', '?')} GB"
                + (
                    f", {v['gpus']}x {v.get('gpu_type') or 'GPU'}"
                    if v.get("gpus")
                    else ""
                )
                + (f", up to {v['max_nodes']} nodes" if v.get("max_nodes") else "")
                + f", ${v.get('usd_per_hour', '?')}/node-hour"
                + (f". {v['use_for']}" if v.get("use_for") else "")
            )
        return "Partitions (whole nodes, created on demand):\n" + "\n".join(rows)

    def describe(self) -> str:
        parts = "\n".join(
            f"- `{p}`: {v.get('machine', '?')}, {v.get('cpus', '?')} Slurm CPUs per node, "
            f"{v.get('mem_gb', '?')} GB RAM"
            + (f", {v['gpus']}x {v.get('gpu_type', 'GPU')}" if v.get("gpus") else "")
            + f", about ${v.get('usd_per_hour', '?')}/node-hour"
            for p, v in self.partitions.items()
        )
        return (
            f"Target: {self.label} (Slurm). Partitions:\n{parts}\n"
            f"Default partition: {self.default_partition}. Whole nodes are allocated "
            f"(exclusive). Nodes boot on demand (1-5 minutes before the job starts).\n"
            f"Existing environment modules: {', '.join(self.modules) or 'none'}.\n"
            f"{self.software_notes}"
        )


def load_targets(config_dir: Path) -> dict[str, SlurmSSHTarget]:
    """Targets from lab_targets.json in the state dir (kept out of the repo)."""
    path = config_dir / "lab_targets.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    out = {}
    for t in data.get("targets", []):
        if t.get("type", "slurm-ssh") == "slurm-ssh":
            tgt = SlurmSSHTarget(t)
            out[tgt.name] = tgt
    return out


def validate_plan(target: SlurmSSHTarget | None, plan: dict) -> list[str]:
    """Check a plan against the cluster catalog before anything is submitted.

    Returns human-readable warnings (empty when the plan fits). Never raises: a plan
    with warnings can still be edited or submitted by the reviewer.
    """
    if not target or not isinstance(plan, dict):
        return []
    warns: list[str] = []
    r = plan.get("resources") or {}
    part_name = r.get("partition") or target.default_partition
    part = target.partitions.get(part_name)
    if target.partitions and part is None:
        warns.append(
            f"Partition '{part_name}' does not exist; available: "
            + ", ".join(target.partitions)
        )
    gpus = int(r.get("gpus") or 0) if str(r.get("gpus") or 0).isdigit() else 0
    if part is not None and isinstance(part, dict):
        have_gpus = int(part.get("gpus") or 0)
        if gpus and not have_gpus:
            gp = [k for k, v in target.partitions.items() if v.get("gpus")]
            warns.append(
                f"{gpus} GPU(s) requested on '{part_name}', which has none"
                + (f"; use {', '.join(gp)}" if gp else "")
            )
        elif have_gpus and gpus > have_gpus:
            warns.append(f"{gpus} GPUs requested; '{part_name}' nodes have {have_gpus}")
        mx = part.get("max_nodes")
        nodes = r.get("nodes") or 1
        if isinstance(mx, int) and str(nodes).isdigit() and int(nodes) > mx:
            warns.append(f"{nodes} nodes requested; '{part_name}' has at most {mx}")
    if getattr(target, "catalog", None):
        have, needs = target.known_modules()
        mods = [str(m) for m in (plan.get("install") or {}).get("modules") or []]
        loaded_mpi = {m.split("/")[0] for m in mods} & {
            "openmpi",
            "mpich",
            "intel-oneapi-mpi",
        }
        for m in mods:
            if m not in have:
                base = m.split("/")[0]
                alts = sorted(x for x in have if x.split("/")[0] == base and "/" in x)
                warns.append(
                    f"Module '{m}' is not installed"
                    + (f"; available: {', '.join(alts[:6])}" if alts else "")
                )
            elif needs.get(m) and needs[m] not in loaded_mpi:
                warns.append(
                    f"Module '{m}' needs `module load {needs[m]}` before it "
                    f"(add '{needs[m]}' earlier in install.modules)"
                )
        # modules the cluster's smoke test found broken (load but cannot run)
        broken = ((target.catalog or {}).get("module_health") or {}).get("broken") or {}
        for m in mods:
            hit = broken.get(m) or next(
                (v for k, v in broken.items() if k.split("/")[0] == m), None
            )
            if hit:
                warns.append(
                    f"Module '{m}' is broken on the cluster ({hit[:120]}); use a "
                    "Pixi/conda environment instead"
                )
        # Python stacks do not mix: one environment module, never py-* or bare python
        pyenv = [m for m in mods if m.split("/")[0] in ("python-sci", "python-ml")]
        stray = [m for m in mods if m.split("/")[0] == "python" or m.startswith("py-")]
        if len(pyenv) > 1:
            warns.append(
                "Load only one Python environment module (" + ", ".join(pyenv) + ")"
            )
        if stray:
            warns.append(
                "Python modules " + ", ".join(stray) + " are not a working stack; load "
                "python-sci (CPU science) or python-ml (PyTorch/GPU), or use conda"
            )
        if pyenv and (plan.get("install") or {}).get("conda"):
            warns.append(
                f"{pyenv[0]} and a conda environment both provide Python; pick one "
                "(extra packages on top of the module: pip, which builds a venv)"
            )
        # installing something that is already a module wastes minutes
        pkgs = [
            re.split(r"[=<>!\[]", str(x))[0].lower()
            for x in ((plan.get("install") or {}).get("conda") or [])
            + ((plan.get("install") or {}).get("pip") or [])
            if not str(x).startswith(("--", "https:"))
        ]
        dup = sorted(
            {p for p in pkgs if p in have and p not in ("python", "pip", "numpy")}
        )
        if dup:
            warns.append(
                "Already installed as modules (faster than installing): "
                + ", ".join(dup)
            )
        imgs = (plan.get("install") or {}).get("apptainer") or []
        local = {
            c.get("path", "") for c in (target.catalog or {}).get("containers") or []
        }
        for i in imgs:
            if str(i).startswith("/") and str(i) not in local:
                warns.append(f"Container image '{i}' is not in /apps/containers")
    return warns


def _describe(tgt, full: bool) -> str:
    if not tgt:
        return "No cluster configured."
    if getattr(tgt, "catalog", None):
        return tgt.describe_full() if full else tgt.describe_brief()
    return tgt.describe()


def estimate_cost(target: SlurmSSHTarget | None, plan: dict) -> float | None:
    if not target:
        return None
    r = plan.get("resources") or {}
    part = target.partitions.get(r.get("partition") or target.default_partition, {})
    rate = part.get("usd_per_hour")
    if rate is None:
        return None
    nodes = max(int(r.get("nodes") or 1), 1)
    hours = _hours(r.get("time_limit") or "01:00:00")
    return round(nodes * hours * float(rate), 2)


def _hours(limit: str) -> float:
    """Slurm time limit -> hours, read the way sbatch --time reads it.

    Without a day part: 'MM', 'MM:SS', 'HH:MM:SS'. With one: 'D-HH', 'D-HH:MM',
    'D-HH:MM:SS'.
    """
    days = 0
    s = str(limit).strip()
    parts: list[int]
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d or 0)
        parts = [int(p or 0) for p in s.split(":")] + [0, 0]
        h, m, sec = parts[0], parts[1], parts[2]
    else:
        parts = [int(p or 0) for p in s.split(":")]
        if len(parts) == 1:
            h, m, sec = 0, parts[0], 0
        elif len(parts) == 2:
            h, m, sec = 0, parts[0], parts[1]
        else:
            h, m, sec = parts[-3], parts[-2], parts[-1]
    return days * 24 + h + m / 60 + sec / 3600


# --------------------------------------------------------------------------- prompts

SUGGEST_PROMPT = """You are a computational scientist reviewing a research report. Propose up to 3
computations that could be run on the HPC cluster below to test, quantify or extend claims in
the report with real software and real data or models.

Rules:
- Only propose what is genuinely computable with open-source software (already installed on
  the cluster, or installable from conda-forge, bioconda, PyPI, Spack or a public container)
  and with inputs that are public (datasets, structures, sequences) or can be generated
  (simulations).
- Choose the best science for the question, not only what is preinstalled; installed
  applications start fastest, so prefer them when they fit equally well.
- Use the cluster's real capabilities when they help: multi-node MPI, GPUs, large-memory
  nodes, spot nodes for sweeps over many parameters.
- Prefer decisive computations that finish in minutes to a few hours.
- If nothing in the report is meaningfully computable, return an empty list. Do not invent
  busywork; news summaries, policy and how-to documents usually have nothing to compute.

{target}

REPORT TITLE: {title}

REPORT:
{text}

Return JSON only, in a ```json block:
{{"suggestions": [{{"title": "short name", "question": "the exact scientific question",
"approach": "method and software in one or two sentences", "software": ["package"],
"est_runtime": "e.g. 20 min", "why": "what it would tell the reader"}}],
"note": "one sentence on how computable this report is"}}"""

PLAN_PROMPT = """You are a computational scientist. Turn the selected material into ONE runnable job
on the HPC cluster below. Use a few web searches (at most 5) to choose the best-established
open-source software for the task and to check exact package names and command-line usage.

{target}

SOURCE REPORT: {title}
SELECTED MATERIAL ({scope}):
{text}
{question_line}
Requirements for the job:
- Real software, real data. Never simulate results with sleep, random numbers presented as
  findings, or hard-coded answers. Tiny demonstration inputs are fine only when labelled as
  such in the plan.
- Software: FIRST check the cluster's installed modules and tested recipes below and use
  them when they fit (exact module names; load the compiler/MPI module before modules that
  need it, e.g. install.modules = ["openmpi", "gromacs/2026.1"]). Installing something
  that is already a module wastes 5-10 minutes. Use a prebuilt image from
  /apps/containers by its path inside the script instead of pulling it.
- Otherwise install software inside the job. Preference after modules: a Pixi environment
  from conda-forge/bioconda (list the packages, the harness installs them with
  `pixi add`); else `pip` packages inside that environment; else an Apptainer image
  (list it under install.apptainer; the harness pulls it and exports IMG_<NAME>, NAME being the
  image name without registry or tag, upper-cased, dashes as underscores, e.g.
  docker://vllm/vllm-openai:v0.6.4 -> $IMG_VLLM_OPENAI; use `apptainer exec --nv` for GPUs);
  Spack only for compiled HPC codes.
- Download public inputs inside the job (curl/wget work; the cluster has outbound internet).
- The run script runs with the working directory set to the job folder; write every result
  file (CSV, JSON, PNG plots, text summaries) into ./outputs/. Keep outputs under 100 MB.
- Print progress lines. Mark phases with `stage "Running"`, `stage "Post-processing"`
  (a shell function the harness provides).
- Use $SLURM_CPUS_ON_NODE for thread counts. For MPI codes launch with `srun` (Slurm
  starts one rank per task across all nodes; set resources.nodes and optionally
  resources.ntasks_per_node). Temporary files go to $TMPDIR (private, node-local).
  Exit non-zero on failure (the harness uses `set -euo pipefail`).
- Parameter sweeps or many independent cases: the `spot` partition costs about half;
  write the script so a rerun skips cases whose output already exists (spot nodes can be
  reclaimed).
- Pick a partition and node count that fit the problem. The time limit covers software
  installation too (a first pip install of PyTorch-based packages can take 5-10 minutes;
  environments are cached for later runs), so leave headroom.

Return JSON only, in a ```json block, with exactly these keys. It must be valid JSON: inside
strings write every backslash as \\\\ (regexes, LaTeX, Windows paths) and line breaks as \\n.
{{"computable": true or false,
"title": "short name",
"question": "the precise question this job answers",
"why_not": "only when computable is false: why, and what nearby question could be computed",
"approach": "2-4 sentences: method, model or dataset, what is measured",
"software": [{{"name": "...", "source": "module|conda-forge|bioconda|pip|apptainer|spack", "version": "optional", "why": "..."}}],
"inputs": ["data or structures used, with URLs where downloaded"],
"parameters": {{"name": value}},
"resources": {{"partition": "...", "nodes": 1, "ntasks_per_node": null, "time_limit": "HH:MM:SS", "gpus": 0}},
"install": {{"modules": [], "conda": ["package", ...], "channels": ["conda-forge"], "pip": ["package", "--extra-index-url https://...", ...], "apptainer": ["docker://image:tag"]}},
"script": "bash commands to run after install (no #SBATCH lines, no install commands). Parameters from 'parameters' are exported as env vars named PARAM_<NAME> in upper case; use them.",
"expected_outputs": ["outputs/..."],
"success_criteria": "how to tell the run worked",
"caveats": "limits of what this computation can show"}}"""

ANALYZE_PROMPT = """You ran a computational job to answer a question raised by a research report.
Write a short results note in Markdown for the report's reader.

QUESTION: {question}
APPROACH: {approach}
PARAMETERS: {params}
SUCCESS CRITERIA: {criteria}
JOB STATE: {state} (Slurm exit {exit_code}, elapsed {elapsed})

OUTPUT FILES:
{files}

TEXT OUTPUTS (truncated):
{texts}

LOG TAIL:
{log}

Rules: report only what the outputs and log show. Quote the key numbers exactly. No LaTeX
(the reader does not render it): write symbols in plain Unicode, e.g. θ, ≤, √2, ×10⁻³. If the job
failed or the outputs do not answer the question, say so plainly and say what to change.
Sections: **Result** (2-4 sentences), **Key numbers** (bullets), **What it means for the
report**, **Limits**, **Next run** (one or two parameter changes worth trying)."""


# --------------------------------------------------------------------------- harness


def _slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "run").lower()).strip("-")[:n] or "run"


def build_sbatch(run_id: int, plan: dict, target: SlurmSSHTarget) -> str:
    r = plan.get("resources") or {}
    part = r.get("partition") or target.default_partition
    if part not in target.partitions and target.partitions:
        part = target.default_partition
    nodes = max(int(r.get("nodes") or 1), 1)
    gpus = int(r.get("gpus") or 0)
    tl = str(r.get("time_limit") or "01:00:00")
    if not re.fullmatch(r"(\d+-)?\d{1,3}(:\d{2}){0,2}", tl):
        tl = "01:00:00"
    inst = plan.get("install") or {}
    mods = [m for m in inst.get("modules") or [] if re.fullmatch(r"[\w./+-]+", str(m))]
    conda = [
        c for c in inst.get("conda") or [] if re.fullmatch(r"[\w.=<>!*,+-]+", str(c))
    ]
    chans = [
        c
        for c in inst.get("channels") or ["conda-forge"]
        if re.fullmatch(r"[\w./:-]+", str(c))
    ]
    pips: list[str] = []
    for p in inst.get("pip") or []:
        p = str(p).strip()
        m = re.fullmatch(r"(--(?:extra-)?index-url)[= ](https://[\w./:-]+)", p)
        if m:  # e.g. the CUDA 12.4 PyTorch wheel index
            pips += [m.group(1), m.group(2)]
        elif re.fullmatch(r"[\w.=<>!\[\],+-]+", p):
            pips.append(p)
    imgs = [
        i for i in inst.get("apptainer") or [] if re.fullmatch(r"[\w./:@-]+", str(i))
    ]
    params = plan.get("parameters") or {}
    exports = "\n".join(
        f"export PARAM_{re.sub(r'[^A-Za-z0-9]', '_', str(k)).upper()}={shlex.quote(str(v))}"
        for k, v in params.items()
    )
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=lab-{run_id}-{_slug(plan.get('title', ''), 24)}",
        f"#SBATCH --partition={part}",
        f"#SBATCH --nodes={nodes}",
        f"#SBATCH --time={tl}",
        "#SBATCH --output=job.log",
        "#SBATCH --open-mode=append",
    ]
    if gpus:
        lines.append(f"#SBATCH --gres=gpu:{gpus}")
    tpn = str(r.get("ntasks_per_node") or "")
    if tpn.isdigit() and int(tpn) > 0:
        lines.append(f"#SBATCH --ntasks-per-node={int(tpn)}")
    if (getattr(target, "partitions", {}) or {}).get(part, {}).get("spot"):
        lines.append("#SBATCH --requeue")  # spot nodes can be reclaimed
    hdr = ((getattr(target, "catalog", None) or {}).get("job_header") or {}).get("path")
    site_header = (
        f"# cluster's site job header (TMPDIR, SCRATCH, caches), published in its catalog\n"
        f"[ -r {shlex.quote(hdr)} ] && . {shlex.quote(hdr)}\n"
        if hdr and re.fullmatch(r"/[\w./-]+", str(hdr))
        else ""
    )
    body = f"""
# Generated by deep-research Lab runs (run #{run_id}). Edit the plan, not this file.
set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
mkdir -p outputs
stage() {{ echo "$1" >> "$SLURM_SUBMIT_DIR/stage.txt"; echo "[STAGE] $1"; }}
export -f stage
trap 'rc=$?; if [ $rc -ne 0 ]; then stage "Failed (exit $rc)"; fi' EXIT
echo "[INFO] job $SLURM_JOB_ID on $(hostname), $SLURM_CPUS_ON_NODE CPUs, $(date -u +%FT%TZ)"
{site_header}{exports}

stage "Installing software"
"""
    if mods:
        body += "module purge >/dev/null 2>&1 || true\n"
        body += "".join(f"module load {m}\n" for m in mods)
    if conda or pips:
        # Readable prefix plus a hash of everything that shapes the environment: a
        # truncated name alone let two different package lists share one cache.
        pkg_names = sorted(
            conda + [x for x in pips if not x.startswith(("--", "https:"))]
        )
        digest = hashlib.sha256(
            json.dumps([pkg_names, sorted(chans), pips]).encode()
        ).hexdigest()[:12]
        env_key = f"{_slug('-'.join(pkg_names), 40)}-{digest}"
        body += f"""export PATH=/apps/pixi/bin:$HOME/.pixi/bin:$PATH
export PIXI_CACHE_DIR=$HOME/.cache/rattler
ENVDIR=$HOME/deep-research-lab/envs/{env_key}
mkdir -p "$(dirname "$ENVDIR")"
# Two jobs needing the same environment build it once: the second waits here.
exec 9>"$ENVDIR.lock"
flock 9
if [ ! -f "$ENVDIR/.ready" ]; then
  rm -rf "$ENVDIR"; mkdir -p "$ENVDIR"
  ( cd "$ENVDIR" && pixi init {" ".join("-c " + shlex.quote(c) for c in chans)} . >/dev/null )
"""
        pkgs = list(conda)
        if pips:
            # pip needs a Python and pip inside the environment; pin Python because
            # wheels for brand-new releases lag (vLLM, PyTorch...)
            if not any(re.match(r"python\b", c) for c in pkgs):
                pkgs.append("python=3.12")
            if not any(re.match(r"pip\b", c) for c in pkgs):
                pkgs.append("pip")
        body += (
            f'  ( cd "$ENVDIR" && pixi add {" ".join(shlex.quote(c) for c in pkgs)} )\n'
        )
        if pips:
            body += f'  ( cd "$ENVDIR" && pixi run python -m pip install --progress-bar off {" ".join(shlex.quote(p) for p in pips)} )\n'
        body += """  touch "$ENVDIR/.ready"
else
  echo "[INFO] reusing cached environment $ENVDIR"
fi
flock -u 9
eval "$(cd "$ENVDIR" && pixi shell-hook)"
# Pip wheels (PyTorch...) pull in the host's old libstdc++ first, which breaks conda
# libraries that need a newer one. Put the environment's own runtime libraries first.
if declare -F ursa_conda_libs >/dev/null; then ursa_conda_libs; else
  export LD_LIBRARY_PATH="$CONDA_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
"""
    if imgs:
        body += """command -v apptainer >/dev/null 2>&1 || module load apptainer 2>/dev/null || true
if ! command -v apptainer >/dev/null 2>&1; then
  echo "[ERROR] apptainer is not installed on $(hostname); this node cannot run container images."
  exit 3
fi
"""
    for img in imgs:
        name = _slug(img, 60)
        body += f"""mkdir -p $HOME/deep-research-lab/images
[ -f $HOME/deep-research-lab/images/{name}.sif ] || apptainer pull $HOME/deep-research-lab/images/{name}.sif {shlex.quote(img)}
export IMG_{_slug(img.rsplit("/", 1)[-1].split(":")[0], 30).upper().replace("-", "_")}=$HOME/deep-research-lab/images/{name}.sif
echo "[INFO] container {img} -> $HOME/deep-research-lab/images/{name}.sif"
"""
    body += f"""
stage "Running"
cat > user_script.sh <<'DR_LAB_EOF'
{plan.get("script", 'echo "no script"; exit 1')}
DR_LAB_EOF
bash -euo pipefail user_script.sh

stage "Done"
echo "[INFO] finished $(date -u +%FT%TZ)"
ls -la outputs
"""
    return "\n".join(lines) + "\n" + body


# --------------------------------------------------------------------------- store + service


class Lab:
    def __init__(
        self,
        db_path: str,
        config_factory: Callable,
        state_dir: Path,
        targets: dict[str, SlurmSSHTarget] | None = None,
    ):
        self.db_path = db_path
        self._config = config_factory
        self.state_dir = state_dir
        self.results_dir = state_dir / "lab"
        self.targets = targets if targets is not None else load_targets(state_dir)
        for t in self.targets.values():
            if getattr(t, "catalog_path", ""):
                try:
                    t.load_catalog(self.state_dir, refresh=False)  # cache only
                except Exception:
                    pass  # no cache yet; fetched on first use
        self._genai = None
        self._client_lock = threading.Lock()
        self._fetch_tries: dict[int, int] = {}
        self._watch_lock = threading.Lock()
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS lab_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    scope TEXT NOT NULL,            -- 'selection' or 'document'
                    selection TEXT,
                    request TEXT,                   -- optional user instruction
                    target TEXT,
                    status TEXT NOT NULL,
                    stage TEXT,
                    plan TEXT,                      -- JSON (editable while draft)
                    script TEXT,                    -- generated sbatch
                    job_id TEXT,
                    slurm_state TEXT,
                    node TEXT,
                    elapsed TEXT,
                    exit_code TEXT,
                    error TEXT,
                    result_md TEXT,
                    files TEXT,                     -- JSON list
                    estimate_usd REAL,
                    ai_cost_usd REAL DEFAULT 0,
                    rerun_of INTEGER,
                    created_at TEXT,
                    updated_at TEXT,
                    submitted_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_lab_runs_session ON lab_runs(session_id);
                CREATE TABLE IF NOT EXISTS lab_suggestions (
                    session_id INTEGER PRIMARY KEY,
                    data TEXT,
                    cost_usd REAL,
                    created_at TEXT
                );
                """
            )

    # ---- plumbing --------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _client(self):
        # One shared client, created under a lock: two planning threads racing here
        # used to build two clients, and the loser was garbage-collected (closing its
        # HTTP session) mid-request: "Cannot send a request, as the client has been
        # closed."
        with self._client_lock:
            if self._genai is None:
                from google import genai

                self._genai = genai.Client(api_key=self._config().api_key)
            return self._genai

    def _model(self) -> str:
        cfg = self._config()
        return getattr(cfg, "followup_model", None) or PLAN_MODEL

    def _ask(self, prompt: str, search: bool) -> tuple[str, float | None]:
        from google.genai import types

        cfg = (
            types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
            if search
            else None
        )
        client = self._client()  # hold a reference for the whole call
        resp = client.models.generate_content(
            model=self._model(), contents=prompt, config=cfg
        )
        cost = _cost(getattr(resp, "usage_metadata", None), _search_count(resp))
        if not (resp.text or "").strip():
            # Flash with Google Search can stop on TOO_MANY_TOOL_CALLS with only
            # thoughts and no answer; surface the reason instead of "no JSON".
            cand = (getattr(resp, "candidates", None) or [None])[0]
            reason = getattr(cand, "finish_reason", None)
            raise EmptyReply(getattr(reason, "name", None) or str(reason), cost)
        return resp.text, cost

    def target(self, name: str | None = None) -> SlurmSSHTarget | None:
        if name and name in self.targets:
            return self.targets[name]
        return next(iter(self.targets.values()), None)

    CATALOG_MAX_AGE_H = 24.0

    def catalog(self, name: str | None = None, refresh: bool = False) -> dict:
        """Load (or refresh) the target's cluster catalog; returns a status summary."""
        tgt = self.target(name)
        if not tgt or not getattr(tgt, "catalog_path", ""):
            return {"available": False, "reason": "no catalog_path configured"}
        err = ""
        try:
            tgt.load_catalog(self.state_dir, refresh=refresh)
        except Exception as e:  # keep any cached copy
            err = str(e)[:300]
        cat = tgt.catalog or {}
        s = cat.get("summary") or {}
        return {
            "available": bool(tgt.catalog),
            "target": tgt.name,
            "fetched": tgt.catalog_fetched,
            "generated": cat.get("generated"),
            "modules": s.get("modules"),
            "spack_packages": s.get("spack_packages"),
            "recipes": len(cat.get("recipes") or []),
            "gpu": s.get("gpu"),
            "error": err,
        }

    def _fresh_catalog(self, tgt: SlurmSSHTarget | None) -> None:
        """Before prompting: refresh a catalog older than a day (errors ignored)."""
        if not tgt or not getattr(tgt, "catalog_path", ""):
            return
        age_h = 1e9
        if tgt.catalog_fetched:
            try:
                then = dt.datetime.fromisoformat(tgt.catalog_fetched)
                if then.tzinfo is not None:
                    then = then.astimezone().replace(tzinfo=None)
                age_h = (dt.datetime.now() - then).total_seconds() / 3600
            except ValueError:
                pass
        try:
            tgt.load_catalog(self.state_dir, refresh=age_h > self.CATALOG_MAX_AGE_H)
        except Exception:
            pass

    def get(self, run_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM lab_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        for k in ("plan", "files"):
            try:
                d[k] = json.loads(d[k]) if d.get(k) else None
            except ValueError:
                pass
        return d

    def runs_for(self, session_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM lab_runs WHERE session_id = ? ORDER BY id DESC",
                (session_id,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def active(self) -> list[dict]:
        marks = ",".join("?" * len(ACTIVE))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM lab_runs WHERE status IN ({marks}) ORDER BY id", ACTIVE
            ).fetchall()
        return [self._row(r) for r in rows]

    def _update(
        self, run_id: int, only_if: tuple[str, ...] | None = None, **fields
    ) -> bool:
        """Write fields; with `only_if`, only while the run is in one of those states.

        Background threads (planning, the watcher) pass `only_if` so a cancel that
        lands while they work is not overwritten. Returns whether the row changed.
        """
        if not fields:
            return False
        for k in ("plan", "files"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k])
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        sql = f"UPDATE lab_runs SET {cols} WHERE id = ?"
        args: list[Any] = [*fields.values(), run_id]
        if only_if:
            sql += f" AND status IN ({','.join('?' * len(only_if))})"
            args += only_if
        with self._conn() as conn:
            changed = conn.execute(sql, args).rowcount > 0
            conn.commit()
        return changed

    def _add_cost(self, run_id: int, usd: float | None) -> None:
        if usd:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE lab_runs SET ai_cost_usd = COALESCE(ai_cost_usd, 0) + ? WHERE id = ?",
                    (usd, run_id),
                )
                conn.commit()

    def active_for(self, session_ids: list[int]) -> list[dict]:
        """Runs of these sessions that still hold (or are about to hold) a Slurm job."""
        if not session_ids:
            return []
        marks = ",".join("?" * len(session_ids))
        states = ",".join("?" * len(ACTIVE))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM lab_runs WHERE session_id IN ({marks}) "
                f"AND status IN ({states}) ORDER BY id",
                (*session_ids, *ACTIVE),
            ).fetchall()
        return [self._row(r) for r in rows]

    def purge_session(self, session_ids: list[int]) -> None:
        import shutil

        marks = ",".join("?" * len(session_ids))
        with self._conn() as conn:
            ids = [
                r[0]
                for r in conn.execute(
                    f"SELECT id FROM lab_runs WHERE session_id IN ({marks})",
                    session_ids,
                )
            ]
            conn.execute(
                f"DELETE FROM lab_runs WHERE session_id IN ({marks})", session_ids
            )
            conn.execute(
                f"DELETE FROM lab_suggestions WHERE session_id IN ({marks})",
                session_ids,
            )
            conn.commit()
        for i in ids:
            shutil.rmtree(self.results_dir / f"run_{i}", ignore_errors=True)

    # ---- suggestions -----------------------------------------------------
    def suggestions(
        self, session_id: int, title: str, text: str, refresh=False
    ) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM lab_suggestions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row and not refresh:
            return {
                **json.loads(row["data"]),
                "cost_usd": row["cost_usd"],
                "cached": True,
            }
        tgt = self.target()
        self._fresh_catalog(tgt)
        prompt = SUGGEST_PROMPT.format(
            target=_describe(tgt, full=False),
            title=title,
            text=_strip_sources(text)[:60000],
        )
        try:
            reply, cost = self._ask(prompt, search=False)
        except EmptyReply as e:
            raise ValueError(f"{e}{_spent(e.cost)}") from e
        try:
            data = extract_json(reply)
        except ValueError as e:
            raise ValueError(f"{e}{_spent(cost)}") from e
        if isinstance(data, list):
            data = {"suggestions": data}
        if not isinstance(data, dict):
            raise ValueError(f"suggestions are not a JSON object{_spent(cost)}")
        data["suggestions"] = [
            s for s in data.get("suggestions") or [] if isinstance(s, dict)
        ][:3]
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO lab_suggestions VALUES (?, ?, ?, ?)",
                (session_id, json.dumps(data), cost, _now()),
            )
            conn.commit()
        return {**data, "cost_usd": cost, "cached": False}

    # ---- plan ------------------------------------------------------------
    def create(
        self,
        session_id: int,
        scope: str,
        selection: str,
        request: str = "",
        target: str | None = None,
        rerun_of: int | None = None,
        plan: dict | None = None,
    ) -> dict:
        tgt = self.target(target)
        now = _now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO lab_runs (session_id, scope, selection, request, target, status, "
                "stage, plan, rerun_of, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    scope,
                    selection,
                    request,
                    tgt.name if tgt else None,
                    "draft" if plan else "planning",
                    "Plan copied for re-run" if plan else "Reading the selection",
                    json.dumps(plan) if plan else None,
                    rerun_of,
                    now,
                    now,
                ),
            )
            conn.commit()
            run_id = cur.lastrowid or 0
        if plan and tgt:
            plan = {**plan, "warnings": validate_plan(tgt, plan)}
            self._update(
                run_id,
                plan=plan,
                script=build_sbatch(run_id, plan, tgt),
                estimate_usd=estimate_cost(tgt, plan),
            )
        return self.get(run_id) or {}

    def make_plan(self, run_id: int, title: str) -> None:
        """Runs in a background thread: AI decomposes the selection into a job plan."""
        run = self.get(run_id)
        if not run:
            return
        tgt = self.target(run["target"])
        try:
            if not self._update(
                run_id,
                only_if=("planning",),
                stage="Researching software and methods (web search)",
            ):
                return  # cancelled before it started
            self._fresh_catalog(tgt)
            prompt = PLAN_PROMPT.format(
                target=_describe(tgt, full=True),
                title=title,
                scope="a highlighted passage"
                if run["scope"] == "selection"
                else "the whole report",
                text=_strip_sources(run["selection"] or "")[:60000],
                question_line=(
                    f"\nTHE USER'S INSTRUCTION: {run['request']}\n"
                    if run.get("request")
                    else ""
                ),
            )
            no_search = ""
            try:
                reply, cost = self._ask(prompt, search=True)
            except EmptyReply as e:
                self._add_cost(run_id, e.cost)
                self._update(
                    run_id,
                    only_if=("planning",),
                    stage="Web search stopped early; planning without it",
                )
                no_search = (
                    f"Planned without web search (search stopped: {e.finish}); "
                    "check package names and flags."
                )
                reply, cost = self._ask(prompt, search=False)
            self._add_cost(run_id, cost)
            plan = extract_json(reply)
            if not isinstance(plan, dict):
                raise ValueError("plan is not a JSON object")
            if no_search:
                plan["caveats"] = " ".join(
                    x for x in (no_search, str(plan.get("caveats") or "")) if x
                )
            if not plan.get("computable", True):
                self._update(
                    run_id,
                    only_if=("planning",),
                    status="plan_failed",
                    stage="Not computable",
                    plan=plan,
                    error=plan.get("why_not")
                    or "The model judged this not computable.",
                )
                return
            for key in ("script", "resources", "install"):
                if key not in plan:
                    raise ValueError(f"plan is missing '{key}'")
            plan["warnings"] = validate_plan(tgt, plan)
            self._update(
                run_id,
                only_if=("planning",),
                status="draft",
                stage="Plan ready for review"
                + (f" ({len(plan['warnings'])} warnings)" if plan["warnings"] else ""),
                plan=plan,
                script=build_sbatch(run_id, plan, tgt) if tgt else None,
                estimate_usd=estimate_cost(tgt, plan),
            )
        except Exception as e:
            self._update(
                run_id,
                only_if=("planning",),
                status="plan_failed",
                stage="Planning failed",
                error=str(e)[:500],
            )

    def edit_plan(self, run_id: int, plan: dict) -> dict:
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] not in ("draft", "plan_failed"):
            raise ValueError(f"run is {run['status']}; only drafts can be edited")
        tgt = self.target(run["target"])
        plan = {**plan, "warnings": validate_plan(tgt, plan)}
        self._update(
            run_id,
            plan=plan,
            status="draft",
            error=None,
            script=build_sbatch(run_id, plan, tgt) if tgt else None,
            estimate_usd=estimate_cost(tgt, plan),
        )
        return self.get(run_id) or {}

    # ---- submit / cancel -------------------------------------------------
    def submit(self, run_id: int) -> dict:
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "draft":
            raise ValueError(
                f"run is {run['status']}; only a reviewed draft can be submitted"
            )
        tgt = self.target(run["target"])
        if not tgt:
            raise ValueError("no compute target configured (lab_targets.json)")
        plan = run["plan"] or {}
        script = build_sbatch(run_id, plan, tgt)
        self._update(
            run_id,
            status="submitting",
            stage="Submitting to " + tgt.label,
            script=script,
        )
        try:
            job = tgt.submit(
                run_id,
                {
                    "run.sbatch": script,
                    "plan.json": json.dumps(plan, indent=2),
                    "README.txt": f"deep-research Lab run #{run_id} for session "
                    f"#{run['session_id']}\n{plan.get('question', '')}\n",
                },
            )
        except Exception as e:
            self._update(
                run_id, status="failed", stage="Submit failed", error=str(e)[:500]
            )
            raise
        self._update(
            run_id,
            status="queued",
            stage="Queued, waiting for a node",
            job_id=job,
            submitted_at=_now(),
        )
        self.ensure_watcher()
        return self.get(run_id) or {}

    def cancel(self, run_id: int) -> dict:
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] in ("planning", "draft", "plan_failed"):
            self._update(run_id, status="cancelled", stage="Cancelled before submit")
        elif run["status"] in ("fetching", "analyzing"):
            # The Slurm job already ended; there is nothing to scancel. Cancelling now
            # skips the rest of the fetch and the AI write-up.
            self._update(
                run_id,
                status="cancelled",
                stage="Cancelled after the job ended",
                finished_at=_now(),
            )
        elif run["status"] in ACTIVE and run.get("job_id"):
            tgt = self.target(run["target"])
            if tgt:
                try:
                    tgt.cancel(run["job_id"])
                except Exception:
                    # scancel fails when the job ended between the page's last poll
                    # and this click; the watcher has then moved the run on.
                    now = self.get(run_id) or run
                    if now["status"] not in ("fetching", "analyzing", *FINAL):
                        raise
                    if now["status"] in FINAL:
                        return now
            self._update(
                run_id,
                only_if=ACTIVE,
                status="cancelled",
                stage="Cancelled",
                finished_at=_now(),
            )
        else:
            raise ValueError(f"run is {run['status']}")
        return self.get(run_id) or {}

    def replan(self, run_id: int) -> dict:
        """Put a run whose planning failed back to `planning` (caller starts make_plan)."""
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "plan_failed":
            raise ValueError(f"run is {run['status']}")
        self._update(
            run_id, status="planning", stage="Retrying the plan", error=None, plan=None
        )
        return self.get(run_id) or {}

    def log(self, run_id: int, offset: int = 0) -> dict:
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        local = self.results_dir / f"run_{run_id}" / "job.log"
        if run["status"] in ("completed", "failed", "cancelled") and local.exists():
            data = local.read_bytes()
            off = offset if 0 <= offset <= len(data) else 0
            return {
                "text": data[off:].decode("utf-8", "replace"),
                "size": len(data),
                "source": "local",
            }
        if not run.get("job_id"):
            return {"text": "", "size": 0, "source": "none"}
        tgt = self.target(run["target"])
        if not tgt:
            return {"text": "", "size": 0, "source": "none"}
        text, size = tgt.log(run_id, offset)
        return {"text": text, "size": size, "source": "cluster"}

    # ---- watcher ---------------------------------------------------------
    def ensure_watcher(self) -> None:
        with self._watch_lock:
            if self._watcher and self._watcher.is_alive():
                return
            if not self.active():
                return
            self._stop.clear()
            self._watcher = threading.Thread(
                target=self._watch_loop, daemon=True, name="lab-watcher"
            )
            self._watcher.start()

    def _watch_loop(self, interval: float = 15.0) -> None:
        idle_rounds = 0
        while not self._stop.is_set():
            runs = self.active()
            if not runs:
                idle_rounds += 1
                if idle_rounds > 2:
                    return
            else:
                idle_rounds = 0
            for run in runs:
                try:
                    self.poll(run)
                except Exception as e:  # network blip: keep the run, try again later
                    self._update(
                        run["id"], only_if=ACTIVE, error=f"watcher: {str(e)[:300]}"
                    )
            self._stop.wait(interval)

    def poll(self, run: dict) -> None:
        """Advance one active run by one step. Safe to call repeatedly."""
        tgt = self.target(run["target"])
        if not tgt or not run.get("job_id"):
            return
        if run["status"] in ("fetching", "analyzing"):
            return self._finish(run, tgt)
        st = tgt.status(run["id"], run["job_id"])
        state = st["slurm_state"]
        stage = st["stage"] or run.get("stage")
        upd: dict[str, Any] = {
            "slurm_state": state or run.get("slurm_state"),
            "node": st["node"] or run.get("node"),
            "elapsed": st["elapsed"] or run.get("elapsed"),
            "error": None,
        }
        if state in ("PENDING", "CONFIGURING") or (
            not state and run["status"] == "queued"
        ):
            reason = st.get("reason") or ""
            label = {
                "PENDING": "Queued, waiting for a node",
                "CONFIGURING": "Node booting",
            }.get(state, "Queued")
            if reason and reason not in ("None", "Priority"):
                label += f" ({reason})"
            upd.update(status="queued", stage=label)
        elif state in ("RUNNING", "COMPLETING"):
            upd.update(status="running", stage=stage or "Running")
        elif state in SLURM_DONE or state.startswith("CANCELLED"):
            upd.update(
                status="fetching",
                stage="Fetching results",
                exit_code=st["exit_code"],
                slurm_state=state,
            )
            # `run` was read at the start of the watcher pass; a cancel since then
            # must win, so every write here is conditional on the run still being live.
            if not self._update(run["id"], only_if=ACTIVE, **upd):
                return
            return self._finish(self.get(run["id"]) or run, tgt)
        self._update(run["id"], only_if=ACTIVE, **upd)

    def _finish(self, run: dict, tgt: SlurmSSHTarget) -> None:
        run = self.get(run["id"]) or run  # the watcher's copy may predate a cancel
        dest = self.results_dir / f"run_{run['id']}"
        state = run.get("slurm_state") or ""
        final = SLURM_DONE.get(state.split()[0] if state else "", "failed")
        if run["status"] == "fetching":
            try:
                files = tgt.fetch(run["id"], dest)
            except Exception as e:
                # A fetch can block the watcher for up to 10 minutes; stop retrying
                # after a few attempts instead of stalling every other run forever.
                tries = self._fetch_tries.get(run["id"], 0) + 1
                self._fetch_tries[run["id"]] = tries
                if tries < MAX_FETCH_TRIES:
                    raise
                self._fetch_tries.pop(run["id"], None)
                self._update(
                    run["id"],
                    only_if=("fetching",),
                    status="failed",
                    stage="Could not fetch results",
                    error=f"fetch failed {tries} times: {str(e)[:300]} "
                    "(the outputs are still on the cluster)",
                    finished_at=_now(),
                )
                return
            self._fetch_tries.pop(run["id"], None)
            if not self._update(
                run["id"],
                only_if=("fetching",),
                files=files,
                status="analyzing",
                stage="Writing up results",
            ):
                return  # cancelled while fetching
            run = self.get(run["id"]) or run
        if run.get("status") != "analyzing":
            return
        note, cost = self._analyze(run, dest, final)
        self._add_cost(run["id"], cost)
        self._update(
            run["id"],
            only_if=("analyzing",),
            status=final,
            stage="Completed"
            if final == "completed"
            else f"Ended: {state or 'unknown'}",
            result_md=note,
            finished_at=_now(),
        )

    def _analyze(self, run: dict, dest: Path, final: str) -> tuple[str, float | None]:
        plan = run.get("plan") or {}
        files = [f for f in run.get("files") or [] if not f.get("skipped")]
        texts = []
        budget = 40000
        for f in files:
            p = dest / f["path"]
            if (
                f["path"].startswith("outputs/")
                and p.suffix.lower()
                in (
                    ".txt",
                    ".csv",
                    ".json",
                    ".md",
                    ".tsv",
                    ".dat",
                    ".log",
                    ".out",
                    ".xvg",
                )
                and p.exists()
                and budget > 0
            ):
                chunk = p.read_text("utf-8", "replace")[: min(8000, budget)]
                texts.append(f"--- {f['path']} ---\n{chunk}")
                budget -= len(chunk)
        log_path = dest / "job.log"
        log_tail = (
            log_path.read_text("utf-8", "replace")[-6000:] if log_path.exists() else ""
        )
        prompt = ANALYZE_PROMPT.format(
            question=plan.get("question", ""),
            approach=plan.get("approach", ""),
            params=json.dumps(plan.get("parameters") or {}),
            criteria=plan.get("success_criteria", ""),
            state=final,
            exit_code=run.get("exit_code"),
            elapsed=run.get("elapsed"),
            files="\n".join(f"- {f['path']} ({f['size']} bytes)" for f in files)
            or "(none)",
            texts="\n\n".join(texts) or "(none)",
            log=log_tail,
        )
        try:
            return self._ask(prompt, search=False)
        except Exception as e:
            return (
                f"**Result**\n\nThe job ended as `{final}`. The AI write-up failed "
                f"({str(e)[:200]}); see the files and log.",
                getattr(e, "cost", None),  # an empty reply is still billed
            )

    def file_path(self, run_id: int, rel: str) -> Path:
        base = (self.results_dir / f"run_{run_id}").resolve()
        p = (base / rel).resolve()
        if not p.is_relative_to(base) or not p.is_file():
            raise FileNotFoundError(rel)
        return p


def _spent(cost: float | None) -> str:
    return f" (${cost:.4f} spent on the attempt)" if cost else ""


def _strip_sources(md: str) -> str:
    m = re.search(r"\n\*\*Sources:?\*\*\s*\n", md or "")
    if m:
        return md[: m.start()]
    m = re.search(r"\n#+\s*Sources\s*\n", md or "")
    return md[: m.start()] if m else (md or "")
