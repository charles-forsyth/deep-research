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

PLAN_MODEL = "gemini-3.8-flash"
# gemini-3.8-flash list prices, USD per 1M tokens (thinking billed as output).
FLASH_IN_1M = 0.75
FLASH_OUT_1M = 3.75
SEARCH_PER_1K = 14.00

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


def _cost(usage) -> float | None:
    if not usage:
        return None
    inp = getattr(usage, "prompt_token_count", 0) or 0
    out = (getattr(usage, "candidates_token_count", 0) or 0) + (
        getattr(usage, "thoughts_token_count", 0) or 0
    )
    tool = getattr(usage, "tool_use_prompt_token_count", 0) or 0
    return round((inp + tool) / 1e6 * FLASH_IN_1M + out / 1e6 * FLASH_OUT_1M, 4)


def extract_json(text: str) -> Any:
    """First JSON object or array in a model reply (fenced or bare)."""
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text or "", re.S)
    body = m.group(1) if m else (text or "")
    try:
        return json.loads(body)
    except ValueError:
        pass
    start = min([i for i in (body.find("{"), body.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("no JSON in model reply")
    opener = body[start]
    closer = "}" if opener == "{" else "]"
    end = body.rfind(closer)
    return json.loads(body[start : end + 1])


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
    """Slurm time limit ('D-HH:MM:SS', 'HH:MM:SS', 'MM') -> hours."""
    days = 0
    s = str(limit).strip()
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d or 0)
    parts = [int(p or 0) for p in s.split(":")]
    if len(parts) == 1:
        h, m, sec = 0, parts[0], 0
    elif len(parts) == 2:
        h, m, sec = parts[0], parts[1], 0
    else:
        h, m, sec = parts[-3], parts[-2], parts[-1]
    return days * 24 + h + m / 60 + sec / 3600


# --------------------------------------------------------------------------- prompts

SUGGEST_PROMPT = """You are a computational scientist reviewing a research report. Propose up to 3
computations that could be run on the HPC cluster below to test, quantify or extend claims in
the report with real software and real data or models.

Rules:
- Only propose what is genuinely computable with open-source software installable from
  conda-forge, bioconda, PyPI, Spack or a public container, and with inputs that are public
  (datasets, structures, sequences) or can be generated (simulations).
- Prefer small, decisive computations that finish in minutes to a few hours.
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
- Install software inside the job. Preference: an existing module; else a Pixi environment
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
- Use $SLURM_CPUS_ON_NODE for thread counts. Exit non-zero on failure (the harness uses
  `set -euo pipefail`).
- Pick a partition and node count that fit the problem. The time limit covers software
  installation too (a first pip install of PyTorch-based packages can take 5-10 minutes;
  environments are cached for later runs), so leave headroom.

Return JSON only, in a ```json block, with exactly these keys:
{{"computable": true or false,
"title": "short name",
"question": "the precise question this job answers",
"why_not": "only when computable is false: why, and what nearby question could be computed",
"approach": "2-4 sentences: method, model or dataset, what is measured",
"software": [{{"name": "...", "source": "module|conda-forge|bioconda|pip|apptainer|spack", "version": "optional", "why": "..."}}],
"inputs": ["data or structures used, with URLs where downloaded"],
"parameters": {{"name": value}},
"resources": {{"partition": "...", "nodes": 1, "time_limit": "HH:MM:SS", "gpus": 0}},
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
    body = f"""
# Generated by deep-research Lab runs (run #{run_id}). Edit the plan, not this file.
set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
mkdir -p outputs
stage() {{ echo "$1" >> "$SLURM_SUBMIT_DIR/stage.txt"; echo "[STAGE] $1"; }}
export -f stage
trap 'rc=$?; if [ $rc -ne 0 ]; then stage "Failed (exit $rc)"; fi' EXIT
echo "[INFO] job $SLURM_JOB_ID on $(hostname), $SLURM_CPUS_ON_NODE CPUs, $(date -u +%FT%TZ)"
{exports}

stage "Installing software"
"""
    if mods:
        body += "module purge >/dev/null 2>&1 || true\n"
        body += "".join(f"module load {m}\n" for m in mods)
    if conda or pips:
        env_key = _slug(
            "-".join(
                sorted(conda + [x for x in pips if not x.startswith(("--", "https:"))])
            ),
            60,
        )
        body += f"""export PATH=/apps/pixi/bin:$HOME/.pixi/bin:$PATH
export PIXI_CACHE_DIR=$HOME/.cache/rattler
ENVDIR=$HOME/deep-research-lab/envs/{env_key}
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
eval "$(cd "$ENVDIR" && pixi shell-hook)"
# Pip wheels (PyTorch...) pull in the host's old libstdc++ first, which breaks conda
# libraries that need a newer one. Put the environment's own runtime libraries first.
export LD_LIBRARY_PATH="$CONDA_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
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
        self._genai = None
        self._client_lock = threading.Lock()
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
            model=PLAN_MODEL, contents=prompt, config=cfg
        )
        cost = _cost(getattr(resp, "usage_metadata", None))
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

    def _update(self, run_id: int, **fields) -> None:
        if not fields:
            return
        for k in ("plan", "files"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k])
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE lab_runs SET {cols} WHERE id = ?", (*fields.values(), run_id)
            )
            conn.commit()

    def _add_cost(self, run_id: int, usd: float | None) -> None:
        if usd:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE lab_runs SET ai_cost_usd = COALESCE(ai_cost_usd, 0) + ? WHERE id = ?",
                    (usd, run_id),
                )
                conn.commit()

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
        prompt = SUGGEST_PROMPT.format(
            target=tgt.describe() if tgt else "No cluster configured.",
            title=title,
            text=_strip_sources(text)[:60000],
        )
        reply, cost = self._ask(prompt, search=False)
        data = extract_json(reply)
        if isinstance(data, list):
            data = {"suggestions": data}
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
            self._update(
                run_id,
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
            self._update(run_id, stage="Researching software and methods (web search)")
            prompt = PLAN_PROMPT.format(
                target=tgt.describe() if tgt else "No cluster configured.",
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
                    run_id, stage="Web search stopped early; planning without it"
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
            self._update(
                run_id,
                status="draft",
                stage="Plan ready for review",
                plan=plan,
                script=build_sbatch(run_id, plan, tgt) if tgt else None,
                estimate_usd=estimate_cost(tgt, plan),
            )
        except Exception as e:
            self._update(
                run_id,
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
        elif run["status"] in ACTIVE and run.get("job_id"):
            tgt = self.target(run["target"])
            if tgt:
                tgt.cancel(run["job_id"])
            self._update(
                run_id, status="cancelled", stage="Cancelled", finished_at=_now()
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
                    self._update(run["id"], error=f"watcher: {str(e)[:300]}")
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
            self._update(run["id"], **upd)
            return self._finish(self.get(run["id"]) or run, tgt)
        self._update(run["id"], **upd)

    def _finish(self, run: dict, tgt: SlurmSSHTarget) -> None:
        dest = self.results_dir / f"run_{run['id']}"
        state = run.get("slurm_state") or ""
        final = SLURM_DONE.get(state.split()[0] if state else "", "failed")
        if run["status"] == "fetching":
            files = tgt.fetch(run["id"], dest)
            self._update(
                run["id"], files=files, status="analyzing", stage="Writing up results"
            )
            run = self.get(run["id"]) or run
        if run.get("status") == "cancelled":
            return
        note, cost = self._analyze(run, dest, final)
        self._add_cost(run["id"], cost)
        self._update(
            run["id"],
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
                None,
            )

    def file_path(self, run_id: int, rel: str) -> Path:
        base = (self.results_dir / f"run_{run_id}").resolve()
        p = (base / rel).resolve()
        if not p.is_relative_to(base) or not p.is_file():
            raise FileNotFoundError(rel)
        return p


def _strip_sources(md: str) -> str:
    m = re.search(r"\n\*\*Sources:?\*\*\s*\n", md or "")
    if m:
        return md[: m.start()]
    m = re.search(r"\n#+\s*Sources\s*\n", md or "")
    return md[: m.start()] if m else (md or "")
