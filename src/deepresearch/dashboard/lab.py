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
import ast
import hashlib
import json
import os
import re
import shlex
import sys
import sqlite3
import subprocess
import threading
import time

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from deepresearch.dashboard import labguard

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
    "smoke",  # cut-down run on the warm node before the real one
    "queued",  # accepted by Slurm, waiting for a node
    "running",  # the batch script is running (installing or computing)
    "fetching",  # copying results back
    "analyzing",  # AI is writing the results note
    "completed",
    "failed",
    "cancelled",
)
ACTIVE = ("submitting", "smoke", "queued", "running", "fetching", "analyzing")
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
_SMOKE_FIXING: set[int] = set()  # runs whose AI smoke fix runs in this process
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


def _tar_b64(files: dict[str, str]) -> bytes:
    """Files as a base64 tar.gz for `base64 -d | tar xzf -` on the cluster."""
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
    return base64.b64encode(buf.getvalue())


def _worker_script() -> str:
    return (Path(__file__).parent / "warm_worker.sh").read_text()


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
        # Never write into a folder another run (or another history DB) left behind:
        # move it aside so its logs and outputs survive.
        out = self.sh(
            f"set -e; if [ -e {d}/run.sbatch ] || [ -e {d}/job.log ]; then "
            f"mv {d} {d}.prev-$(date +%Y%m%d%H%M%S); fi; "
            f"mkdir -p {d}/outputs; cd {d}; base64 -d | tar xzf -; "
            f"sbatch --parsable run.sbatch",
            stdin=_tar_b64(files),
            timeout=180,
        )
        job = out.strip().split(";")[0]
        if not job.isdigit():
            raise TargetError(f"sbatch returned {out.strip()!r}")
        return job

    def upload(self, run_id: int, files: dict[str, str], fresh: bool = True) -> None:
        """Put the run's files in its folder without submitting (smoke test first).

        `fresh` moves an existing folder aside, like submit; a re-upload after an AI
        smoke fix keeps the folder (its smoke logs) and overwrites the files.
        """
        d = self.job_dir(run_id)
        aside = (
            f"if [ -e {d}/run.sbatch ] || [ -e {d}/job.log ]; then "
            f"mv {d} {d}.prev-$(date +%Y%m%d%H%M%S); fi; "
            if fresh
            else ""
        )
        self.sh(
            f"set -e; {aside}mkdir -p {d}/outputs; cd {d}; base64 -d | tar xzf -",
            stdin=_tar_b64(files),
            timeout=180,
        )

    def sbatch_uploaded(self, run_id: int) -> str:
        """sbatch the run.sbatch already in the run folder. Returns the job id."""
        out = self.sh(
            f"cd {self.job_dir(run_id)} && sbatch --parsable run.sbatch", timeout=120
        )
        job = out.strip().split(";")[0]
        if not job.isdigit():
            raise TargetError(f"sbatch returned {out.strip()!r}")
        return job

    # ---- warm worker -------------------------------------------------------
    # One long-lived Slurm job ("lab-warm") runs Lab tasks from a spool folder so a
    # burst of smoke tests, planner probes and short runs shares one booted node.
    @property
    def warm(self) -> dict | None:
        """Warm worker settings, or None when disabled (`"warm": false`)."""
        cfg = self.cfg.get("warm", {})
        if cfg is False or (isinstance(cfg, dict) and cfg.get("enabled") is False):
            return None
        cfg = cfg if isinstance(cfg, dict) else {}
        part = str(cfg.get("partition") or "computehigh")
        if self.partitions and part not in self.partitions:
            part = self.default_partition
        return {
            "partition": part,
            "idle_min": int(cfg.get("idle_min", 20)),
            "hours": float(cfg.get("hours", 4)),
            "max_par": int(cfg.get("max_par", 2)),
            "max_full_min": int(cfg.get("max_full_min", 120)),
            "smoke_min": int(cfg.get("smoke_min", 15)),
            "max_workers": int(cfg.get("max_workers", 3)),
        }

    @property
    def warm_dir(self) -> str:
        # $HOME, not ~: the path is used inside --export= and quotes, where ~ stays literal
        root = self.remote_root
        if root.startswith("~"):
            root = "$HOME" + root[1:]
        return f"{root}/warm"

    def ensure_warm(self) -> str:
        """Start a warm worker unless a usable one is running or pending.

        Returns 'running:<job>', 'pending:<job>' or 'started:<job>'. The worker script
        is re-uploaded every time, so the cluster always runs this version's copy.
        """
        cfg = self.warm
        if not cfg:
            raise TargetError("the warm worker is disabled for this target")
        w = self.warm_dir
        limit = int(cfg["hours"] * 3600)
        tl = f"{limit // 3600:02d}:{(limit % 3600) // 60:02d}:00"
        max_w = max(1, int(cfg.get("max_workers", 3)))
        part = (
            cfg["partition"]
            if re.fullmatch(r"[\w.-]+", cfg["partition"])
            else "computehigh"
        )
        cmd = (
            f'set -e; W="{w}"; mkdir -p "$W/queue" "$W/running" "$W/done" "$W/workers"; '
            f'cat > "$W/worker.sh.new"; chmod 755 "$W/worker.sh.new"; '
            f'mv -f "$W/worker.sh.new" "$W/worker.sh"; '
            f"live=$(squeue -h -u $USER -n lab-warm -o '%i %T' 2>/dev/null || true); ok=''; "
            f'for j in $(echo "$live" | awk \'$2=="PENDING"||$2=="CONFIGURING"{{print $1}}\'); '
            f"do ok=pending:$j; done; "
            f'n=$(echo "$live" | awk \'$2=="PENDING"||$2=="CONFIGURING"\' | grep -c . || true); '
            f'for f in "$W"/workers/*.json; do [ -e "$f" ] || continue; j=$(basename "$f" .json); '
            f'if echo "$live" | grep -q "^$j RUNNING" && ! grep -q \'"draining": true\' "$f"; '
            f"then ok=running:$j; n=$((n+1)); fi; done; "
            # scale out with the backlog: one worker, plus one per 2 queued tasks, capped
            f'q=$(ls "$W/queue" 2>/dev/null | wc -l); want=$((1 + q / 2)); '
            f'[ "$want" -gt {max_w} ] && want={max_w}; '
            f'while [ "$n" -lt "$want" ]; do ok=started:$(sbatch --parsable --job-name=lab-warm '
            f"-p {part} -N 1 --exclusive -t {tl} --signal=B:USR1@60 "
            f'-o "$W/worker-%j.log" '
            f'--export=ALL,WARM="$W",IDLE_MIN={cfg["idle_min"]},MAX_PAR={cfg["max_par"]},'
            f'WARM_LIMIT_SEC={limit} "$W/worker.sh"); n=$((n+1)); done; echo "$ok"'
        )
        out = self.sh(cmd, stdin=_worker_script().encode(), timeout=120).strip()
        state = out.splitlines()[-1] if out else ""
        if not re.fullmatch(r"(running|pending|started):\d+(;\S+)?", state):
            raise TargetError(f"could not start the warm worker: {out[-300:]!r}")
        return state.split(";")[0]

    def warm_status(self) -> dict:
        """Workers (heartbeats joined with squeue) and the task counts."""
        w = self.warm_dir
        out = self.sh(
            f'W="{w}"; echo "::SQ::"; squeue -h -u $USER -n lab-warm -o "%i|%T|%M|%N|%L" '
            f'2>/dev/null; echo "::HB::"; cat "$W"/workers/*.json 2>/dev/null; '
            f'echo "::Q::"; ls "$W/queue" 2>/dev/null | wc -l; ls "$W/running" 2>/dev/null | wc -l',
            timeout=60,
        )
        sq = out.split("::SQ::", 1)[-1].split("::HB::", 1)[0]
        hb = out.split("::HB::", 1)[-1].split("::Q::", 1)[0]
        q = out.split("::Q::", 1)[-1].split()
        beats = {}
        for ln in hb.splitlines():
            try:
                d = json.loads(ln)
                beats[str(d.get("job"))] = d
            except ValueError:
                continue
        jobs = []
        for ln in sq.splitlines():
            parts = (ln.strip().split("|") + [""] * 5)[:5]
            if not parts[0]:
                continue
            b = beats.get(parts[0], {})
            jobs.append(
                {
                    "job": parts[0],
                    "state": parts[1],
                    "elapsed": parts[2],
                    "node": parts[3] or b.get("node", ""),
                    "left": parts[4],
                    "busy": [t for t in str(b.get("busy") or "").split() if t],
                    "draining": bool(b.get("draining")),
                }
            )
        return {
            "enabled": True,
            "partition": (self.warm or {}).get("partition"),
            "workers": jobs,
            "queued": int(q[0]) if q and q[0].isdigit() else 0,
            "running": int(q[1]) if len(q) > 1 and q[1].isdigit() else 0,
        }

    def warm_enqueue(
        self, task: str, files: dict[str, str], need_sec: int, exclusive: bool = False
    ) -> None:
        if not re.fullmatch(r"[\w.-]+", task):
            raise ValueError(f"bad task name {task!r}")
        w = self.warm_dir
        self.sh(
            f'set -e; W="{w}"; mkdir -p "$W/queue"; rm -rf "$W/queue/.{task}" "$W/queue/{task}" '
            f'"$W/done/{task}"; mkdir -p "$W/queue/.{task}"; cd "$W/queue/.{task}"; '
            f"base64 -d | tar xzf -; echo {int(need_sec)} > need_sec; "
            + ("touch exclusive; " if exclusive else "")
            + f'chmod 755 run.sh; mv "$W/queue/.{task}" "$W/queue/{task}"',
            stdin=_tar_b64(files),
            timeout=120,
        )

    def warm_task(self, task: str) -> dict:
        """Where a task is (queue/running/done/missing) with rc, times and node."""
        w = self.warm_dir
        out = self.sh(
            # done first: a task moves queue -> running -> done by rename, so checking in
            # that order can miss it mid-move and report it missing (run #77)
            f'W="{w}"; for d in done running queue done; do if [ -d "$W/$d/{task}" ]; then '
            f'echo "where=$d"; T="$W/$d/{task}"; echo "rc=$(cat $T/rc 2>/dev/null)"; '
            f'echo "started=$(cat $T/started 2>/dev/null)"; echo "finished=$(cat $T/finished 2>/dev/null)"; '
            f'echo "node=$(cat $T/node 2>/dev/null)"; echo "now=$(date +%s)"; break; fi; done',
            timeout=60,
        )
        info: dict[str, str] = {}
        for ln in out.splitlines():
            k, sep, v = ln.partition("=")
            if sep and k in ("where", "rc", "started", "finished", "node", "now"):
                info[k] = v.strip()
        return {
            "where": info.get("where", "missing"),
            "rc": int(info["rc"]) if info.get("rc", "").lstrip("-").isdigit() else None,
            "started": int(info["started"])
            if info.get("started", "").isdigit()
            else None,
            "finished": int(info["finished"])
            if info.get("finished", "").isdigit()
            else None,
            "node": info.get("node", ""),
            "now": int(info["now"]) if info.get("now", "").isdigit() else None,
        }

    def warm_task_log(self, task: str, limit: int = 60000) -> str:
        w = self.warm_dir
        return self.run(
            f'W="{w}"; for d in running done queue; do [ -f "$W/$d/{task}/log" ] && '
            f'{{ tail -c {int(limit)} "$W/$d/{task}/log"; break; }}; done',
            timeout=60,
        ).stdout.decode("utf-8", "replace")

    def warm_cancel(self, task: str) -> None:
        w = self.warm_dir
        self.sh(
            f'W="{w}"; if [ -d "$W/queue/{task}" ]; then mkdir -p "$W/done"; rm -rf "$W/done/{task}"; '
            f'mv "$W/queue/{task}" "$W/done/{task}" && echo 130 > "$W/done/{task}/rc"; '
            f'elif [ -d "$W/running/{task}" ]; then touch "$W/running/{task}/cancel"; fi; true',
            timeout=60,
        )

    def warm_stop(self) -> None:
        """Ask every warm worker to finish (running tasks are stopped)."""
        w = self.warm_dir
        self.sh(f'W="{w}"; mkdir -p "$W"; touch "$W/stop"; true', timeout=60)

    def read_file(self, run_id: int, rel: str, limit: int = 60000) -> str:
        """Tail of a text file in the run folder ('' when missing)."""
        if not re.fullmatch(r"[\w./-]+", rel) or ".." in rel:
            raise ValueError(f"bad path {rel!r}")
        return self.run(
            f"tail -c {int(limit)} {self.job_dir(run_id)}/{rel} 2>/dev/null", timeout=60
        ).stdout.decode("utf-8", "replace")

    def missing_outputs(self, run_id: int, sub: str, patterns: list[str]) -> list[str]:
        """Expected output patterns with no non-empty match under the run folder/sub."""
        pats = [p for p in patterns if re.fullmatch(r"[\w./*?-]+", p) and ".." not in p]
        if not pats:
            return []
        d = self.job_dir(run_id) + (f"/{sub}" if sub else "")
        out = self.run(
            f"cd {d} 2>/dev/null || exit 0; for p in {' '.join(shlex.quote(x) for x in pats)}; do "
            f'ok=""; for f in $p; do [ -s "$f" ] && ok=1 && break; done; [ -n "$ok" ] || echo "$p"; done',
            timeout=60,
        ).stdout.decode("utf-8", "replace")
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def status(self, run_id: int, job_id: str) -> dict:
        """One round trip: Slurm state, last stage marker, log tail and size."""
        if str(job_id).startswith("warm:"):
            return self._warm_run_status(run_id, str(job_id)[5:])
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
        if str(job_id).startswith("warm:"):
            return self.warm_cancel(str(job_id)[5:])
        self.sh(f"scancel {job_id}", timeout=60)

    def _warm_run_status(self, run_id: int, task: str) -> dict:
        """A full run on the warm node, reported in the same shape as Slurm status."""
        t = self.warm_task(task)
        stage = self.read_file(run_id, "stage.txt", 2000).strip().splitlines()
        rc = t["rc"]
        state = {"queue": "PENDING", "running": "RUNNING"}.get(t["where"], "")
        if t["where"] == "done":
            state = (
                "COMPLETED" if rc == 0
                else "TIMEOUT" if rc == 124
                else "CANCELLED" if rc in (130, 143)
                else "FAILED"
            )  # fmt: skip
        elif t["where"] == "missing":
            state = ""
        end = t["finished"] or t["now"]
        el = (end - t["started"]) if (t["started"] and end) else 0
        return {
            "slurm_state": state,
            "reason": "warm node" if state == "PENDING" else "",
            "elapsed": f"{el // 3600:d}:{(el % 3600) // 60:02d}:{el % 60:02d}"
            if el
            else "",
            "node": t["node"],
            "exit_code": f"{rc}:0" if rc is not None else "",
            "started": "",
            "stage": stage[-1] if stage else "",
            "log_size": 0,
            "node_fails": 0,
        }

    def fetch(self, run_id: int, dest: Path) -> list[dict]:
        """Copy outputs/ plus the log, plan and script back. Returns the file list."""
        import io
        import tarfile

        d = self.job_dir(run_id)
        listing = self.sh(
            f"cd {d} && find outputs job.log run.sbatch plan.json stage.txt smoke/job.log "
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
        cards = cat.get("usage_cards") or {}
        if cards:
            out.append(
                "Usage cards (verified facts for installed software; follow them):"
            )
            out += [f"- {k}: {v}" for k, v in sorted(cards.items())]
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
# pip packages whose compiled core crashes when layered on a Python environment module:
# OR-Tools bundles its own abseil/protobuf, and python-sci/2026.09 puts conda's
# libabsl_*.so (2605) on the same process; CP-SAT's Solve() segfaults (exit 139) on even a
# one-variable model once the module's site-packages are visible. Verified on Ursa Major
# 2026-09-29 (Lab run #38): ortools 9.12, 9.14 and 9.15 all crash on top of python-sci
# and all solve in a plain venv. These get an isolated venv (no --system-site-packages),
# with the module's Python and the plan's other pip packages installed alongside.
ISOLATE_FROM_MODULE_PIP = ("ortools",)
ISOLATED_BASE_PIP = ["numpy", "pandas", "matplotlib", "scipy"]
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
        # a pinned install of something the module has is deliberate when the module's
        # build lacks what the plan checks for (LAMMPS without GRANULAR, run #57)
        verify_txt = " ".join(
            str(v) for v in (plan.get("install") or {}).get("verify") or []
        )
        pinned = {
            re.split(r"[=<>!\[]", str(x))[0].lower()
            for x in (plan.get("install") or {}).get("conda") or []
            if re.search(r"[=<>]", str(x))
        }
        dup = sorted(
            {
                p
                for p in pkgs
                if p in have
                and p not in ("python", "pip", "numpy")
                and not (
                    p in pinned
                    and re.search(rf"\b{re.escape(p)}\b|lmp|grep", verify_txt)
                )
                and not (
                    p == "gmsh" and "import gmsh" in verify_txt
                )  # module has no Python API
            }
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
    warns += labguard.science_warnings(plan)
    warns += missing_import_warnings(plan)
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


# What each site Python module provides (from the catalog usage cards; used to catch a
# script importing a package nobody installs: run #76 imported pandas on python-ml).
MODULE_PYTHON_PACKAGES = {
    "python-sci": {
        "numpy",
        "scipy",
        "pandas",
        "matplotlib",
        "numba",
        "xarray",
        "astropy",
        "skyfield",
        "Bio",
        "networkx",
        "sklearn",
        "statsmodels",
    },
    # checked on a compute node 2026-09-29
    "python-ml": {
        "torch",
        "transformers",
        "sklearn",
        "numpy",
        "scipy",
        "pandas",
        "matplotlib",
        "networkx",
    },
}
_STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}


def script_imports(script: str) -> set[str]:
    """Top-level modules the script's Python imports (heredoc bodies and python -c)."""
    out: set[str] = set()
    for _, body in labguard._python_bodies(script):
        try:
            tree = ast.parse(body)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                out.add(node.module.split(".")[0])
    return {m for m in out if m not in _STDLIB}


def missing_import_warnings(plan: dict) -> list[str]:
    """Imports in the script that no module, pip or conda entry provides."""
    inst = plan.get("install") or {}
    mods = [str(m).split("/")[0] for m in inst.get("modules") or []]
    if inst.get("apptainer") and not any(m in MODULE_PYTHON_PACKAGES for m in mods):
        return []  # Python from a container may carry anything
    pyenv = [m for m in mods if m in MODULE_PYTHON_PACKAGES]
    if not pyenv and not (inst.get("pip") or inst.get("conda")):
        return []
    have: set[str] = set()
    for m in pyenv:
        have |= MODULE_PYTHON_PACKAGES[m]
    listed = [str(x) for x in (inst.get("pip") or []) + (inst.get("conda") or [])]
    have |= set(import_names(listed)) | {_pkg_name(x) for x in listed}
    have |= {_pkg_name(x).replace("-", "_") for x in listed}
    have |= {IMPORT_NAMES.get(_pkg_name(x), _pkg_name(x)) for x in listed}
    if inst.get("conda"):
        have |= {"numpy"}  # every conda Python science package pulls numpy
    if inst.get("conda") or any(
        not m.startswith(("python-sci", "python-ml")) for m in mods
    ):
        # conda envs and other modules bring packages we can't enumerate; only flag
        # imports that are clearly the common scientific stack
        common = {
            "numpy",
            "scipy",
            "pandas",
            "matplotlib",
            "numba",
            "networkx",
            "sklearn",
        }
        need = script_imports(str(plan.get("script") or "")) & common
    else:
        need = script_imports(str(plan.get("script") or ""))
    missing = sorted(m for m in need - have if not m.startswith("_"))
    if not missing:
        return []
    where = f" ({', '.join(pyenv)} doesn't have them)" if pyenv else ""
    return [
        f"The script imports {', '.join(missing)} but nothing installs them{where}; "
        "add them to install.pip (or install.conda)."
    ]


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


# ---------------------------------------------------------------- failure classes
# What kind of failure a log shows decides who can fix it. The AI can fix the plan's
# script; it can't fix a cluster, a registry or its own guess about a tool's internals.
FAILURE_CLASSES = [
    # (class, regex on the log, message shown to the person / given to the AI)
    (
        "install",
        r"\[ERROR\] no install method produced a working environment",
        "No install method produced a working environment (see the [LADDER] lines). "
        "This is the software setup, not the script: change install (another package "
        "source, a module, a container), not the analysis code.",
    ),
    (
        "container",
        r"FATAL:\s+While (?:making image|pulling)|unable to parse image name",
        "A container image could not be pulled or found.",
    ),
    (
        "tool-crash",
        r"Assertion '.*' failed|Segmentation fault|core dumped|signal 11|"
        r"\*\*\* Process received signal",
        "An external program crashed inside itself (assertion or segfault). The usual "
        "cause is an input file it did not expect (name or format): list the files it "
        "wrote (templates, example_* files) and match them exactly.",
    ),
    (
        "glibc",
        r"version `GLIBC_2\.\d+' not found",
        "A binary needs a newer glibc than the compute nodes have (Rocky 8, glibc 2.28). "
        "Pin an older build of that package (conda-forge keeps old versions), or use the "
        "module or a container.",
    ),
    (
        "missing-feature",
        r"lacks? (?:the )?\w+ (?:support|package)|Unrecognized (?:pair|fix|atom) style|"
        r"Package \w+ is not installed|not compiled with",
        "The installed build lacks a feature the plan needs; use a build that has it "
        "(another module variant, conda-forge, or a container).",
    ),
    (
        "numerical",
        r"ZeroDivisionError|FloatingPointError|nan detected|diverg|"
        r"RuntimeWarning: (?:overflow|invalid value)",
        "The computation blew up numerically (division by zero, NaN, divergence). Check "
        "stability limits (time step, relaxation time, CFL) and guard divisions; do not "
        "hide it with try/except.",
    ),
    (
        "timeout",
        r"DUE TO TIME LIMIT|CANCELLED AT .* DUE to TIME|exit 124\b",
        "The job ran out of time.",
    ),
    (
        "oom",
        r"oom-kill|Out Of Memory|MemoryError|Killed\s*$",
        "The job ran out of memory.",
    ),
]


def classify_failure(log: str) -> tuple[str, str]:
    """(class, advice) for a failed run's log; ('script', '') when nothing specific."""
    tail = log[-40000:]
    for name, rx, msg in FAILURE_CLASSES:
        if re.search(rx, tail, re.M):
            return name, msg
    return "script", ""


# ------------------------------------------------------------- AI-fix review gate
_REF_FALLBACK = re.compile(
    r"return\s+\(?\s*(-?\d+\.\d+)\s*,\s*0(?:\.0)?\s*\)?|"  # return 1.27, 0.0 on failure
    r"(?:except[^\n]*:\s*\n\s*)\w+\s*=\s*(-?\d+\.\d+)\s*$",
    re.M,
)


def _ref_numbers(script: str) -> set[str]:
    """Numbers the script treats as reference values (expected/REF names, verdict)."""
    out = set()
    for m in re.finditer(
        r"(?:REF|ref|expected|EXPECTED|reference|benchmark)\w*\s*[=:]\s*(-?\d+\.\d+)",
        script,
    ):
        out.add(m.group(1))
    for m in re.finditer(r"['\"]expected['\"]\s*:\s*(-?\d+\.\d+)", script):
        out.add(m.group(1))
    return out


def _imports(script: str) -> set[str]:
    return set(re.findall(r"^\s*(?:import|from)\s+([A-Za-z_]\w*)", script, re.M))


def fix_concerns(old: dict, new: dict) -> list[str]:
    """Things an AI fix did that a person should look at before it runs.

    Deterministic checks on the diff (runs #56, #59, #64): a fallback that returns a
    reference value (a failed fit would then look like agreement), a verdict tolerance or
    expected value that changed, a library removed, a large rewrite, new fixed-text
    findings.
    """
    a = str((old or {}).get("script") or "")
    b = str((new or {}).get("script") or "")
    out: list[str] = []
    refs = _ref_numbers(a) | _ref_numbers(b)
    added = [ln for ln in b.splitlines() if ln not in set(a.splitlines())]
    add_txt = "\n".join(added)
    for m in _REF_FALLBACK.finditer(add_txt):
        val = m.group(1) or m.group(2)
        if val in refs:
            out.append(
                f"a new fallback returns the reference value {val}, so a failed "
                "computation would look like agreement"
            )
    for key in ("tolerance", "expected"):
        rx = rf"['\"]{key}['\"]\s*:\s*([^,}}\n]+)"
        ra = {x.strip() for x in re.findall(rx, a)}
        rb = {x.strip() for x in re.findall(rx, b)}
        if ra and rb and ra != rb:
            out.append(
                f"the verdict's {key} values changed "
                f"(removed {', '.join(sorted(ra - rb)[:3]) or '-'}; "
                f"added {', '.join(sorted(rb - ra)[:3]) or '-'})"
            )
    gone = _imports(a) - _imports(b)
    gone -= {"os", "sys", "re", "json", "time", "math"}
    if gone:
        out.append(f"removes library import(s): {', '.join(sorted(gone))}")
    la, lb = a.splitlines(), b.splitlines()
    if len(la) >= 10:
        import difflib

        ratio = difflib.SequenceMatcher(None, la, lb, autojunk=False).ratio()
        if ratio < 0.7:
            out.append(
                f"rewrites much of the script ({int((1 - ratio) * 100)}% changed)"
            )
    # a changed line that only swaps identifiers inside a formula (k3_v -> k3_u in an
    # RK4 update, run #59): usually a typo the AI introduced, and it changes the maths
    import difflib as _dl

    for op, i1, i2, j1, j2 in _dl.SequenceMatcher(
        None, la, lb, autojunk=False
    ).get_opcodes():
        if op != "replace" or (i2 - i1) != (j2 - j1):
            continue
        for x, y in zip(la[i1:i2], lb[j1:j2]):
            # arithmetic assignment lines only (not strings, titles, shell)
            if not re.match(
                r"\s*[A-Za-z_][\w.\[\]]*\s*[-+*/]?=\s*[^=]", x
            ) or re.search(r"['\"]", x):
                continue
            tx = re.findall(r"[A-Za-z_]\w*|\S", x)
            ty = re.findall(r"[A-Za-z_]\w*|\S", y)
            if len(tx) != len(ty) or not re.search(r"[-+*/]", x.split("=", 1)[-1]):
                continue
            diffs = [(p, q) for p, q in zip(tx, ty) if p != q]
            if 0 < len(diffs) <= 2 and all(
                re.fullmatch(r"[A-Za-z_]\w*", p) and re.fullmatch(r"[A-Za-z_]\w*", q)
                for p, q in diffs
            ):
                sw = ", ".join(f"{p} -> {q}" for p, q in diffs)
                out.append(
                    f"changes a variable inside a formula ({sw}): {y.strip()[:90]}"
                )
    new_hard = set(labguard.hardcoded_findings(b)) - set(labguard.hardcoded_findings(a))
    if new_hard:
        out.append("adds fixed-text results: " + "; ".join(sorted(new_hard)[:2]))
    return out[:6]


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

{lessons}

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

{lessons}

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

{lessons}
{probes}
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
  Spack only for compiled HPC codes (install.spack; the harness builds them in a user
  Spack chained to the site's). The harness verifies each install (imports of listed Python
  packages plus install.verify) and falls back automatically: layered venv -> isolated venv ->
  conda-forge, or Pixi -> relaxed Pixi -> pip; a module that does not load is replaced by its
  conda package. So list what the job needs plainly; do not write install code in the script.
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
"install": {{"modules": [], "conda": ["package", ...], "channels": ["conda-forge"], "pip": ["package", "--extra-index-url https://...", ...], "apptainer": ["docker://image:tag"], "spack": ["only for compiled codes that are neither modules nor on conda-forge"], "verify": ["one-line shell checks that prove the software works, e.g. \"SU2_CFD --help | head -1\" or \"python -c 'import rdkit'\""]}},
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
KNOWN-ANSWER CHECKS (outputs/verdict.json): {verdict}

OUTPUT FILES:
{files}

TEXT OUTPUTS (truncated):
{texts}

LOG TAIL:
{log}

Rules: report only what the outputs and log show. Quote the key numbers exactly. If a
known-answer check failed, say so in the first sentence of **Result** and do not present the
other numbers as findings. Take
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
export LAB_SMOKE="${{LAB_SMOKE:-0}}"  # 1 = cut-down smoke test (see plan)
[ "$LAB_SMOKE" = 1 ] && echo "[INFO] SMOKE TEST: cut-down run"
{site_header}{exports}

stage "Installing software"
"""
    if mods:
        body += "module purge >/dev/null 2>&1 || true\n"
        body += 'LADDER_MOD_FALLBACK=""\n'
        body += "".join(
            f'module load {m} || {{ echo "[LADDER] module {m} did not load; will try '
            f'{m.split("/")[0]} from conda-forge/bioconda"; '
            f'LADDER_MOD_FALLBACK="$LADDER_MOD_FALLBACK {m.split("/")[0]}"; }}\n'
            for m in mods
        )
    body += install_ladder(mods, conda, chans, pips, inst)
    if imgs:
        body += """command -v apptainer >/dev/null 2>&1 || module load apptainer 2>/dev/null || true
if ! command -v apptainer >/dev/null 2>&1; then
  echo "[ERROR] apptainer is not installed on $(hostname); this node cannot run container images."
  exit 3
fi
"""
    for img in imgs:
        var = "IMG_" + _slug(
            img.rsplit("/", 1)[-1].split(":")[0].removesuffix(".sif"), 30
        ).upper().replace("-", "_")
        if img.startswith("/") or img.endswith(".sif"):
            # an image already on the cluster (e.g. /apps/containers/*.sif): use it in
            # place; `apptainer pull` would treat the path as a registry name (run #61)
            body += f"""if [ ! -r {shlex.quote(img)} ]; then
  echo "[ERROR] container image {img} is not on this node"; exit 3
fi
export {var}={shlex.quote(img)}
echo "[INFO] container {img} (on the cluster, used in place)"
"""
            continue
        name = _slug(img, 60)
        body += f"""mkdir -p $HOME/deep-research-lab/images
[ -f $HOME/deep-research-lab/images/{name}.sif ] || apptainer pull $HOME/deep-research-lab/images/{name}.sif {shlex.quote(img)}
export {var}=$HOME/deep-research-lab/images/{name}.sif
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


# ----------------------------------------------------------------------- cluster matching
def workload_shape(plan: dict) -> str:
    """gpu | mpi | sweep | bigmem | cpu, from what the plan says it needs."""
    r = plan.get("resources") or {}
    text = (
        " ".join(
            str(plan.get(k) or "") for k in ("approach", "title", "question")
        ).lower()
        + " "
        + str(plan.get("script") or "")[:20000].lower()
    )
    if _int(r.get("gpus"), 0) > 0 or re.search(
        r"\bcuda\b|--nv\b|\.to\(['\"]cuda|torch\.cuda", text
    ):
        return "gpu"
    if _int(r.get("nodes"), 1) > 1 or re.search(r"\b(srun|mpirun|mpiexec)\b", text):
        return "mpi"
    mem = re.search(r"(\d{3,4})\s*gb\b", text)
    if mem and int(mem.group(1)) > 100:
        return "bigmem"
    if re.search(
        r"parameter sweep|monte carlo|replicates|xargs -p|job array|sbatch --array",
        text,
    ):
        return "sweep"
    return "cpu"


def suggest_partition(
    target, plan: dict, stocked_out: set[str] | None = None
) -> tuple[str, str]:
    """(partition, reason) that fits the plan's shape and is not stocked out."""
    parts = getattr(target, "partitions", {}) or {}
    want = (plan.get("resources") or {}).get("partition") or target.default_partition
    out = set(stocked_out or ())
    shape = workload_shape(plan)
    by_shape = {
        "gpu": ["gpul4"],
        "mpi": ["computehigh", "standard"],
        "bigmem": ["highmem"],
        "sweep": ["computehigh", "spot", "standard"],
        "cpu": ["computehigh", "standard"],
    }[shape]
    ok = [p for p in by_shape if p in parts and p not in out]
    if want in parts and want not in out:
        if (shape == "gpu") == bool(parts[want].get("gpus")):
            return want, ""
    if ok:
        why = (
            f"'{want}' is stocked out in the zone"
            if want in out
            else f"a {shape} job fits '{ok[0]}' better than '{want}'"
        )
        return ok[0], why
    return want, ""


def time_from_history(db_path: str, plan: dict, floor_min: int = 10) -> str | None:
    """A time limit from similar completed runs (same software), or None.

    3x the longest similar run plus 5 minutes for installs, rounded up to 5 minutes,
    never below `floor_min`. Similar = shares a software/package name with the plan.
    """
    keys = set(labguard.software_keys(plan))
    if not keys:
        return None
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            rows = conn.execute(
                "SELECT plan, elapsed FROM lab_runs WHERE status='completed' AND elapsed IS NOT NULL "
                "ORDER BY id DESC LIMIT 300"
            ).fetchall()
    except sqlite3.Error:
        return None
    worst = 0
    n = 0
    for pj, el in rows:
        try:
            other = json.loads(pj or "{}")
        except ValueError:
            continue
        if not keys & set(labguard.software_keys(other)):
            continue
        el = str(el)
        sec = int(_hours(el) * 3600) if TIME_RE.fullmatch(el) else 0
        if sec:
            worst, n = max(worst, sec), n + 1
    if not n:
        return None
    mins = max(floor_min, -(-(worst * 3 + 300) // 300) * 5)
    return f"{mins // 60:02d}:{mins % 60:02d}:00"


def stocked_out_partitions(target) -> set[str]:
    """Partitions whose nodes failed to start for lack of GCP capacity (sinfo -R)."""
    try:
        out = target.sh(
            "sinfo -h -R -o '%E|%N' 2>/dev/null | grep -iE 'RESOURCE_POOL_EXHAUSTED|stockout|"
            "ZONE_RESOURCE|insufficient capacity' | cut -d'|' -f2; "
            "echo '::P::'; sinfo -h -o '%R|%N' 2>/dev/null",
            timeout=40,
        )
    except Exception:
        return set()
    bad_nodes, _, parts = out.partition("::P::")
    prefixes = {
        re.sub(r"[\[\d].*$", "", n.strip()) for n in bad_nodes.split() if n.strip()
    }
    res = set()
    for ln in parts.splitlines():
        p, _, nodes = ln.partition("|")
        if any(nodes.strip().startswith(px) for px in prefixes if px):
            res.add(p.strip())
    return res


# ----------------------------------------------------------------------- planner probes
PROBE_PROMPT = """You are about to plan a computational job on the HPC cluster below. Before
writing the plan, you may check facts on a compute node: installed module details, program
help text and versions, Python package versions and function signatures, and whether
download URLs work. List the checks that would most reduce the risk of the job failing
(wrong flags, removed APIs, wrong output file names, dead links). At most {max_checks}.

{target}

SOURCE REPORT: {title}
MATERIAL (excerpt):
{text}
{question_line}
Check kinds (use exactly these):
- {{"kind": "module", "name": "su2/8.2.0", "load": ["openmpi"]}}  -> `module show` and the programs it adds
- {{"kind": "help", "cmd": "SU2_CFD --help", "load": ["openmpi", "su2/8.2.0"]}}  -> first 150 lines of output
- {{"kind": "pyversion", "package": "ortools"}}  -> versions available on PyPI and the one in python-sci
- {{"kind": "pyhelp", "target": "ortools.sat.python.cp_model.CpModel.NewFixedSizeIntervalVar", "pip": ["ortools"]}}  -> signature and docstring
- {{"kind": "url", "url": "https://..."}}  -> HTTP status and size
- {{"kind": "features", "cmd": "lmp", "load": ["openmpi", "lammps"]}}  -> the program's full `-h` output
  (compiled-in packages, styles, solvers): use it when the plan relies on an optional package
  (LAMMPS GRANULAR, a GROMACS GPU build, an SU2 option)
- {{"kind": "conda", "package": "lammps"}}  -> versions on conda-forge/bioconda

Return JSON only, in a ```json block: {{"checks": [ ... ]}}"""

PROBE_KINDS = ("module", "help", "pyversion", "pyhelp", "url", "features", "conda")
_SAFE_TOKEN = re.compile(r"[\w.+/:=@-]+")


def _probe_cmd(chk: dict) -> str | None:
    """Shell for one probe, or None when it is unsafe or malformed.

    Probes are read-only: module show/avail, `<program> --help|-h|--version|-version`,
    pip index/download metadata into a throwaway venv, Python `help()` on a dotted name,
    and `curl -sI` on http(s) URLs. Anything else is refused.
    """
    kind = chk.get("kind")
    loads = [str(m) for m in chk.get("load") or [] if _SAFE_TOKEN.fullmatch(str(m))][:4]
    pre = "module purge >/dev/null 2>&1; " + "".join(
        f"module load {shlex.quote(m)} >/dev/null 2>&1; " for m in loads
    )
    if kind == "module":
        name = str(chk.get("name") or "")
        if not _SAFE_TOKEN.fullmatch(name):
            return None
        q = shlex.quote(name)
        return (
            pre + f"module show {q} 2>&1 | head -60; "
            f'for d in $(module show {q} 2>&1 | sed -n \'s/.*prepend_path("PATH","\\([^"]*\\)").*/\\1/p\'); '
            'do echo "programs in $d:"; ls "$d" 2>/dev/null | head -40; done'
        )
    if kind == "help":
        parts = str(chk.get("cmd") or "").split()
        if (
            not parts
            or len(parts) > 3
            or not all(_SAFE_TOKEN.fullmatch(p) for p in parts)
        ):
            return None
        flags = parts[1:]
        if any(
            f not in ("--help", "-h", "-help", "--version", "-version", "-V", "help")
            for f in flags
        ):
            return None
        return (
            pre
            + "timeout 30 "
            + " ".join(shlex.quote(p) for p in parts)
            + " 2>&1 < /dev/null | head -150"
        )
    if kind == "features":
        prog = str(chk.get("cmd") or "").split()
        if len(prog) != 1 or not re.fullmatch(r"[\w.+-]+", prog[0]):
            return None
        q = shlex.quote(prog[0])
        # the whole help text, with package/feature sections first
        return (
            pre
            + f"H=$(timeout 30 {q} -h 2>&1 < /dev/null || timeout 30 {q} --help 2>&1 < /dev/null); "
            + 'echo "$H" | grep -i -A12 -E "installed packages|compiled|features|build" | head -60; '
            + 'echo "--- full help (head)"; echo "$H" | head -120'
        )
    if kind == "conda":
        pkg = str(chk.get("package") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", pkg):
            return None
        return (
            "export PATH=/apps/pixi/bin:$PATH; "
            f"timeout 90 pixi search -c conda-forge -c bioconda {shlex.quote(pkg)} 2>&1 | head -25"
        )
    if kind == "pyversion":
        pkg = str(chk.get("package") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", pkg):
            return None
        return (
            "module load python-sci >/dev/null 2>&1; "
            f'echo "python-sci: $(python3 -c \'import importlib.metadata as m; print(m.version(\\"{pkg}\\"))\' 2>/dev/null || echo not installed)"; '
            f"timeout 60 python3 -m pip index versions {shlex.quote(pkg)} 2>/dev/null | head -3"
        )
    if kind == "pyhelp":
        target = str(chk.get("target") or "")
        pips = [
            str(p)
            for p in chk.get("pip") or []
            if re.fullmatch(r"[A-Za-z0-9_.=<>!-]+", str(p))
        ][:3]
        if not re.fullmatch(r"[A-Za-z_][\w.]*", target):
            return None
        mod = target.split(".")[0]
        inst = (
            'V="$TMPDIR/probe-venv"; python3 -m venv --system-site-packages "$V" >/dev/null && '
            f'"$V/bin/python" -m pip install -q {" ".join(shlex.quote(p) for p in pips)} >/dev/null 2>&1; PY="$V/bin/python"; '
            if pips
            else "PY=python3; "
        )
        code = (
            "import importlib, inspect, pydoc, sys\n"
            f"t = {target!r}\n"
            "parts = t.split('.')\n"
            "obj = None\n"
            "for i in range(len(parts), 0, -1):\n"
            "    try:\n"
            "        obj = importlib.import_module('.'.join(parts[:i]))\n"
            "        for a in parts[i:]:\n"
            "            obj = getattr(obj, a)\n"
            "        break\n"
            "    except (ImportError, AttributeError) as e:\n"
            "        err = e\n"
            "if obj is None:\n"
            "    print('NOT FOUND:', err); sys.exit(0)\n"
            "try:\n"
            "    print('signature:', inspect.signature(obj))\n"
            "except (TypeError, ValueError):\n"
            "    pass\n"
            "print(pydoc.render_doc(obj, renderer=pydoc.plaintext)[:4000])\n"
            f"m = importlib.import_module({mod!r}); print('version:', getattr(m, '__version__', '?'))\n"
        )
        return (
            "module load python-sci >/dev/null 2>&1; "
            + inst
            + f"timeout 60 $PY - <<'PYEOF'\n{code}PYEOF"
        )
    if kind == "url":
        url = str(chk.get("url") or "")
        if not re.fullmatch(r"https?://[\w.:/%?=&~+@,-]+", url) or len(url) > 400:
            return None
        return (
            f"curl -sSIL -m 20 -o /dev/null -w 'HTTP %{{http_code}}, %{{size_download}} bytes\\n' {shlex.quote(url)} 2>&1 | head -c 300; "
            f"curl -sSL -m 20 -r 0-2047 {shlex.quote(url)} 2>/dev/null | head -c 400 | tr -c '[:print:]\\n' '.'"
        )
    return None


def probe_script(checks: list[dict]) -> tuple[str, list[dict]]:
    """A bash script running each accepted probe with a header line; (script, accepted)."""
    ok = []
    lines = [
        "#!/bin/bash",
        "set +e",
        "[ -r /apps/docs/templates/job-header.sh ] && . /apps/docs/templates/job-header.sh >/dev/null 2>&1",
    ]
    for i, c in enumerate(checks):
        if not isinstance(c, dict) or c.get("kind") not in PROBE_KINDS:
            continue
        cmd = _probe_cmd(c)
        if not cmd:
            continue
        ok.append(c)
        lines.append(
            f"echo '=== CHECK {len(ok)}: {json.dumps(c)[:200].replace(chr(39), '')}'"
        )
        lines.append(f"( {cmd} ) 2>&1 | head -c 6000")
    return "\n".join(lines) + "\n", ok


_URL_IN_SCRIPT = re.compile(r"https?://[^\s'\"<>)\\]+")


def script_urls(plan: dict) -> list[str]:
    """Download URLs in a plan's script and inputs (not docs links), deduplicated."""
    text = (
        str(plan.get("script") or "")
        + "\n"
        + "\n".join(str(x) for x in plan.get("inputs") or [])
    )
    urls = []
    for u in _URL_IN_SCRIPT.findall(text):
        u = u.rstrip(".,;:")
        if "$" in u or "{" in u or u in urls:
            continue
        urls.append(u)
    return urls[:15]


# ----------------------------------------------------------------------- install ladder
# Import names that differ from the package name (for the automatic import check).
IMPORT_NAMES = {
    "scikit-learn": "sklearn",
    "scikit-image": "skimage",
    "opencv-python": "cv2",
    "opencv-python-headless": "cv2",
    "pillow": "PIL",
    "pyyaml": "yaml",
    "biopython": "Bio",
    "beautifulsoup4": "bs4",
    "python-dateutil": "dateutil",
    "batman-package": "batman",
    "pytorch": "torch",
    "tensorflow-cpu": "tensorflow",
    "ortools": "ortools",
    "netcdf4": "netCDF4",
    "pytables": "tables",
    "tables": "tables",
    "py3dmol": "py3Dmol",
    "rdkit-pypi": "rdkit",
    "mdanalysis": "MDAnalysis",
    "pymatgen": "pymatgen",
    "ase": "ase",
    "openmm": "openmm",
    "pyscf": "pyscf",
    "networkx": "networkx",
    "simpy": "simpy",
    "pythermalcomfort": "pythermalcomfort",
    "astropy": "astropy",
    "skyfield": "skyfield",
    "treetime": "treetime",
    "phylo-treetime": "treetime",
    "nextstrain-augur": "augur",
    "augur": "augur",
    "jax": "jax",
    "jaxlib": "jaxlib",
    "numba": "numba",
    "sympy": "sympy",
    "scikit-rf": "skrf",
}
# conda-only tools with no Python import: never import-checked
NO_IMPORT = {
    "python",
    "pip",
    "gcc",
    "gxx",
    "gfortran",
    "make",
    "cmake",
    "openmpi",
    "mpich",
    "iqtree",
    "mafft",
    "raxml-ng",
    "blast",
    "samtools",
    "bwa",
    "gromacs",
    "lammps",
    "cp2k",
    "quantum-espresso",
    "nodejs",
    "r-base",
    "julia",
    "fftw",
    "hdf5",
    "compilers",
    "cxx-compiler",
    "c-compiler",
    "fortran-compiler",
    "ffmpeg",
}


def _pkg_name(spec: str) -> str:
    return re.split(r"[<>=!~\[ ;]", spec.strip(), maxsplit=1)[0].lower()


# import name -> PyPI/conda package, for modules found in verify commands
IMPORT_TO_PKG = {v: k for k, v in IMPORT_NAMES.items()}
_VERIFY_IMPORT = re.compile(r"\bimport\s+([\w.]+(?:\s*,\s*[\w.]+)*)")
_VERIFY_FROM = re.compile(r"\bfrom\s+([\w.]+)\s+import\b")


def verify_imports(verify: list[str]) -> list[str]:
    """Top-level third-party modules imported by the plan's verify commands."""
    std = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    out: list[str] = []
    for v in verify:
        if "python" not in v:
            continue
        froms = _VERIFY_FROM.findall(v)
        rest = re.sub(r"\bfrom\s+[\w.]+\s+import\s+[\w.]+(?:\s*,\s*[\w.]+)*", " ", v)
        mods = [m for grp in _VERIFY_IMPORT.findall(rest) for m in grp.split(",")]
        mods += froms
        for m in mods:
            top = m.strip().split(".")[0]
            if (
                top
                and top not in std
                and top not in out
                and re.fullmatch(r"[A-Za-z_]\w*", top)
            ):
                out.append(top)
    return out


def import_names(pkgs: list[str]) -> list[str]:
    """Python import names to verify an environment with (best effort)."""
    out = []
    for p in pkgs:
        if p.startswith(("--", "https:")):
            continue
        n = _pkg_name(p)
        if not n or n in NO_IMPORT or n.startswith(("r-", "lib")):
            continue
        mod = IMPORT_NAMES.get(n, n.replace("-", "_"))
        if re.fullmatch(r"[A-Za-z_][\w.]*", mod):
            out.append(mod)
    return sorted(set(out))


_LADDER_FUNCS = r"""
# ---- install ladder: try each way to get the software, keep the first that verifies
LADDER_LOG="$SLURM_SUBMIT_DIR/outputs/environment.json"
ladder_verify() {  # python-bin: import checks and the plan's verify commands
  local py=$1 rc=0
  if [ -n "$LADDER_IMPORTS" ]; then
    "$py" - "$LADDER_IMPORTS" <<'PYEOF' || rc=1
import importlib, sys
bad = []
for m in sys.argv[1].split():
    try:
        importlib.import_module(m)
    except Exception as e:  # noqa: BLE001
        bad.append(f"{m}: {type(e).__name__}: {e}"[:300])
if bad:
    print("[LADDER] import check failed: " + "; ".join(bad))
    sys.exit(1)
print("[LADDER] imports OK: " + sys.argv[1])
PYEOF
  fi
  if [ $rc = 0 ] && [ -n "${LADDER_VERIFY:-}" ]; then
    # every line must succeed (eval of a multi-line string only reports the last one:
    # `lmp -h | grep -q GRANULAR` failed and was ignored, run #75)
    local line
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      ( set +e; set -o pipefail; eval "$line" ) >> "$TMPDIR/ladder_verify.log" 2>&1 || {
        echo "[LADDER] verify failed: $line"; tail -20 "$TMPDIR/ladder_verify.log"; rc=1; break; }
    done <<< "$LADDER_VERIFY"
  fi
  return $rc
}
ladder_record() {  # rung envdir
  local py; py=$(command -v python3 || command -v python || true)
  local lock=""
  if [ -n "$py" ]; then lock=$("$py" -m pip freeze 2>/dev/null | head -400 | tr '\n' ';' | sed 's/"/\\"/g'); fi
  printf '{"rung": "%s", "env": "%s", "tried": "%s", "python": "%s", "packages": "%s"}\n' \
    "$1" "$2" "${LADDER_TRIED# }" "$($py -V 2>&1 | tr -d '\n')" "$lock" > "$LADDER_LOG"
  mkdir -p "$HOME/deep-research-lab/envs"
  printf '{"key": "%s", "rung": "%s", "run": "%s", "when": "%s"}\n' "$LADDER_KEY" "$1" \
    "$(basename "$SLURM_SUBMIT_DIR")" "$(date -u +%FT%TZ)" >> "$HOME/deep-research-lab/envs/ladder.jsonl"
  echo "[LADDER] using rung $1 ($2)"
}
ladder_try() {  # name envdir build-commands...: build once (cached), activate, verify
  local name=$1 dir=$2; shift 2
  LADDER_TRIED="$LADDER_TRIED $name"
  echo "[LADDER] trying $name"
  mkdir -p "$(dirname "$dir")"
  exec 9>"$dir.lock"; flock 9
  if [ -f "$dir/.bad" ]; then
    echo "[LADDER] $name failed before for this package list; skipping"; flock -u 9; return 1
  fi
  if [ ! -f "$dir/.ready" ]; then
    rm -rf "$dir"
    if ! ( set -e; "$@" ) ; then
      echo "[LADDER] $name: install failed"; mkdir -p "$dir"; touch "$dir/.bad"; flock -u 9; return 1
    fi
    touch "$dir/.ready"
  else
    echo "[INFO] reusing cached environment $dir"
  fi
  flock -u 9
  return 0
}
"""


def install_ladder(
    mods: list[str], conda: list[str], chans: list[str], pips: list[str], inst: dict
) -> str:
    """Shell that installs the plan's software, falling back rung by rung.

    Python work: (1) venv layered on the Python module; (2) isolated venv on the
    module's Python (native wheels that clash with the module, e.g. OR-Tools); (3) a
    Pixi environment with everything from conda-forge (pip for what conda lacks).
    Conda/Pixi plans: (1) Pixi as listed; (2) Pixi with conda-forge + bioconda and the
    Python pins dropped; (3) pip into a plain venv. Every rung is verified (imports of
    the listed packages plus the plan's install.verify commands) before it is used; a
    rung that fails is cached as bad for that package list. A module that does not load
    is replaced by its conda package. The rung used goes to outputs/environment.json and
    ~/deep-research-lab/envs/ladder.jsonl (read by later plans).
    """
    spack = [
        str(x)
        for x in inst.get("spack") or []
        if re.fullmatch(r"[\w.@%+~=^-]+", str(x))
    ][:4]
    spack_sh = _spack_block(spack) if spack else ""
    pymods = [m for m in mods if m.split("/")[0] in PYTHON_ENV_MODULES]
    names = [x for x in pips if not x.startswith(("--", "https:"))]
    verify = [
        str(v)
        for v in (inst.get("verify") or [])
        if isinstance(v, str) and "\n" not in v
    ][:10]
    imports = import_names(names + conda)
    # packages the verify commands import (python -c 'import numba, scipy.sparse'): the
    # isolated/conda rungs must install them too, since they don't have the module's set
    vimports = verify_imports(verify)
    vpk = [IMPORT_TO_PKG.get(m, m) for m in vimports]
    # the MPI/module fallbacks can add conda packages at run time
    if not (conda or pips or verify):
        return spack_sh + (
            'if [ -n "${LADDER_MOD_FALLBACK:-}" ]; then\n'
            + _pixi_fallback_only(chans)
            + "fi\n"
        )
    # verify is part of the key: a rung marked bad for one plan's checks must not be
    # skipped for a plan with different checks
    key = hashlib.sha256(
        json.dumps([mods, sorted(conda), sorted(chans), pips, verify]).encode()
    ).hexdigest()[:12]
    base = f"$HOME/deep-research-lab/envs/{_slug('-'.join(sorted(conda + names)) or 'env', 40)}-{key}"
    out = _LADDER_FUNCS
    out += f'LADDER_KEY={shlex.quote(key)}\nLADDER_TRIED=""\n'
    out += f"LADDER_IMPORTS={shlex.quote(' '.join(imports))}\n"
    out += "LADDER_VERIFY=" + shlex.quote("\n".join(verify)) + "\n"
    out += "export PATH=/apps/pixi/bin:$HOME/.pixi/bin:$PATH\n"
    out += "export PIXI_CACHE_DIR=${PIXI_CACHE_DIR:-$HOME/.cache/rattler}\n"
    q = " ".join(shlex.quote(p) for p in pips)
    isolate_first = any(_pkg_name(n) in ISOLATE_FROM_MODULE_PIP for n in names)
    have = [_pkg_name(n) for n in names]
    extra = " ".join(
        shlex.quote(p) for p in dict.fromkeys(ISOLATED_BASE_PIP + vpk) if p not in have
    )
    rungs: list[tuple[str, str, str]] = []  # name, envdir, build commands (bash)
    out += 'LADDER_OK=""\n'
    if pymods and not conda and not pips:
        # nothing to add: the module itself is rung 1 (no venv, nothing to build)
        out += (
            '( ladder_verify python3 ) && { LADDER_OK=module; LADDER_TRIED=" module"; '
            f"ladder_record module {shlex.quote(pymods[0])}; }} "
            '|| { LADDER_TRIED=" module"; echo "[LADDER] module did not verify"; }\n'
        )
    if pymods and not conda and pips:
        # --system-site-packages is not enough when the module is itself a venv
        # (python-ml: its packages live in its own site-packages, not the base Python's,
        # so a layered venv saw none of them, run #76). A .pth file pointing at the
        # module's site-packages layers it in both cases.
        layered = (
            "layered-venv",
            f"{base}-layered",
            f'python3 -m venv --system-site-packages "{base}-layered" && '
            f'SP=$(python3 -c "import site; print(chr(10).join(site.getsitepackages()))") && '
            f'for d in "{base}-layered"/lib*/python3*/site-packages; do '
            f'echo "$SP" > "$d/_site_module.pth"; done && '
            f'"{base}-layered/bin/python" -m pip install --progress-bar off {q}',
        )
        isolated = (
            "isolated-venv",
            f"{base}-isolated",
            f'python3 -m venv "{base}-isolated" && '
            f'"{base}-isolated/bin/python" -m pip install --progress-bar off {extra} {q}',
        )
        rungs += [isolated, layered] if isolate_first else [layered, isolated]
    if pymods and not conda:
        if not pips:
            rungs.append(
                (
                    "isolated-venv",
                    f"{base}-isolated",
                    f'python3 -m venv "{base}-isolated" && '
                    f'"{base}-isolated/bin/python" -m pip install --progress-bar off {extra}',
                )
            )
        conda_all = list(
            dict.fromkeys(
                ["python=3.12", "pip", "numpy", "scipy", "pandas", "matplotlib"] + vpk
            )
        )
        add_names = " ".join(shlex.quote(_pkg_name(n)) for n in names)
        rungs.append(
            (
                "pixi-conda-forge",
                f"{base}-pixi",
                f'mkdir -p "{base}-pixi" && cd "{base}-pixi" && pixi init -c conda-forge . >/dev/null && '
                f"pixi add {' '.join(shlex.quote(c) for c in conda_all)}"
                + (
                    f" && (pixi add {add_names} || "
                    f"pixi run python -m pip install --progress-bar off {q})"
                    if names
                    else ""
                ),
            )
        )
    elif not (pymods and not conda):
        pk = list(conda)
        # a pinned old build (e.g. lammps=2023.08.02 for glibc 2.28) may not exist for a
        # new Python: don't force python=3.12 next to a pin, let the solver choose
        pinned_pkg = any(
            re.search(r"[=<>]", c) and not re.match(r"python\b", c) for c in conda
        )
        if pips or not any(re.match(r"python\b", c) for c in pk):
            if not any(re.match(r"python\b", c) for c in pk) and (pips or imports):
                pk.append("python" if pinned_pkg else "python=3.12")
        if pips and not any(re.match(r"pip\b", c) for c in pk):
            pk.append("pip")
        chan_args = " ".join("-c " + shlex.quote(c) for c in chans)
        pip_step = (
            f" && pixi run python -m pip install --progress-bar off {q}" if pips else ""
        )
        rungs.append(
            (
                "pixi",
                f"{base}-pixi",
                f'mkdir -p "{base}-pixi" && cd "{base}-pixi" && pixi init {chan_args} . >/dev/null && '
                f"pixi add {' '.join(shlex.quote(c) for c in pk)}{pip_step}",
            )
        )
        loose = [c for c in pk if not re.match(r"python\b", c)] + ["python", "pip"]
        # the relaxed rung drops version pins, but keeps pins on compiled programs
        # (NO_IMPORT: lammps, gromacs...): those pins are usually there for a reason the
        # solver can't see (glibc 2.28 on the nodes, run #75)
        vtxt = " ".join(verify).lower()
        loose = [
            c
            if c.startswith("python")
            or (re.search(r"[=<>]", c) and _pkg_name(c) in NO_IMPORT)
            else re.split(r"[=<>]", c)[0]
            for c in loose
        ]
        rungs.append(
            (
                "pixi-loose",
                f"{base}-pixi2",
                f'mkdir -p "{base}-pixi2" && cd "{base}-pixi2" && '
                f"pixi init -c conda-forge -c bioconda . >/dev/null && "
                f"pixi add {' '.join(shlex.quote(c) for c in sorted(set(loose)))}{pip_step}",
            )
        )
        pyish = [
            _pkg_name(c)
            for c in conda
            if _pkg_name(c) not in NO_IMPORT and not c.startswith("python")
        ]
        # pip can't provide a compiled program the checks call (lmp, gmx...): a pip rung
        # would "verify" Python imports and then fail at run time (run #75: lmp: command
        # not found after the pip-venv rung)
        needs_binary = any(
            _pkg_name(c).lower() in vtxt and _pkg_name(c) in NO_IMPORT for c in conda
        ) or bool(re.search(r"\blmp\b|\bgmx\b|SU2_CFD|\bnvcc\b", " ".join(verify)))
        if (pyish or pips) and not needs_binary:
            rungs.append(
                (
                    "pip-venv",
                    f"{base}-pip",
                    f'python3 -m venv "{base}-pip" && "{base}-pip/bin/python" -m pip install '
                    f"--progress-bar off {' '.join(shlex.quote(x) for x in pyish)} {q}",
                )
            )
    for name, envdir, build in rungs:
        act = (
            f'set +u; eval "$(cd "{envdir}" && pixi shell-hook)"; set -u; '
            "if declare -F ursa_conda_libs >/dev/null; then ursa_conda_libs; else "
            'export LD_LIBRARY_PATH="$CONDA_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"; fi'
            if name.startswith("pixi")
            else f'. "{envdir}/bin/activate"'
        )
        py = "python3"
        out += (
            f'if [ -z "$LADDER_OK" ] && ladder_try {name} "{envdir}" bash -c {shlex.quote(build)}; then\n'
            f'  ( {act}; ladder_verify {py} ) && {{ {act}; LADDER_OK={name}; ladder_record {name} "{envdir}"; }} '
            f'|| {{ echo "[LADDER] {name} did not verify"; touch "{envdir}/.bad"; }}\n'
            "fi\n"
        )
    if inst.get("apptainer"):
        # the plan also lists container images; they may carry what the rungs lacked
        out += (
            'if [ -z "$LADDER_OK" ]; then\n'
            '  echo "[LADDER] no Python/conda rung worked (tried:$LADDER_TRIED); '
            'continuing with the container image(s)"\n'
            "fi\n"
        )
    else:
        out += (
            'if [ -z "$LADDER_OK" ]; then\n'
            '  echo "[ERROR] no install method produced a working environment (tried:$LADDER_TRIED)"\n'
            "  exit 4\nfi\n"
        )
    out += (
        'if [ -n "${LADDER_MOD_FALLBACK:-}" ]; then\n'
        + _pixi_fallback_only(chans)
        + "fi\n"
    )
    return spack_sh + out


def _spack_block(specs: list[str]) -> str:
    """User-level Spack builds chained to the site's /apps/spack (reuses its packages).

    For compiled codes that are neither a module nor on conda-forge. The user instance
    lives in ~/deep-research-lab/spack and is shared by later jobs.
    """
    q = " ".join(shlex.quote(s) for s in specs)
    names = " ".join(specs)
    return (
        f"# ---- user-level Spack (chained to /apps/spack), for: {names}\n"
        "SP=$HOME/deep-research-lab/spack\n"
        'exec 8>"$SP.lock"; flock 8\n'
        'if [ ! -x "$SP/bin/spack" ]; then\n'
        '  git clone --depth 1 -q https://github.com/spack/spack.git "$SP"\n'
        '  mkdir -p "$SP/etc/spack"\n'
        "  printf 'upstreams:\\n  site:\\n    install_tree: /apps/spack/opt/spack\\n' "
        '> "$SP/etc/spack/upstreams.yaml"\n'
        "fi\n"
        '. "$SP/share/spack/setup-env.sh"\n'
        f'echo "[LADDER] spack: installing {names} (reuses /apps/spack builds)"\n'
        f'spack install -j "${{SLURM_CPUS_ON_NODE:-8}}" --reuse {q}\n'
        "flock -u 8\n"
        f"spack load {q}\n"
        f'echo "[LADDER] spack loaded: {names}"\n'
    )


def _pixi_fallback_only(chans: list[str]) -> str:
    """Modules that failed to load: install them from conda-forge/bioconda instead."""
    return (
        "  export PATH=/apps/pixi/bin:$HOME/.pixi/bin:$PATH\n"
        '  FB="$HOME/deep-research-lab/envs/modfallback-$(echo $LADDER_MOD_FALLBACK | tr " " "-")"\n'
        '  mkdir -p "$FB"; ( cd "$FB" && [ -f .ready ] || { pixi init -c conda-forge -c bioconda . >/dev/null '
        "&& pixi add $LADDER_MOD_FALLBACK && touch .ready; } )\n"
        '  set +u; eval "$(cd "$FB" && pixi shell-hook)"; set -u\n'
        '  echo "[LADDER] replaced modules with conda packages:$LADDER_MOD_FALLBACK"\n'
    )


# --------------------------------------------------------------------------- store + service


PLAN_DIFF_SKIP = {
    "warnings", "plan_before_fix", "fix_changes", "fix_notes", "fix_diff",
    "catalog_generated", "caveats", "fix_concerns", "url_checks",
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
        self._warm_checked: dict[str, float] = {}  # target -> last ensure_warm()
        self._stocked_out: dict[
            str, set[str]
        ] = {}  # target -> partitions GCP can't fill
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
            if "verdict" not in cols:  # outputs/verdict.json: known-answer checks
                conn.execute("ALTER TABLE lab_runs ADD COLUMN verdict TEXT")
            if "smoke" not in cols:  # smoke-test rounds and the plan as submitted
                conn.execute("ALTER TABLE lab_runs ADD COLUMN smoke TEXT")
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
        for k in ("plan", "files", "data_sources", "verdict", "smoke"):
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
        for k in ("plan", "files", "verdict", "smoke"):
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
        return (
            validate_plan(tgt, plan)
            + self.source_warnings(plan)
            + self.url_warnings(plan)
            + self.env_warnings(plan)
            + self.match_warnings(tgt, plan)
        )

    def match_warnings(self, tgt, plan: dict) -> list[str]:
        """Partition and time-limit advice from the workload shape and past runs."""
        if not tgt or not getattr(tgt, "partitions", None):
            return []
        warns = []
        part, why = suggest_partition(tgt, plan, self._stocked_out.get(tgt.name))
        if why:
            warns.append(f"Partition: {why}; consider resources.partition = '{part}'")
        hist = time_from_history(self.db_path, plan)
        tl = str((plan.get("resources") or {}).get("time_limit") or "01:00:00")
        if hist and TIME_RE.fullmatch(tl) and _hours(tl) > 2.5 * _hours(hist):
            warns.append(
                f"Time limit {tl} is far above similar past runs (suggested {hist}); "
                "a tighter limit lowers the worst-case cost"
            )
        return warns

    @staticmethod
    def url_warnings(plan: dict) -> list[str]:
        """Download URLs that failed their check on a compute node (while planning)."""
        bad = [
            u for u in (plan.get("url_checks") or [])
            if isinstance(u, dict) and u.get("ok") is False
            and u.get("url") in script_urls(plan)
        ]  # fmt: skip
        return [
            f"URL did not download on the cluster ({u.get('status') or 'no answer'}): "
            f"{str(u.get('url'))[:160]}"
            for u in bad[:5]
        ]

    def env_warnings(self, plan: dict) -> list[str]:
        """Nothing blocks on ladder memory; this only notes a known-good environment."""
        return []

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
            probes = self._run_probes(run_id, run, tgt, title)
            notes = self.env_notes(tgt)
            if notes:
                probes += "\n" + notes + "\n"
            prompt = PLAN_PROMPT.format(
                target=_describe(tgt, full=True),
                probes=probes,
                lessons=labguard.prompt_block(
                    " ".join(
                        [
                            title,
                            run.get("request") or "",
                            (run["selection"] or "")[:20000],
                        ]
                    ),
                    self.state_dir,
                ),
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
            plan["url_checks"] = self._check_urls(run_id, tgt, plan)
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
                lessons=labguard.prompt_block(json.dumps(body), self.state_dir),
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
        concerns = fix_concerns(before, plan)
        if concerns:
            notes.insert(0, "".join(f"REVIEW: {c}. " for c in concerns).strip())
        plan = {
            **plan,
            "warnings": warns,
            "plan_before_fix": before,
            "fix_changes": changes,
            "fix_concerns": concerns,
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
        fclass, fadvice = classify_failure(log)
        prompt = RUNFIX_PROMPT.format(
            target=_describe(tgt, full=True),
            lessons=labguard.prompt_block(
                json.dumps(plan) + " " + _log_for_fix(log, 3000), self.state_dir
            ),
            state=(run.get("slurm_state") or run.get("stage") or "FAILED")
            + (f". DIAGNOSIS ({fclass}): {fadvice}" if fadvice else ""),
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
        concerns = fix_concerns(plan, new)
        if concerns:
            notes = (notes + " " + "".join(f"REVIEW: {c}. " for c in concerns)).strip()
        new["fix_changes"] = changes
        new["fix_concerns"] = concerns
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
            if self._smoke_applies(tgt, plan):
                # Smoke test first, on the warm node: the real run only starts if the
                # cut-down run of this exact plan passes (decisions, Lab plan v3).
                tgt.upload(run_id, files, fresh=True)
                for src in sources:
                    self.sources.record_use(src, "lab_run", run_id)
                _IN_FLIGHT.discard(run_id)
                self._start_smoke(run_id, tgt, plan, round_no=1, original=plan)
                self.ensure_watcher()
                return self.get(run_id) or {}
            job = self._dispatch(run_id, tgt, plan, files)
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
            stage="Queued on the warm Lab node"
            if job.startswith("warm:")
            else "Queued, waiting for a node",
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
        elif run["status"] == "smoke":
            tgt = self.target(run["target"])
            task = (run.get("smoke") or {}).get("task")
            if tgt and task:
                try:
                    tgt.warm_cancel(task)
                except Exception:
                    pass  # the task will still end with the worker
            self._update(
                run_id,
                only_if=("smoke",),
                status="cancelled",
                stage="Cancelled during the smoke test",
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

    def _switch_partition(self, run: dict, tgt) -> bool:
        """A queued job whose nodes keep failing on a stocked-out partition moves to one
        that has capacity (same plan, new partition), once. True when it moved."""
        plan = dict(run.get("plan") or {})
        if plan.get("partition_switched"):
            return False
        out = stocked_out_partitions(tgt)
        self._stocked_out[tgt.name] = out
        cur = (plan.get("resources") or {}).get("partition") or tgt.default_partition
        if cur not in out:
            return False
        new, why = suggest_partition(tgt, plan, out)
        if new == cur:
            return False
        try:
            tgt.cancel(run["job_id"])
        except Exception:
            return False
        plan["resources"] = {**(plan.get("resources") or {}), "partition": new}
        plan["partition_switched"] = f"{cur} -> {new} (GCP had no capacity for {cur})"
        script = build_sbatch(run["id"], plan, tgt, self._safe_sources(plan))
        try:
            tgt.upload(
                run["id"],
                {"run.sbatch": script, "plan.json": json.dumps(plan, indent=2)},
                fresh=False,
            )
            job = tgt.sbatch_uploaded(run["id"])
        except Exception as e:
            self._update(
                run["id"],
                only_if=ACTIVE,
                error=f"partition switch failed: {str(e)[:200]}",
            )
            return False
        self._update(
            run["id"],
            only_if=ACTIVE,
            plan=plan,
            script=script,
            job_id=job,
            estimate_usd=estimate_cost(tgt, plan),
            stage=f"Moved from {cur} to {new}: GCP had no capacity for {cur}; queued again",
        )
        return True

    # ---- planner probes (warm node) ---------------------------------------
    PROBE_MAX = 6
    PROBE_WAIT_S = 420

    def _warm_exec(
        self, tgt, task: str, script: str, wait_s: int
    ) -> tuple[int | None, str]:
        """Run a short read-only script on the warm node; (rc, output). Never raises."""
        try:
            tgt.warm_enqueue(task, {"run.sh": script}, need_sec=min(wait_s, 600))
            self._keep_warm(tgt, force=True)
        except Exception as e:
            return None, f"(could not reach the warm node: {str(e)[:200]})"
        t0 = time.time()
        while time.time() - t0 < wait_s:
            try:
                t = tgt.warm_task(task)
            except Exception:
                t = {"where": "?"}
            if t.get("where") == "done":
                return t.get("rc"), tgt.warm_task_log(task, 40000)
            time.sleep(5)
        try:
            tgt.warm_cancel(task)
        except Exception:
            pass
        return None, "(timed out waiting for the warm node)"

    def _run_probes(self, run_id: int, run: dict, tgt, title: str) -> str:
        """Ask the model which facts to check, check them on the warm node, return text."""
        if not tgt or not getattr(tgt, "warm", None):
            return ""
        try:
            self._update(
                run_id,
                only_if=("planning",),
                stage="Choosing what to check on the cluster",
            )
            reply, cost = self._ask(
                PROBE_PROMPT.format(
                    max_checks=self.PROBE_MAX,
                    target=_describe(tgt, full=True)[:30000],
                    title=title,
                    text=_strip_sources(run["selection"] or "")[:15000],
                    question_line=f"\nTHE USER'S INSTRUCTION: {run['request']}\n"
                    if run.get("request")
                    else "",
                ),
                search=False,
            )
            self._add_cost(run_id, cost)
            data = extract_json(reply)
            checks = (data.get("checks") if isinstance(data, dict) else data) or []
            script, ok = probe_script(
                [c for c in checks if isinstance(c, dict)][: self.PROBE_MAX]
            )
            if not ok:
                return ""
            self._update(
                run_id,
                only_if=("planning",),
                stage=f"Checking {len(ok)} facts on the warm Lab node (software help, versions, URLs)",
            )
            rc, out = self._warm_exec(
                tgt, f"probe-{run_id}-{int(time.time())}", script, self.PROBE_WAIT_S
            )
        except Exception as e:  # probes help; they never block planning
            return f"\n(Cluster checks were skipped: {str(e)[:160]})\n"
        return (
            "\nFACTS CHECKED ON THE CLUSTER JUST NOW (trust these over memory; use the exact "
            "flags, versions, signatures and file names shown):\n" + out[:24000] + "\n"
        )

    def _check_urls(self, run_id: int, tgt, plan: dict) -> list[dict]:
        """curl every download URL of a new plan from the warm node."""
        urls = script_urls(plan)
        if not urls or not tgt or not getattr(tgt, "warm", None):
            return []
        self._update(
            run_id,
            only_if=("planning",),
            stage=f"Checking {len(urls)} download URL(s) on the cluster",
        )
        lines = ["#!/bin/bash"]
        for u in urls:
            q = shlex.quote(u)
            lines.append(
                f"echo \"URL {u} $(curl -sSL -m 25 -o /dev/null -w '%{{http_code}} %{{size_download}}' -r 0-65535 {q} 2>&1 | tail -c 120)\""
            )
        rc, out = self._warm_exec(
            tgt, f"urls-{run_id}-{int(time.time())}", "\n".join(lines) + "\n", 240
        )
        res: list[dict[str, Any]] = []
        for u in urls:
            m = re.search(r"^URL " + re.escape(u) + r" (\S+)(?: (\S+))?", out, re.M)
            if not m:
                res.append({"url": u, "ok": None, "status": "not checked"})
                continue
            code = m.group(1)
            # 206 = the partial range we asked for
            ok = code.isdigit() and 200 <= int(code) < 400
            res.append(
                {
                    "url": u,
                    "ok": ok,
                    "status": f"HTTP {code}" if code.isdigit() else code[:60],
                }
            )
        return res

    def env_notes(self, tgt) -> str:
        """Which install rung worked for recent package lists (ladder.jsonl), for prompts."""
        if not tgt or not getattr(tgt, "warm", None):
            return ""
        try:
            raw = tgt.run(
                "tail -n 400 ~/deep-research-lab/envs/ladder.jsonl 2>/dev/null",
                timeout=30,
            ).stdout.decode("utf-8", "replace")
        except Exception:
            return ""
        runs = {}
        for ln in raw.splitlines():
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if d.get("rung") and d.get("rung") not in ("layered-venv", "pixi"):
                runs[d.get("key")] = (
                    d  # only the interesting ones: a fallback was needed
                )
        if not runs:
            return ""
        return (
            "Recent jobs needed a fallback install method (the harness does this "
            "automatically; list packages normally): "
            + "; ".join(
                f"{d.get('run')}: {d.get('rung')}" for d in list(runs.values())[-8:]
            )
        )

    # ---- smoke test and warm node ------------------------------------------
    SMOKE_MAX_ROUNDS = 3  # AI fixes of a failing smoke test before giving up
    WARM_RECHECK_S = 120.0

    @staticmethod
    def _plan_core(plan: dict) -> dict:
        """The parts of a plan that decide what runs (for 'is this still the same plan')."""
        return {
            k: v
            for k, v in (plan or {}).items()
            if k not in PLAN_DIFF_SKIP
            and k not in ("fix_changes", "fix_notes", "smoke_fix")
        }

    @staticmethod
    def _smoke_applies(tgt, plan: dict) -> bool:
        if not getattr(tgt, "warm", None) or plan.get("smoke") is False:
            return False
        r = plan.get("resources") or {}
        # CPU warm node: a GPU plan's smoke test would fail for want of a GPU
        return _int(r.get("gpus"), 0) == 0

    @staticmethod
    def _warm_full_ok(tgt, plan: dict) -> bool:
        """Short single-node CPU runs on the warm node's partition skip the node boot."""
        cfg = getattr(tgt, "warm", None)
        if not cfg:
            return False
        r = plan.get("resources") or {}
        part = r.get("partition") or tgt.default_partition
        tl = str(r.get("time_limit") or "01:00:00")
        tl = tl if TIME_RE.fullmatch(tl) else "01:00:00"
        return (
            part == cfg["partition"]
            and max(_int(r.get("nodes"), 1), 1) == 1
            and _int(r.get("gpus"), 0) == 0
            and _hours(tl) * 60 <= cfg["max_full_min"]
        )

    def _keep_warm(self, tgt, force: bool = False) -> str | None:
        """Make sure a warm worker exists (at most every WARM_RECHECK_S seconds)."""
        now = time.time()
        if (
            not force
            and now - self._warm_checked.get(tgt.name, 0) < self.WARM_RECHECK_S
        ):
            return None
        self._warm_checked[tgt.name] = now
        return tgt.ensure_warm()

    @staticmethod
    def _home(path: str) -> str:
        return "$HOME" + path[1:] if path.startswith("~") else path

    def _dispatch(self, run_id: int, tgt, plan: dict, files: dict | None) -> str:
        """Start the real run: on the warm node when it fits, else as its own Slurm job.

        `files` None means they are already in the run folder (after a smoke test).
        Returns the job id ('warm:<task>' for the warm node).
        """
        if self._warm_full_ok(tgt, plan):
            if files:
                tgt.upload(run_id, files, fresh=True)
            r = plan.get("resources") or {}
            tl = str(r.get("time_limit") or "01:00:00")
            sec = int(_hours(tl if TIME_RE.fullmatch(tl) else "01:00:00") * 3600)
            d = self._home(tgt.job_dir(run_id))
            task = f"full-{run_id}"
            run_sh = (
                "#!/bin/bash\n"
                f'RUN="{d}"\nexport SLURM_SUBMIT_DIR="$RUN"\ncd "$RUN"\n'
                'echo "[INFO] running on the warm Lab node $(hostname -s)" >> job.log\n'
                f"exec timeout --kill-after=30 {sec} bash run.sbatch >> job.log 2>&1\n"
            )
            tgt.warm_enqueue(
                task, {"run.sh": run_sh}, need_sec=sec + 120, exclusive=True
            )
            self._keep_warm(tgt, force=True)
            return "warm:" + task
        if files:
            return tgt.submit(run_id, files)
        return tgt.sbatch_uploaded(run_id)

    def _start_smoke(
        self, run_id: int, tgt, plan: dict, round_no: int, original: dict,
        rounds: list | None = None,
    ) -> None:  # fmt: skip
        cfg = tgt.warm or {}
        d = self._home(tgt.job_dir(run_id))
        task = f"smoke-{run_id}-{round_no}"
        sec = int(cfg.get("smoke_min", 15)) * 60
        run_sh = (
            "#!/bin/bash\n"
            f'RUN="{d}"\nS="$RUN/smoke"\nrm -rf "$S"; mkdir -p "$S"\n'
            'cp "$RUN/run.sbatch" "$S/"; for f in plan.json sources.json; do '
            '[ -f "$RUN/$f" ] && cp "$RUN/$f" "$S/"; done\n'
            'cd "$S"\nexport SLURM_SUBMIT_DIR="$S" LAB_SMOKE=1\n'
            f"exec timeout --kill-after=20 {sec} bash run.sbatch > job.log 2>&1\n"
        )
        tgt.warm_enqueue(task, {"run.sh": run_sh}, need_sec=sec + 60)
        state = self._keep_warm(tgt, force=True) or ""
        self._update(
            run_id,
            only_if=("submitting", "smoke"),
            status="smoke",
            stage=f"Smoke test (round {round_no}): "
            + (
                "warm Lab node booting"
                if state.startswith(("started", "pending"))
                else "queued on the warm Lab node"
            ),
            error=None,
            smoke={
                "task": task,
                "round": round_no,
                "rounds": rounds or [],
                "original": self._plan_core(original),
                "fixing": False,
            },
        )

    _SMOKE_ERR = re.compile(
        r"Traceback|Error:|error:|No such file|not found|Segmentation fault|Killed|"
        r"command not found|FAILED|Failed \(exit",
    )

    def _poll_smoke(self, run: dict, tgt) -> None:
        sm = dict(run.get("smoke") or {})
        if sm.get("fixing") and run["id"] not in _SMOKE_FIXING:
            # the dashboard restarted while the AI was fixing it: nothing will finish it
            self._fail_smoke(
                run, sm, str((sm.get("rounds") or [{}])[-1].get("log_tail") or ""),
                "Smoke test interrupted (the dashboard restarted during the AI fix)",
                "Submit again to retry.",
            )  # fmt: skip
            return
        if sm.get("fixing") or not sm.get("task"):
            return
        run_id = run["id"]
        t = tgt.warm_task(sm["task"])
        n = sm.get("round", 1)
        if t["where"] in ("queue", "missing"):
            if t["where"] == "missing":
                sm["missing"] = int(sm.get("missing") or 0) + 1
                if sm["missing"] < 6:  # renames between polls; look again
                    self._update(run_id, only_if=("smoke",), smoke=sm)
                    return
                self._update(
                    run_id,
                    only_if=("smoke",),
                    status="failed",
                    stage="Smoke test lost",
                    error="The smoke-test task disappeared from the warm node's queue "
                    "(the node may have been reclaimed). Submit again to retry.",
                    finished_at=_now(),
                )
                return
            state = self._keep_warm(tgt) or ""
            if state:
                self._update(
                    run_id,
                    only_if=("smoke",),
                    stage=f"Smoke test (round {n}): "
                    + (
                        "warm Lab node booting"
                        if state.startswith(("started", "pending"))
                        else "queued on the warm Lab node"
                    ),
                )
            return
        if t["where"] == "running":
            st = tgt.read_file(run_id, "smoke/stage.txt", 2000).strip().splitlines()
            self._update(
                run_id,
                only_if=("smoke",),
                node=t["node"],
                stage=f"Smoke test (round {n}) on {t['node']}"
                + (f": {st[-1]}" if st else ""),
            )
            return
        # done
        rc = t["rc"]
        log = tgt.read_file(run_id, "smoke/job.log", 60000)
        plan = run.get("plan") or {}
        missing = tgt.missing_outputs(
            run_id, "smoke", [str(x) for x in plan.get("expected_outputs") or []]
        )
        clean_timeout = rc == 124 and not self._SMOKE_ERR.search(log[-20000:])
        passed = (rc == 0 and not missing) or clean_timeout
        fclass, fadvice = ("", "") if passed else classify_failure(log)
        sm["rounds"] = list(sm.get("rounds") or []) + [
            {
                "round": n,
                "rc": rc,
                "missing": missing,
                "passed": passed,
                "class": fclass,
                "note": "timed out without errors (the script may ignore LAB_SMOKE)"
                if clean_timeout
                else "",
                "seconds": (t["finished"] - t["started"])
                if t["finished"] and t["started"]
                else None,
                "log_tail": log[-4000:],
            }
        ]
        if passed:
            same = self._plan_core(plan) == sm.get("original")
            if same:
                try:
                    job = self._dispatch(run_id, tgt, plan, None)
                except Exception as e:
                    self._update(
                        run_id,
                        only_if=("smoke",),
                        status="failed",
                        stage="Submit failed after the smoke test",
                        error=str(e)[:500],
                        smoke=sm,
                        finished_at=_now(),
                    )
                    return
                self._update(
                    run_id,
                    only_if=("smoke",),
                    status="queued",
                    stage=f"Smoke test passed (round {n}); "
                    + (
                        "queued on the warm Lab node"
                        if job.startswith("warm:")
                        else "queued, waiting for a node"
                    ),
                    job_id=job,
                    smoke=sm,
                )
            else:
                # the AI changed the plan to get here: a person approves it first
                self._update(
                    run_id,
                    only_if=("smoke",),
                    status="draft",
                    stage=f"Smoke test passed after {n - 1} AI fix(es): review the changes, then submit",
                    smoke=sm,
                    submitted_at=None,
                )
            return
        if n >= self.SMOKE_MAX_ROUNDS:
            self._fail_smoke(run, sm, log, f"Smoke test failed {n} times", fadvice)
            return
        # the same failure class twice in a row means the AI is not getting anywhere
        prev = [r.get("class") for r in sm["rounds"][:-1]]
        if fclass in ("container", "timeout", "oom") or (
            prev and prev[-1] == fclass and fclass in ("install", "missing-feature")
        ):
            self._fail_smoke(
                run, sm, log,
                f"Smoke test failed ({fclass}); not handed to the AI again", fadvice,
            )  # fmt: skip
            return
        sm["fixing"] = True
        if not self._update(
            run_id,
            only_if=("smoke",),
            stage=f"Smoke test failed (round {n}, exit {rc}"
            + (f", missing {', '.join(missing[:3])}" if missing else "")
            + "); AI is fixing it",
            smoke=sm,
        ):
            return
        _SMOKE_FIXING.add(run_id)
        threading.Thread(
            target=self._smoke_fix,
            args=(run_id, log, rc, missing, fadvice),
            daemon=True,
        ).start()

    def _fail_smoke(
        self, run: dict, sm: dict, log: str, why: str, advice: str = ""
    ) -> None:
        dest = self.results_dir / f"run_{run['id']}"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "job.log").write_text(log)  # so Fix with AI and the Log view have it
        last = (sm.get("rounds") or [{}])[-1]
        self._update(
            run["id"],
            only_if=("smoke",),
            status="failed",
            stage=why,
            error=f"{why}; last exit {last.get('rc')}"
            + (
                f", missing outputs: {', '.join(last.get('missing') or [])}"
                if last.get("missing")
                else ""
            )
            + ". The full run was not started."
            + (f" {advice}" if advice else ""),
            smoke={**sm, "fixing": False},
            finished_at=_now(),
        )

    def _smoke_fix(
        self, run_id: int, log: str, rc: int | None, missing: list[str],
        advice: str = "",
    ) -> None:  # fmt: skip
        """Background: ask the AI to fix a failed smoke test, then run the next round."""
        try:
            self._smoke_fix_inner(run_id, log, rc, missing, advice)
        finally:
            _SMOKE_FIXING.discard(run_id)

    def _smoke_fix_inner(
        self, run_id: int, log: str, rc: int | None, missing: list[str],
        advice: str = "",
    ) -> None:  # fmt: skip
        run = self.get(run_id)
        if not run or run["status"] != "smoke":
            return
        sm = dict(run.get("smoke") or {})
        tgt = self.target(run["target"])
        plan = {
            k: v for k, v in (run.get("plan") or {}).items() if k not in PLAN_DIFF_SKIP
        }
        try:
            if not tgt:
                raise ValueError("no target")
            self._fresh_catalog(tgt)
            extra = (
                f"Expected output files that were missing or empty: {', '.join(missing)}. "
                if missing
                else ""
            )
            prompt = RUNFIX_PROMPT.format(
                target=_describe(tgt, full=True),
                lessons=labguard.prompt_block(
                    json.dumps(plan) + " " + log[-3000:], self.state_dir
                ),
                state="SMOKE TEST FAILED: the plan ran with LAB_SMOKE=1 (a cut-down run) on the "
                "warm Lab node. "
                + extra
                + (f"DIAGNOSIS: {advice} " if advice else "")
                + "Fix the cause. If the script ignores LAB_SMOKE, "
                "also make it honour it (smallest sizes, every step, every output)",
                exit_code=rc,
                elapsed="-",
                log=_log_for_fix(log),
                plan=json.dumps(plan, indent=1)[:60000],
            )
            reply, cost = self._ask(prompt, search=False)
            self._add_cost(run_id, cost)
            out = extract_json(reply)
            new = out.get("plan") if isinstance(out, dict) else None
            changes = (
                [str(c) for c in (out.get("changes") or [])][:20]
                if isinstance(out, dict)
                else []
            )
            if not isinstance(new, dict) or not all(
                k in new for k in ("script", "resources", "install")
            ):
                raise ValueError("the AI returned no usable plan")
            if not changes or self._plan_core(new) == self._plan_core(plan):
                raise ValueError(
                    "the AI found nothing to fix"
                    + (
                        f": {str(out.get('notes'))[:200]}"
                        if isinstance(out, dict) and out.get("notes")
                        else ""
                    )
                )
            dropped = _dropped_options(plan, new)
            concerns = fix_concerns(plan, new)
            original = sm.get("original") or {}
            new["fix_changes"] = list(
                (run.get("plan") or {}).get("fix_changes") or []
            ) + [f"smoke round {sm.get('round', 1)}: {c}" for c in changes]
            new["fix_notes"] = (
                (f"REVIEW: removes option(s) {', '.join(dropped)}. " if dropped else "")
                + "".join(f"REVIEW: {c} " for c in concerns)
                + str(out.get("notes") or "")
            ).strip()
            new["fix_concerns"] = (
                list((run.get("plan") or {}).get("fix_concerns") or []) + concerns
            )
            new["fix_diff"] = plan_diff(original, new)
            if run.get("data_sources"):
                new["data_sources"] = list(run["data_sources"])
            new["warnings"] = self._check(tgt, new)
            if tgt.catalog:
                new["catalog_generated"] = tgt.catalog.get("generated")
            sources = self._plan_sources(new)
            script = build_sbatch(run_id, new, tgt, sources)
            files = {"run.sbatch": script, "plan.json": json.dumps(new, indent=2)}
            tgt.upload(run_id, files, fresh=False)
            if not self._update(
                run_id, only_if=("smoke",), plan=new, script=script,
                estimate_usd=estimate_cost(tgt, new),
            ):  # fmt: skip
                return  # cancelled meanwhile
            self._start_smoke(
                run_id, tgt, new, int(sm.get("round", 1)) + 1,
                original={}, rounds=sm.get("rounds"),
            )  # fmt: skip
            # keep the plan Chuck submitted as the reference, not the fixed one
            cur = self.get(run_id) or {}
            sm2 = dict(cur.get("smoke") or {})
            sm2["original"] = original
            self._update(run_id, only_if=("smoke",), smoke=sm2)
        except Exception as e:
            run = self.get(run_id) or run
            self._fail_smoke(
                run, sm, log, f"Smoke test failed; AI fix failed ({str(e)[:160]})"
            )

    def warm_status(self, name: str | None = None) -> dict:
        tgt = self.target(name)
        if not tgt or not getattr(tgt, "warm", None):
            return {"enabled": False}
        return tgt.warm_status()

    def warm_start(self, name: str | None = None) -> dict:
        tgt = self.target(name)
        if not tgt or not getattr(tgt, "warm", None):
            raise ValueError("no warm worker configured for this target")
        state = self._keep_warm(tgt, force=True)
        return {"state": state, **tgt.warm_status()}

    def warm_stop(self, name: str | None = None) -> dict:
        tgt = self.target(name)
        if not tgt or not getattr(tgt, "warm", None):
            raise ValueError("no warm worker configured for this target")
        tgt.warm_stop()
        return {"stopping": True}

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
        tgt = self.target(run["target"])
        if run["status"] == "smoke" and tgt:
            text = tgt.read_file(run_id, "smoke/job.log", 200000)
            data = text.encode()
            off = offset if 0 <= offset <= len(data) else 0
            return {
                "text": data[off:].decode("utf-8", "replace"),
                "size": len(data),
                "source": "smoke",
            }
        if not run.get("job_id"):
            return {"text": "", "size": 0, "source": "none"}
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
        if tgt and run["status"] == "smoke":
            return self._poll_smoke(run, tgt)
        if tgt and str(run.get("job_id") or "").startswith("warm:"):
            self._keep_warm(tgt)
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
            if fails >= NODE_FAIL_WARN and self._switch_partition(run, tgt):
                return
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
        verdict = labguard.read_verdict(dest)
        if verdict is not None:
            self._update(run["id"], verdict=verdict)
            run = self.get(run["id"]) or run
        note, cost = self._analyze(run, dest, final)
        self._add_cost(run["id"], cost)
        stage = "Completed" if final == "completed" else f"Ended: {state or 'unknown'}"
        if final == "completed" and verdict and verdict.get("pass") is False:
            stage = "Completed, known-answer check FAILED"
        changed = self._update(
            run["id"],
            only_if=("analyzing",),
            status=final,
            stage=stage,
            result_md=note,
            finished_at=_now(),
        )
        if changed and final == "completed":
            self._learn_from_fix(run)

    def _learn_from_fix(self, run: dict) -> None:
        """A completed AI fix of a failed run becomes a lesson for future plans."""
        plan = run.get("plan") or {}
        if not run.get("rerun_of") or not plan.get("fix_changes"):
            return
        failed = self.get(int(run["rerun_of"]))
        if not failed or failed.get("status") != "failed":
            return
        lesson = labguard.lesson_from_fix(failed, plan)
        if lesson:
            try:
                labguard.add_learned(
                    self.state_dir,
                    lesson["text"],
                    lesson["match"],
                    f"{lesson['source']} fixed by run #{run['id']}",
                )
            except (OSError, ValueError):
                pass  # a lesson is a bonus; never fail a finished run over it

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
            verdict=json.dumps(run.get("verdict"))[:3000]
            if run.get("verdict")
            else "none written",
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
