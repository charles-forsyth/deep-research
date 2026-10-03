"""Cluster access for Lab runs: one Slurm cluster reached over SSH (v0.52.0).

Moved out of `lab.py` unchanged (K20): `SlurmSSHTarget` (SSH through gcloud IAP or a
plain ssh host, one ControlMaster connection per target, sbatch/squeue/sacct, run
folders, the warm worker and its spool, file transfer, catalog cache), `ScopedTarget`
(a workspace's view of the shared target: its own run folders and task names) and
`load_targets`. No model calls here and nothing from the Lab's database; `lab.py`
drives it. The read-only hpc-agent MCP server is meant to build on this module.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shlex
import subprocess
import tarfile
import threading
import time
from datetime import datetime
from pathlib import Path

from deepresearch.dashboard import labcores

MAX_FETCH_BYTES = 200 * 1024 * 1024  # whole outputs folder
MAX_FILE_BYTES = 50 * 1024 * 1024  # any single file


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- targets


class TargetError(RuntimeError):
    pass


def _tar_b64(files: dict[str, str]) -> bytes:
    """Files as a base64 tar.gz for `base64 -d | tar xzf -` on the cluster."""
    import io

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
            "idle_min": int(cfg.get("idle_min", 20)),  # 0 = never exit when idle
            "always_on": bool(cfg.get("always_on", False)),
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
        idle0 = int(cfg["idle_min"])
        burst = idle0 if idle0 > 0 else max(1, int(cfg.get("burst_idle_min", 20)))
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
            # only the first worker may be always-on (idle_min 0); extra workers started
            # for a backlog get the burst idle limit, or they would run forever (v0.48.2:
            # a planning burst left a second always-on node idle)
            f'while [ "$n" -lt "$want" ]; do idle={idle0}; [ "$n" -gt 0 ] && idle={burst}; '
            f"ok=started:$(sbatch --parsable --job-name=lab-warm "
            f"-p {part} -N 1 --exclusive -t {tl} --signal=B:USR1@60 "
            f'-o "$W/worker-%j.log" '
            f'--export=ALL,WARM="$W",IDLE_MIN=$idle,MAX_PAR={cfg["max_par"]},'
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
            "idle_min": int((self.warm or {}).get("idle_min", 20)),
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
            # shared vs whole-node, when the cluster publishes it (labcores)
            if isinstance(p.get("exclusive"), bool):
                parts[p["name"]]["exclusive"] = p["exclusive"]
            # the catalog's default applies only when lab_targets.json names none:
            # the person's choice wins (2026-09-30: computehigh, not the catalog's standard)
            if p.get("default") and not self.cfg.get("default_partition"):
                self.default_partition = p["name"]
        if parts:
            self.partitions = parts
            if self.default_partition not in parts:
                self.default_partition = next(iter(parts))

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
                # the catalog's text may call another partition the default; ours wins
                + (
                    f". {re.sub(r'^Default\.\s*', '', v['use_for'])}"
                    if v.get("use_for")
                    else ""
                )
            )
        warm = getattr(self, "warm", None) or {}
        tail = (
            f"\nUse `{self.default_partition}` unless the job needs GPUs, more memory or "
            "many nodes"
            + (
                f"; single-node CPU jobs on `{warm['partition']}` run on the always-on warm "
                "node and start in seconds"
                if warm.get("partition") == self.default_partition
                else ""
            )
            + "."
        )
        cores = labcores.describe(self)
        return (
            "Partitions (nodes created on demand; prices per node-hour):\n"
            + "\n".join(rows)
            + tail
            + (f"\n{cores}" if cores else "")
        )

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
            f"Default partition: {self.default_partition}. Nodes boot on demand "
            f"(1-5 minutes before the job starts).\n{labcores.describe(self)}\n"
            f"Existing environment modules: {', '.join(self.modules) or 'none'}.\n"
            f"{self.software_notes}"
        )


class ScopedTarget:
    """A cluster target seen from one workspace: same SSH connection, catalog and warm
    worker, but its Lab runs live under <remote_root>/<prefix>/run_<id> and its warm-node
    tasks and Slurm job names carry the prefix, so run #1 of a new workspace never
    touches run #1 of Main (Main keeps prefix '' and its original folders)."""

    def __init__(self, base: "SlurmSSHTarget", prefix: str):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_prefix", prefix)

    def __getattr__(self, name: str):
        return getattr(self._base, name)

    def __setattr__(self, name: str, value) -> None:
        setattr(self._base, name, value)

    @property
    def base(self) -> "SlurmSSHTarget":
        return self._base

    @property
    def ws_prefix(self) -> str:
        return self._prefix

    def job_dir(self, run_id: int) -> str:
        return f"{self._base.remote_root}/{self._prefix}/run_{run_id}"

    # every method that builds the run folder from job_dir must use ours, so call the
    # base implementation with self (bound to this scoped view)
    def submit(self, run_id, files):
        return SlurmSSHTarget.submit(self, run_id, files)  # type: ignore[arg-type]

    def upload(self, run_id, files, fresh=True):
        return SlurmSSHTarget.upload(self, run_id, files, fresh)  # type: ignore[arg-type]

    def sbatch_uploaded(self, run_id):
        return SlurmSSHTarget.sbatch_uploaded(self, run_id)  # type: ignore[arg-type]

    def read_file(self, run_id, rel, limit=60000):
        return SlurmSSHTarget.read_file(self, run_id, rel, limit)  # type: ignore[arg-type]

    def missing_outputs(self, run_id, sub, patterns):
        return SlurmSSHTarget.missing_outputs(self, run_id, sub, patterns)  # type: ignore[arg-type]

    def status(self, run_id, job_id):
        return SlurmSSHTarget.status(self, run_id, job_id)  # type: ignore[arg-type]

    def log(self, *a, **k):
        return SlurmSSHTarget.log(self, *a, **k)  # type: ignore[arg-type]

    def fetch(self, run_id, dest):
        return SlurmSSHTarget.fetch(self, run_id, dest)  # type: ignore[arg-type]

    def _warm_run_status(self, run_id, task):
        return SlurmSSHTarget._warm_run_status(self, run_id, task)  # type: ignore[arg-type]


def task_prefix(tgt) -> str:
    """'' for Main; '<ws>-' for other workspaces (warm-node task and job names)."""
    p = getattr(tgt, "ws_prefix", "") or ""
    return (p.removeprefix("ws-") + "-") if p else ""


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
