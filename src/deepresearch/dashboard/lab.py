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
import difflib
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
# Lab runs whose submit is running in this process right now (see poll()).
_IN_FLIGHT: set[int] = set()
GONE_POLLS = 8  # empty squeue+sacct polls (~2 min) before a running job is fetched
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
    last: ValueError | None = None
    for text, strict in (
        (body, True),
        (body, False),
        (_ESCAPE.sub(lambda m: m.group(0) if m.group(1) else "\\\\", body), False),
    ):
        try:
            return json.loads(text, strict=strict)
        except json.JSONDecodeError as e:
            last = e
            if e.msg == "Extra data":
                # a complete object followed by more text (a second object, notes):
                # keep the first complete value
                try:
                    return json.JSONDecoder(strict=strict).raw_decode(text.lstrip())[0]
                except ValueError:
                    pass
    raise last or ValueError("no JSON")


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


class NotSubmitted(TargetError):
    """The cluster was never reached: nothing was uploaded and no job exists."""


GCLOUD_LOGIN_HINT = (
    "Google sign-in has expired, so the cluster can't be reached. Run "
    "`gcloud auth login` in a terminal, then press Submit again."
)


def _gcloud_error(stderr: str) -> str:
    """gcloud's last stderr line is often only the tail of its advice ("to select an
    already authenticated account to use."); name the real problem instead."""
    text = (stderr or "").strip()
    low = text.lower()
    if (
        "reauthentication" in low
        or "gcloud auth login" in low
        or "refreshing your current auth tokens" in low
        or "credentials" in low
        and "expired" in low
    ):
        return GCLOUD_LOGIN_HINT
    first = next(
        (ln.strip() for ln in text.splitlines() if ln.strip().startswith("ERROR")),
        None,
    )
    return "gcloud could not build the SSH command: " + (
        first or (text.splitlines() or ["no output"])[-1]
    )


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
                        raise TargetError(_gcloud_error(out.stderr))
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
        try:
            argv, dest = self._base()
        except TargetError as e:
            raise NotSubmitted(str(e)) from e
        try:
            r = subprocess.run(
                argv + [dest, command],
                input=stdin,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise TargetError(f"cluster command timed out after {timeout}s") from e
        if r.returncode == 255:  # ssh itself failed: the command never ran
            with self._lock:
                self._argv = None  # rebuild next time (token or tunnel expired)
            raise NotSubmitted(
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
        # Never write into a folder another run (or another history DB) left behind:
        # move it aside so its logs and outputs survive.
        out = self.sh(
            f"set -e; if [ -e {d}/run.sbatch ] || [ -e {d}/job.log ]; then "
            f"mv {d} {d}.prev-$(date +%Y%m%d%H%M%S); fi; "
            f"mkdir -p {d}/outputs; cd {d}; base64 -d | tar xzf -; "
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
            f"echo '::NF::'; sacct -j {job_id} -n -P --duplicates -o State,NodeList "
            f"2>/dev/null | grep -c NODE_FAIL; "
            f"echo '::ST::'; tail -n 1 {d}/stage.txt 2>/dev/null; "
            f"echo '::SZ::'; stat -c %s {d}/job.log 2>/dev/null || echo 0"
        )
        out = self.sh(cmd, timeout=60)
        acct, rest = (out.split("::SQ::", 1) + [""])[:2]
        sq, rest = (rest.split("::ST::", 1) + [""])[:2]
        sq, nf = (sq.split("::NF::", 1) + ["0"])[:2]
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
            # earlier attempts that died on a failed node (spot capacity, boot
            # failure); Slurm requeues them silently, so "queued" alone hides it
            "node_fails": int((nf.strip() or "0").split()[0] or 0)
            if (nf.strip() or "0").split()[0].isdigit()
            else 0,
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


# What the site Python environment modules provide (import-tested on the cluster; see
# ucr-slurm-production build/python-sci.sbatch). Used to flag redundant installs.
PYTHON_SCI_PACKAGES = set(
    """numpy scipy pandas matplotlib seaborn scikit-learn sklearn statsmodels
sympy numba xarray netcdf4 h5py tables pytables zarr dask polars pyarrow astropy astroquery
skyfield sunpy cartopy shapely geopandas pyproj rasterio networkx biopython pysam
scikit-image opencv pillow jupyterlab ipykernel ipywidgets tqdm requests pyyaml rich
cutadapt multiqc snakemake uv""".split()
)
PYTHON_ML_PACKAGES = set(
    """torch numpy scipy pandas scikit-learn sklearn transformers
jupyterlab""".split()
)
# Site modules that are a whole Python stack. pip packages go into a venv on top of the
# module (build_sbatch), so the module's packages stay importable.
PYTHON_ENV_MODULES = ("python-sci", "python-ml")
# A script that builds its own venv puts another Python first on PATH and hides the
# packages the harness installed (issue #113).
_SCRIPT_VENV = re.compile(
    r"^\s*(?:uv\s+venv|python[0-9.]*\s+-m\s+venv|virtualenv)\b", re.M
)


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
        # packages the loaded Python environment module already provides
        pyenv_mods = [m for m in mods if m.split("/")[0] in ("python-sci", "python-ml")]
        provided = {"python-sci": PYTHON_SCI_PACKAGES, "python-ml": PYTHON_ML_PACKAGES}
        if pyenv_mods:
            have_py = provided.get(pyenv_mods[0].split("/")[0], set())
            redundant = sorted({p for p in pkgs if p in have_py})
            if redundant:
                warns.append(
                    f"{pyenv_mods[0].split('/')[0]} already provides "
                    + ", ".join(redundant)
                    + "; drop these installs (and any venv built only for them)"
                )
        # plan made against an older cluster catalog: its software choices may be stale
        made = plan.get("catalog_generated")
        now = (target.catalog or {}).get("generated")
        if made and now and made != now:
            warns.append(
                f"Planned against the cluster catalog of {str(made)[:16]}; the cluster "
                f"has changed since ({str(now)[:16]}). Re-check the software choices."
            )
        imgs = (plan.get("install") or {}).get("apptainer") or []
        local = {
            c.get("path", "") for c in (target.catalog or {}).get("containers") or []
        }
        for i in imgs:
            if str(i).startswith("/") and str(i) not in local:
                warns.append(f"Container image '{i}' is not in /apps/containers")
    warns += check_script(str(plan.get("script") or ""))
    inst = plan.get("install") or {}
    builds_python = bool(inst.get("pip") or inst.get("conda")) or any(
        str(m).split("/")[0] in PYTHON_ENV_MODULES for m in inst.get("modules") or []
    )
    if builds_python and _SCRIPT_VENV.search(str(plan.get("script") or "")):
        warns.append(
            "The script builds its own Python venv; that hides packages the harness "
            "installs (and the Python module's). List extra packages under install.pip "
            "and drop the venv lines from the script"
        )
    return warns


# Tokens a model emits when it fails to produce a character; they never belong in code.
_MODEL_ARTIFACTS = ("<unk>", "<pad>", "<|endoftext|>", "<eos>", "\ufffd")
_HEREDOC = re.compile(
    r"(?P<cmd>[^\n]*?)<<-?\s*(?P<q>['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=q)[^\n]*\n"
    r"(?P<body>.*?)\n[ \t]*(?P=tag)[ \t]*(?:\n|$)",
    re.S,
)


def check_script(script: str) -> list[str]:
    """Static checks on the run script: model artifacts, bash syntax, embedded Python.

    Catches what would otherwise fail seconds into the job (run #16: a `<unk>` token in
    place of `{` inside an f-string). Python is compiled, never executed.
    """
    if not script.strip():
        return []
    warns: list[str] = []
    for tok in _MODEL_ARTIFACTS:
        n = script.count(tok)
        if n:
            line = next(i for i, ln in enumerate(script.splitlines(), 1) if tok in ln)
            shown = "U+FFFD" if tok == "\ufffd" else tok
            warns.append(
                f"Script contains {n} garbled model token(s) '{shown}' (first on line "
                f"{line}); the code there is incomplete"
            )
    try:
        r = subprocess.run(
            ["bash", "-n"], input=script, text=True, capture_output=True, timeout=10
        )
        if r.returncode != 0:
            msg = (r.stderr or "").strip().splitlines()
            warns.append(
                "Shell syntax error in the script: "
                + (msg[0].replace("bash: ", "", 1) if msg else "bash -n failed")[:200]
            )
    except (OSError, subprocess.TimeoutExpired):
        pass  # no bash here: skip, the cluster will still report it
    for m in _HEREDOC.finditer(script):
        cmd, body = m.group("cmd"), m.group("body")
        if m.group("q") == "" and "$" in body:
            continue  # unquoted heredoc: the shell expands it first, can't compile as-is
        is_py = re.search(r"\bpython[0-9.]*\b", cmd) or re.search(
            r"cat\s*>\s*\S+\.py\b", cmd
        )
        if not is_py:
            continue
        try:
            compile(body, m.group("tag"), "exec")
        except SyntaxError as e:
            start = script[: m.start("body")].count("\n") + 1
            ln = start + (e.lineno or 1) - 1
            warns.append(
                f"Python syntax error in the script (line {ln}): {e.msg}"
                + (f": {e.text.strip()[:80]}" if e.text else "")
            )
    return warns


def _dropped_options(old: dict, new: dict) -> list[str]:
    """Command-line options (--name) present in the old script but gone from the new one,
    unless the new plan replaces the software they belonged to (install lists changed)."""
    opt = re.compile(r"(?<![\w-])--[a-zA-Z][\w-]+")
    before = set(opt.findall(str(old.get("script") or "")))
    after = set(opt.findall(str(new.get("script") or "")))
    return sorted(before - after)


def _log_for_fix(log: str, limit: int = 9000) -> str:
    """The part of a job log that explains a failure: its end, plus the first traceback
    or ERROR block when that sits earlier (tools often print pages after the real error)."""
    log = log or ""
    if len(log) <= limit:
        return log or "(no log)"
    tail = log[-(limit * 2 // 3) :]
    m = re.search(
        r"(Traceback \(most recent call last\)|^ERROR|^Error|error:)", log, re.M
    )
    if m and m.start() < len(log) - len(tail):
        head = log[max(0, m.start() - 500) : m.start() + limit // 3]
        return head + "\n[...]\n" + tail
    return log[-limit:]


def _describe(tgt, full: bool) -> str:
    if not tgt:
        return "No cluster configured."
    if getattr(tgt, "catalog", None):
        return tgt.describe_full() if full else tgt.describe_brief()
    return tgt.describe()


def _int(v: Any, default: int) -> int:
    """A resource count from a plan ('2', 2, 2.0) or the default ('auto', '1.5x')."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return int(f) if f == int(f) else default


TIME_RE = re.compile(r"(\d+-)?\d{1,3}(:\d{2}){0,2}")


def estimate_cost(target: SlurmSSHTarget | None, plan: dict) -> float | None:
    if not target:
        return None
    r = plan.get("resources") or {}
    part = target.partitions.get(r.get("partition") or target.default_partition, {})
    rate = part.get("usd_per_hour")
    if rate is None:
        return None
    nodes = max(_int(r.get("nodes"), 1), 1)
    tl = str(r.get("time_limit") or "01:00:00")
    if not TIME_RE.fullmatch(tl):
        tl = "01:00:00"  # build_sbatch falls back the same way
    return round(nodes * _hours(tl) * float(rate), 2)


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

FIX_PROMPT = """You are fixing a job plan for the HPC cluster below. A pre-flight check found
the problems listed under PROBLEMS. Change ONLY what is needed to fix them: modules, install
lists, resources, the few script lines that set up software, and lines with reported
syntax errors. Keep the science, the
method, the parameters, the inputs and the outputs exactly as they are.

{target}

PROBLEMS:
{problems}

CURRENT PLAN (JSON):
{plan}

Rules:
- Use only module names from the cluster description above, loaded the way it says.
- Python work: load exactly one of python-sci (CPU science: numpy scipy pandas matplotlib
  skyfield astropy xarray ...) or python-ml (PyTorch/GPU). Drop pip/conda installs of packages
  that module already provides, and drop script lines that build a separate venv for them.
  Extra packages the module lacks go in install.pip; the harness installs them into a venv
  on top of the module, so the module's packages stay importable. Never build a venv in the
  script (uv venv, python -m venv): it hides those packages.
- Syntax errors and garbled model tokens (<unk> and similar) in the script: repair only
  those lines so the code is what was evidently intended; change nothing else in the script.
- If a problem cannot be fixed (the science needs something the cluster lacks), leave the
  plan unchanged and say why in "notes".
- Inside JSON strings write every backslash as \\ and line breaks as \n.

Return JSON only, in a ```json block:
{{"plan": <the complete corrected plan, same keys as the current plan>,
"changes": ["one short line per change, e.g. 'python/3.12.14 -> python-sci'"],
"notes": "anything the reviewer should know, or empty"}}"""


RUNFIX_PROMPT = """A job you planned ran on the HPC cluster below and FAILED. Fix the plan so a
rerun gets past this failure. Change ONLY what the error shows is wrong: the lines that
failed, a wrong flag, a missing package or module, a file-format assumption, or resources
(time, memory, partition) if the job was killed for them. Make the SMALLEST change that
fixes it: prefer removing or replacing an unsupported flag, or fixing the lines that
failed, over switching software versions or container images. Change a version only
when the method truly needs it, and then say in "notes" what that risks (e.g. whether
the cluster's GPU driver supports it). Keep the science, method,
parameters and outputs as they are. Do not add retries or try/except that would hide the
error; fix its cause.

{target}

SLURM STATE: {state} (exit {exit_code}, elapsed {elapsed})

END OF THE JOB LOG:
{log}

PLAN THAT FAILED (JSON):
{plan}

Rules:
- Only modules from the cluster description above; one Python environment module at most.
- Inside JSON strings write every backslash as \\\\ and line breaks as \\n.
- An error message can be a symptom. Trace it back through the log to the first thing
  that went wrong (e.g. a model that "cannot estimate a rate" because too few inputs were
  kept: fix why they were dropped, do not remove the analysis option that complained).
  Never remove an analysis step, option or output to make an error go away.
- Every change must appear in "changes". A change you cannot name an error for is not
  allowed.
- If the log does not show why it failed, change nothing and say so in "notes".

Return JSON only, in a ```json block:
{{"plan": <the complete corrected plan, same keys>,
"changes": ["one short line per change, naming the error it fixes"],
"notes": "anything the reviewer should know, or empty"}}"""


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
- Check inputs before the heavy step: right after inputs are downloaded or generated, add a
  few lines under `stage "Checking inputs"` that stop the job (exit 3) with a one-line
  reason if the inputs cannot give a meaningful answer. Fit the check to the job: for
  downloaded data, the record count, coverage of the requested range (dates, regions,
  energies...) and unique identifiers; for generated inputs, that each input file exists
  and is non-empty; for benchmarks, that the server or model answers before timing starts.
  Print what was checked, e.g. "inputs OK: 100 sequences, years 2015-2024, all names
  unique". Check the files the job actually got (after the download), not whether a
  website is reachable. Keep it to seconds and to things that make the result
  meaningless if wrong; do not check the science. Set thresholds loosely (for example at
  least half the requested records) so a check does not stop a run that would have worked.
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

Rules: report only what the outputs and log show. Quote the key numbers exactly. Take
counts of inputs (samples, sequences, structures, cases) from the input or log lines that
state them, not from derived structures (a tree also has internal nodes, a mesh has cells). No LaTeX
(the reader does not render it): write symbols in plain Unicode, e.g. θ, ≤, √2, ×10⁻³. If the job
failed or the outputs do not answer the question, say so plainly and say what to change.
Sections: **Result** (2-4 sentences), **Key numbers** (bullets), **What it means for the
report**, **Limits**, **Next run** (one or two parameter changes worth trying)."""


# --------------------------------------------------------------------------- harness


def _slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "run").lower()).strip("-")[:n] or "run"


def build_sbatch(
    run_id: int, plan: dict, target: SlurmSSHTarget, sources: list | None = None
) -> str:
    r = plan.get("resources") or {}
    part = str(r.get("partition") or target.default_partition)
    if part not in target.partitions and target.partitions:
        part = target.default_partition
    if not re.fullmatch(r"[\w.-]+", part or ""):
        # never let a plan value put extra lines into run.sbatch
        part = target.default_partition
    nodes = max(_int(r.get("nodes"), 1), 1)
    gpus = max(_int(r.get("gpus"), 0), 0)
    tl = str(r.get("time_limit") or "01:00:00")
    if not TIME_RE.fullmatch(tl):
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
    pymods = [m for m in mods if m.split("/")[0] in PYTHON_ENV_MODULES]
    if pips and not conda and len(pymods) == 1:
        # pip packages on top of a Python environment module (python-sci/python-ml):
        # a venv on the module's own Python with --system-site-packages, so the module's
        # numpy/pandas/matplotlib stay importable and pip adds only what is missing. A
        # separate Pixi Python here hid the module's packages (issue #113, run #30).
        pkg_names = sorted(x for x in pips if not x.startswith(("--", "https:")))
        digest = hashlib.sha256(
            json.dumps([pymods[0], pkg_names, pips]).encode()
        ).hexdigest()[:12]
        env_key = f"{_slug(pymods[0] + '-' + '-'.join(pkg_names), 40)}-{digest}"
        body += f"""ENVDIR=$HOME/deep-research-lab/envs/{env_key}
mkdir -p "$(dirname "$ENVDIR")"
# Two jobs needing the same environment build it once: the second waits here.
exec 9>"$ENVDIR.lock"
flock 9
if [ ! -f "$ENVDIR/.ready" ]; then
  rm -rf "$ENVDIR"
  python3 -m venv --system-site-packages "$ENVDIR"
  "$ENVDIR/bin/python" -m pip install --progress-bar off {" ".join(shlex.quote(p) for p in pips)}
  touch "$ENVDIR/.ready"
else
  echo "[INFO] reusing cached environment $ENVDIR"
fi
flock -u 9
. "$ENVDIR/bin/activate"
echo "[INFO] python $(command -v python) on top of {pymods[0]}"
"""
    elif conda or pips:
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
    if sources:
        from deepresearch.sources.staging import staging_block

        body += staging_block(
            sources, getattr(target, "remote_root", "~/deep-research-lab")
        )
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


PLAN_DIFF_SKIP = {
    "warnings", "plan_before_fix", "fix_changes", "fix_notes", "fix_diff",
    "catalog_generated", "caveats",
}  # fmt: skip


def plan_diff(before: dict, after: dict, max_lines: int = 400) -> dict:
    """What an AI fix changed: a unified diff of the script and the other plan fields.

    Returns {"script": "<unified diff>", "fields": [{"key", "before", "after"}],
    "truncated": bool}. Shown in plan review so the reviewer sees the edit, not only
    the model's own summary of it.
    """
    a = str((before or {}).get("script") or "").splitlines()
    b = str((after or {}).get("script") or "").splitlines()
    lines = list(difflib.unified_diff(a, b, "before", "after", n=2, lineterm=""))
    fields = []
    for k in sorted(set(before or {}) | set(after or {})):
        if k == "script" or k in PLAN_DIFF_SKIP:
            continue
        x, y = (before or {}).get(k), (after or {}).get(k)
        if x != y:
            fields.append(
                {
                    "key": k,
                    "before": json.dumps(x, sort_keys=True)[:600],
                    "after": json.dumps(y, sort_keys=True)[:600],
                }
            )
    return {
        "script": "\n".join(lines[:max_lines]),
        "fields": fields,
        "truncated": len(lines) > max_lines,
    }


NODE_FAIL_WARN = 3  # node failures before the queue label suggests another partition


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
        self._gone_polls: dict[int, int] = {}
        self._watch_lock = threading.Lock()
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()
        from deepresearch.sources import SourceRegistry

        self.sources = SourceRegistry(db_path)
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
            cols = {r[1] for r in conn.execute("PRAGMA table_info(lab_runs)")}
            if "data_sources" not in cols:  # names picked at launch, for the planner
                conn.execute("ALTER TABLE lab_runs ADD COLUMN data_sources TEXT")
            conn.commit()

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
        for k in ("plan", "files", "data_sources"):
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

    def all_runs(self, limit: int = 500) -> list[dict]:
        """Every run, newest first, with the report title (for the All Lab runs page)."""
        with self._conn() as conn:
            has_sessions = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
            q = (
                "SELECT r.*, s.prompt AS session_prompt FROM lab_runs r "
                "LEFT JOIN sessions s ON s.id = r.session_id"
                if has_sessions
                else "SELECT r.*, NULL AS session_prompt FROM lab_runs r"
            )
            rows = conn.execute(q + " ORDER BY r.id DESC LIMIT ?", (limit,)).fetchall()
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
        data_sources: list[str] | None = None,
    ) -> dict:
        tgt = self.target(target)
        for n in data_sources or []:
            if self.sources.get(n) is None:
                raise ValueError(f"data source '{n}' does not exist")
        now = _now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO lab_runs (session_id, scope, selection, request, target, status, "
                "stage, plan, rerun_of, created_at, updated_at, data_sources) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    json.dumps(data_sources) if data_sources else None,
                ),
            )
            conn.commit()
            run_id = cur.lastrowid or 0
        if plan and tgt:
            plan = {**plan, "warnings": self._check(tgt, plan)}
            self._update(
                run_id,
                plan=plan,
                script=build_sbatch(run_id, plan, tgt, self._safe_sources(plan)),
                estimate_usd=estimate_cost(tgt, plan),
            )
        return self.get(run_id) or {}

    # ---- data sources ---------------------------------------------------
    def _plan_sources(self, plan: dict) -> list:
        """The registry records for plan["data_sources"]; unknown names are errors."""
        names = [str(n) for n in (plan or {}).get("data_sources") or []]
        out = []
        for n in names:
            s = self.sources.get(n)
            if s is None:
                raise ValueError(f"data source '{n}' does not exist")
            out.append(s)
        return out

    def _check(self, tgt, plan: dict) -> list[str]:
        return validate_plan(tgt, plan) + self.source_warnings(plan)

    def _safe_sources(self, plan: dict) -> list:
        """Like _plan_sources for previews: unknown names are skipped (they show up as
        a pre-flight warning instead of breaking the plan view)."""
        return [
            s
            for n in (plan or {}).get("data_sources") or []
            if (s := self.sources.get(str(n))) is not None
        ]

    def source_warnings(self, plan: dict) -> list[str]:
        """Pre-flight for data: unknown or unreachable sources, relay size, and a script
        that ignores the staged copy."""
        from deepresearch.sources.staging import RELAY_MAX_BYTES

        warns: list[str] = []
        script = str((plan or {}).get("script") or "")
        for n in (plan or {}).get("data_sources") or []:
            s = self.sources.get(str(n))
            if s is None:
                warns.append(f"Data source '{n}' does not exist")
                continue
            if s.status == "unreachable":
                warns.append(
                    f"Data source '{s.name}' failed its last test: {s.last_error}"
                )
            size = s.manifest.total_bytes if s.manifest else 0
            cap = int(s.options.get("max_relay_bytes") or RELAY_MAX_BYTES)
            if s.effective_staging == "relay" and size > cap:
                warns.append(
                    f"Data source '{s.name}' is {size / 1024**3:.1f} GB, over the relay "
                    f"limit ({cap / 1024**3:.1f} GB); use direct staging or raise the limit"
                )
            if script and s.env_var not in script:
                warns.append(
                    f"The script never reads ${s.env_var}; data source '{s.name}' would "
                    "be staged but unused"
                )
        return warns

    def _data_note(self, run: dict, tgt) -> str:
        from deepresearch.sources.staging import plan_data_note

        try:
            srcs = self._plan_sources({"data_sources": run.get("data_sources") or []})
        except ValueError:
            return ""
        return "\n" + plan_data_note(
            srcs, getattr(tgt, "remote_root", "~/deep-research-lab")
        )

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
                )
                + self._data_note(run, tgt),
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
            if run.get("data_sources"):
                plan["data_sources"] = list(run["data_sources"])
            plan["warnings"] = self._check(tgt, plan)
            if tgt and getattr(tgt, "catalog", None):
                plan["catalog_generated"] = tgt.catalog.get("generated")  # type: ignore[union-attr]
            self._update(
                run_id,
                only_if=("planning",),
                status="draft",
                stage="Plan ready for review"
                + (f" ({len(plan['warnings'])} warnings)" if plan["warnings"] else ""),
                plan=plan,
                script=build_sbatch(run_id, plan, tgt, self._safe_sources(plan))
                if tgt
                else None,
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
        plan = {**plan, "warnings": self._check(tgt, plan)}
        if not self._update(
            run_id,
            only_if=("draft", "plan_failed"),
            plan=plan,
            status="draft",
            error=None,
            script=build_sbatch(run_id, plan, tgt, self._safe_sources(plan))
            if tgt
            else None,
            estimate_usd=estimate_cost(tgt, plan),
        ):
            now = self.get(run_id) or run
            raise ValueError(f"run is {now['status']}; only drafts can be edited")
        return self.get(run_id) or {}

    FIX_MAX_ROUNDS = 2

    def fix_plan(self, run_id: int) -> dict:
        """Ask the planner to fix only what pre-flight flagged, then check again.

        Never submits. Stores the corrected plan as the draft and keeps the previous
        plan in `plan_before_fix` so the reviewer can undo. Up to FIX_MAX_ROUNDS model
        calls: a second round only if the first left warnings. Returns the run plus
        `fix` = {changes, notes, rounds, remaining}.
        """
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "draft":
            raise ValueError(f"run is {run['status']}; only drafts can be fixed")
        tgt = self.target(run["target"])
        if not tgt:
            raise ValueError("no cluster target configured")
        self._fresh_catalog(tgt)
        original = dict(run.get("plan") or {})
        plan = dict(original)
        plan.pop("plan_before_fix", None)
        warns = self._check(tgt, plan)  # includes "planned against an older catalog"
        if not warns:
            return {
                **(self.get(run_id) or {}),
                "fix": {
                    "changes": [],
                    "notes": "No problems found against the current cluster catalog.",
                    "rounds": 0,
                    "remaining": [],
                },
            }
        changes: list[str] = []
        notes: list[str] = []
        rounds = 0
        while warns and rounds < self.FIX_MAX_ROUNDS:
            rounds += 1
            body = {
                k: v
                for k, v in plan.items()
                if k not in ("warnings", "plan_before_fix")
            }
            prompt = FIX_PROMPT.format(
                target=_describe(tgt, full=True),
                problems="\n".join(f"- {w}" for w in warns),
                plan=json.dumps(body, indent=1)[:60000],
            )
            reply, cost = self._ask(prompt, search=False)
            self._add_cost(run_id, cost)
            out = extract_json(reply)
            new = out.get("plan") if isinstance(out, dict) else None
            if not isinstance(new, dict) or not all(
                k in new for k in ("script", "resources", "install")
            ):
                notes.append("The model returned no usable plan; nothing changed.")
                break
            changes += [str(c) for c in out.get("changes") or []][:20]
            if out.get("notes"):
                notes.append(str(out["notes"]))
            plan = new
            if getattr(tgt, "catalog", None):
                plan["catalog_generated"] = tgt.catalog.get("generated")  # type: ignore[union-attr]
            warns = self._check(tgt, plan)
        before = {
            k: v
            for k, v in original.items()
            if k not in ("warnings", "plan_before_fix")
        }
        if getattr(tgt, "catalog", None):
            plan["catalog_generated"] = tgt.catalog.get("generated")  # type: ignore[union-attr]
        warns = self._check(tgt, plan)
        plan = {
            **plan,
            "warnings": warns,
            "plan_before_fix": before,
            "fix_changes": changes,
            "fix_notes": " ".join(notes),
            "fix_diff": plan_diff(before, plan),
        }
        if not self._update(
            run_id,
            only_if=("draft",),
            plan=plan,
            stage="Plan fixed by AI, re-checked"
            + (f" ({len(warns)} warnings left)" if warns else " (no warnings)"),
            script=build_sbatch(run_id, plan, tgt, self._safe_sources(plan)),
            estimate_usd=estimate_cost(tgt, plan),
        ):
            now = self.get(run_id) or run
            raise ValueError(
                f"run is {now['status']} now; the AI fix was not applied to it"
            )
        return {
            **(self.get(run_id) or {}),
            "fix": {
                "changes": changes,
                "notes": " ".join(notes),
                "rounds": rounds,
                "remaining": warns,
            },
        }

    def undo_fix(self, run_id: int) -> dict:
        """Restore the plan as it was before the last AI fix."""
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "draft":
            raise ValueError(f"run is {run['status']}; only drafts can be changed")
        before = (run.get("plan") or {}).get("plan_before_fix")
        if not isinstance(before, dict):
            raise ValueError("no AI fix to undo")
        return self.edit_plan(run_id, before)

    def fix_failed(self, run_id: int) -> dict:
        """A failed run: ask the planner to fix what the job log shows, as a new draft.

        The failed run is left untouched. The new run is a draft (`rerun_of` = the failed
        run) that goes through pre-flight like any plan; nothing is submitted. Returns the
        new run plus `fix` = {changes, notes, remaining}.
        """
        run = self.get(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] != "failed":
            raise ValueError(
                f"run is {run['status']}; only failed runs can be fixed this way"
            )
        tgt = self.target(run["target"])
        if not tgt:
            raise ValueError("no cluster target configured")
        plan = {
            k: v
            for k, v in (run.get("plan") or {}).items()
            if k not in PLAN_DIFF_SKIP - {"catalog_generated", "caveats"}
        }
        if not plan.get("script"):
            raise ValueError("the failed run has no plan to fix")
        log_path = self.results_dir / f"run_{run_id}" / "job.log"
        log = log_path.read_text("utf-8", "replace") if log_path.exists() else ""
        if not log.strip() and run.get("job_id"):
            try:
                log, _ = tgt.log(run_id, 0)
            except Exception:  # cluster unreachable: fix from state alone
                log = ""
        self._fresh_catalog(tgt)
        prompt = RUNFIX_PROMPT.format(
            target=_describe(tgt, full=True),
            state=run.get("slurm_state") or run.get("stage") or "FAILED",
            exit_code=run.get("exit_code"),
            elapsed=run.get("elapsed"),
            log=_log_for_fix(log),
            plan=json.dumps(plan, indent=1)[:60000],
        )
        new: dict | None = None
        changes: list[str] = []
        notes = ""
        for attempt in range(2):
            reply, cost = self._ask(
                prompt
                if attempt == 0
                else prompt
                + '\n\nYour previous answer changed the plan but its "changes" list was '
                'empty. Return the same fix again with one line per change in "changes".',
                search=False,
            )
            self._add_cost(run_id, cost)
            try:
                out = extract_json(reply)
            except ValueError:
                out = None
            cand = out.get("plan") if isinstance(out, dict) else None
            if not isinstance(cand, dict) or not all(
                k in cand for k in ("script", "resources", "install")
            ):
                continue
            new = cand
            changes = [str(c) for c in (out.get("changes") or [])][:20]  # type: ignore[union-attr]
            notes = str(out.get("notes") or "")  # type: ignore[union-attr]
            if changes or new == plan:
                break  # a described fix, or an honest "nothing to fix"
        if new is None:
            raise ValueError("the model returned no usable plan; nothing changed")
        dropped = _dropped_options(plan, new)
        if dropped:
            # Removing an analysis option is how a fix hides an error (run #25: the model
            # dropped --covariation instead of fixing why inputs were thrown away). Keep
            # the fix, but say so loudly so the reviewer decides.
            notes = (
                f"REVIEW: this fix removes option(s) {', '.join(dropped)} from the "
                "script. Removing an option can hide an error instead of fixing it; "
                "check the log for an earlier cause before accepting. " + notes
            ).strip()
        if not changes and new != plan:
            raise ValueError(
                "the AI changed the plan without saying what or why; nothing was "
                "created. Edit the plan by hand, or try again."
            )
        if not changes:
            raise ValueError(
                "the AI found nothing to fix from the log"
                + (f": {notes[:300]}" if notes else "")
            )
        new["fix_changes"] = changes
        new["fix_notes"] = notes
        new["fix_diff"] = plan_diff(
            {k: v for k, v in plan.items() if k not in PLAN_DIFF_SKIP}, new
        )
        new["caveats"] = (
            str(new.get("caveats") or "")
            + f" AI fix of failed run #{run_id}: "
            + "; ".join(changes)
        ).strip()
        created = self.create(
            run["session_id"],
            run["scope"],
            run.get("selection") or "",
            run.get("request") or "",
            run.get("target"),
            rerun_of=run_id,
            plan=new,
        )
        return {
            **created,
            "fix": {
                "changes": changes,
                "notes": notes,
                "remaining": (created.get("plan") or {}).get("warnings") or [],
            },
        }

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
        # Claim the draft atomically: a double-click or a second tab must not send
        # a second Slurm job (the first would be orphaned and keep billing).
        if not self._update(
            run_id,
            only_if=("draft",),
            status="submitting",
            stage="Submitting to " + tgt.label,
            submitted_at=_now(),
        ):
            now = self.get(run_id) or run
            raise ValueError(f"run is {now['status']}; it was already submitted")
        _IN_FLIGHT.add(run_id)
        try:
            plan = run["plan"] or {}
            sources = self._plan_sources(plan)
            from deepresearch.sources.staging import relay_upload, sources_json

            for src in sources:
                if src.effective_staging == "relay":
                    self._update(
                        run_id, stage=f"Uploading data source {src.name} to the cluster"
                    )
                    # Re-lists the source first: the cluster copy, its folder name and
                    # the provenance all follow today's contents.
                    relay_upload(tgt, src, log=lambda m: None)
                    self.sources.save_check(src)
            # Built after staging so $DS_<NAME> points at the folder just uploaded.
            script = build_sbatch(run_id, plan, tgt, sources)
            self._update(run_id, script=script)
            files = {
                "run.sbatch": script,
                "plan.json": json.dumps(plan, indent=2),
                "README.txt": f"deep-research Lab run #{run_id} for session "
                f"#{run['session_id']}\n{plan.get('question', '')}\n",
            }
            if sources:
                files["sources.json"] = sources_json(
                    sources, getattr(tgt, "remote_root", "~/deep-research-lab")
                )
            job = tgt.submit(run_id, files)
            for src in sources:
                self.sources.record_use(src, "lab_run", run_id)
        except NotSubmitted as e:
            # Nothing reached the cluster (sign-in expired, VPN or tunnel down): keep
            # the reviewed plan as a draft so Submit works again once it's fixed,
            # instead of a failed run whose only way back is a new draft.
            _IN_FLIGHT.discard(run_id)
            self._update(
                run_id,
                only_if=("submitting",),
                status="draft",
                stage="Not submitted",
                error=str(e)[:500],
                submitted_at=None,
            )
            raise
        except Exception as e:
            _IN_FLIGHT.discard(run_id)
            self._update(
                run_id,
                only_if=("submitting",),
                status="failed",
                stage="Submit failed",
                error=str(e)[:500],
                finished_at=_now(),
            )
            raise
        _IN_FLIGHT.discard(run_id)
        if not self._update(
            run_id,
            only_if=("submitting",),
            status="queued",
            stage="Queued, waiting for a node",
            job_id=job,
            submitted_at=_now(),
        ):
            # Cancelled while the upload/sbatch was in flight: the job exists now,
            # so stop it rather than leave it billing untracked.
            self._update(run_id, job_id=job)
            try:
                tgt.cancel(job)
            except Exception:
                pass
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
        elif run["status"] == "submitting" and not run.get("job_id"):
            # No job id yet: either the upload/sbatch is still in flight (submit sees
            # the cancel and scancels the job it gets back) or the dashboard died
            # mid-submit and nothing will ever finish it.
            self._update(
                run_id,
                only_if=("submitting",),
                status="cancelled",
                stage="Cancelled during submit",
                error="If a job id appears on the cluster later, cancel it there "
                "(squeue --me).",
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
        if (
            run["status"] == "submitting"
            and not run.get("job_id")
            and run["id"] not in _IN_FLIGHT
        ):
            # Left over from a dashboard that stopped mid-submit: nothing will ever
            # finish it, and it would block cancel/delete and keep the watcher busy.
            self._update(
                run["id"],
                only_if=("submitting",),
                status="failed",
                stage="Submit interrupted",
                error="The dashboard stopped while submitting this run. Check the "
                "cluster (squeue --me) for a stray job, then Re-run.",
                finished_at=_now(),
            )
            return
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
            fails = int(st.get("node_fails") or 0)
            if fails:
                part = ((run.get("plan") or {}).get("resources") or {}).get("partition")
                label = (
                    f"Requeued after {fails} node failure{'s' if fails != 1 else ''}"
                    + (f" on {part}" if part else "")
                    + ": the cluster could not start a node"
                    + (
                        ". Consider cancelling and resubmitting on another partition"
                        if fails >= NODE_FAIL_WARN
                        else ""
                    )
                )
            upd.update(status="queued", stage=label)
        elif state in ("RUNNING", "COMPLETING"):
            upd.update(status="running", stage=stage or "Running")
        elif not state and run["status"] == "running":
            # squeue and sacct both know nothing (accounting off, or the record aged
            # out while the laptop slept). The job's own stage markers decide; after
            # a few empty polls with no marker, fetch whatever is there.
            last = (st.get("stage") or "").strip()
            gone = self._gone_polls.get(run["id"], 0) + 1
            self._gone_polls[run["id"]] = gone
            if last == "Done" or last.startswith("Failed") or gone >= GONE_POLLS:
                self._gone_polls.pop(run["id"], None)
                inferred = "COMPLETED" if last == "Done" else "FAILED"
                upd.update(
                    status="fetching",
                    stage="Fetching results (Slurm no longer lists the job)",
                    slurm_state=inferred,
                )
                if not self._update(run["id"], only_if=ACTIVE, **upd):
                    return
                return self._finish(self.get(run["id"]) or run, tgt)
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
