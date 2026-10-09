"""`deep-research status`: one cheap look at what is running and what just finished.

Built for checking back on long work from a terminal or an agent harness: research runs
in flight (with their age and log), reports that finished, failed or crashed recently,
Lab runs on the cluster or waiting for review, and whether the dashboard is up. No model
call, no cluster call and no network apart from the dashboard's local health probe; the
only write is the usual liveness sweep (a dead 'running' row becomes 'crashed').
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Lab states (mirrors dashboard/lab.py; importing lab.py costs ~0.3 s)
LAB_ACTIVE = ("submitting", "smoke", "queued", "running", "fetching", "analyzing")
LAB_WAITING = ("planning", "draft", "plan_failed")
LAB_FINAL = ("completed", "failed", "cancelled")


def _age_min(stamp: Any, now: datetime) -> int | None:
    try:
        t = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is not None:
        t = t.replace(tzinfo=None)
    return max(0, int((now - t).total_seconds() // 60))


def _session(row: sqlite3.Row, now: datetime, logs_dir: Path) -> dict:
    d = {
        "id": row["id"],
        "prompt": (row["prompt"] or "").split("\n\n")[0][:200],
        "status": row["status"],
        "depth": row["depth"],
        "parent_id": row["parent_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "result_chars": len(row["result"] or ""),
    }
    if row["status"] == "running":
        d["age_min"] = _age_min(row["created_at"], now)
        log = logs_dir / f"session_{row['id']}.log"
        d["log"] = str(log) if log.exists() else None
    return d


def _lab_run(row: sqlite3.Row) -> dict:
    from deepresearch.dashboard import labverdict

    def js(key: str) -> Any:
        try:
            return json.loads(row[key]) if row[key] else None
        except (ValueError, IndexError, KeyError):
            return None

    plan = js("plan") or {}
    verdict = js("verdict")
    status = row["status"] or ""
    a = labverdict.assess(verdict if isinstance(verdict, dict) else None, status)
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "report": (row["session_prompt"] or "").split("\n\n")[0][:120],
        "title": plan.get("title") if isinstance(plan, dict) else None,
        "status": status,
        "stage": row["stage"],
        "job_id": row["job_id"],
        "slurm_state": row["slurm_state"],
        "estimate_usd": row["estimate_usd"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
        "outcome": (a or {}).get("outcome"),
    }


def workspace_status(slug: str, since_hours: float) -> dict:
    """Status of one workspace's library (reports and Lab runs)."""
    from deepresearch.core import workspace as W
    from deepresearch.core.session import SessionManager

    ws = W.get(slug)
    logs_dir = (
        W.base_dir() / "logs" if ws.is_main else ws.logs_dir
    )  # Main's logs stay in the config dir
    now = datetime.now()
    cutoff = (now - timedelta(hours=since_hours)).isoformat()
    mgr = SessionManager(ws.db_path)
    out: dict[str, Any] = {"workspace": ws.slug, "name": ws.name}
    with sqlite3.connect(mgr.db_path, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        mgr.sweep_running(conn)
        running = conn.execute(
            "SELECT * FROM sessions WHERE status = 'running' ORDER BY created_at"
        ).fetchall()
        recent = conn.execute(
            "SELECT * FROM sessions WHERE status != 'running' AND updated_at >= ? "
            "ORDER BY updated_at DESC",
            (cutoff,),
        ).fetchall()
        counts = dict(
            conn.execute("SELECT status, COUNT(*) FROM sessions GROUP BY status")
        )
        out["research"] = {
            "running": [_session(r, now, logs_dir) for r in running],
            "recent": [_session(r, now, logs_dir) for r in recent],
            "counts": counts,
        }
        has_lab = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lab_runs'"
        ).fetchone()
        lab: dict[str, Any] = {"active": [], "waiting": [], "recent": [], "counts": {}}
        if has_lab:
            q = (
                "SELECT r.*, s.prompt AS session_prompt FROM lab_runs r "
                "LEFT JOIN sessions s ON s.id = r.session_id "
            )
            act = ",".join("?" * len(LAB_ACTIVE))
            wait = ",".join("?" * len(LAB_WAITING))
            fin = ",".join("?" * len(LAB_FINAL))
            lab["active"] = [
                _lab_run(r)
                for r in conn.execute(
                    q + f"WHERE r.status IN ({act}) ORDER BY r.id", LAB_ACTIVE
                )
            ]
            lab["waiting"] = [
                _lab_run(r)
                for r in conn.execute(
                    q + f"WHERE r.status IN ({wait}) AND r.updated_at >= ? "
                    "ORDER BY r.updated_at DESC",
                    (*LAB_WAITING, cutoff),
                )
            ]
            lab["recent"] = [
                _lab_run(r)
                for r in conn.execute(
                    q + f"WHERE r.status IN ({fin}) AND "
                    "COALESCE(r.finished_at, r.updated_at) >= ? "
                    "ORDER BY COALESCE(r.finished_at, r.updated_at) DESC",
                    (*LAB_FINAL, cutoff),
                )
            ]
            lab["counts"] = dict(
                conn.execute("SELECT status, COUNT(*) FROM lab_runs GROUP BY status")
            )
        out["lab"] = lab
    return out


def dashboard_status() -> dict:
    from deepresearch.dashboard import daemon

    state = daemon.read_state()
    if not state:
        return {"running": False}
    healthy = bool(daemon._probe(state["host"], int(state["port"])))
    return {
        "running": True,
        "healthy": healthy,
        "pid": state.get("pid"),
        "host": state.get("host"),
        "port": state.get("port"),
    }


def collect(all_workspaces: bool, since_hours: float) -> dict:
    from deepresearch.core import workspace as W

    slugs = (
        [w.slug for w in W.list_all(include_archived=False)]
        if all_workspaces
        else [W.current_slug()]
    )
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "since_hours": since_hours,
        "workspaces": [workspace_status(s, since_hours) for s in slugs],
        "dashboard": dashboard_status(),
    }


def _print(d: dict) -> None:
    dash = d["dashboard"]
    if not dash["running"]:
        print("Dashboard: not running")
    else:
        state = "healthy" if dash["healthy"] else "NOT answering"
        print(
            f"Dashboard: {state} on {dash['host']}:{dash['port']} (PID {dash['pid']})"
        )
    for w in d["workspaces"]:
        print(f"\n== Workspace {w['workspace']} ({w['name']})")
        r = w["research"]
        print(f"Research running: {len(r['running'])}")
        for s in r["running"]:
            print(f"  #{s['id']}  {s['age_min']} min  {s['prompt'][:90]}")
        print(f"Finished in the last {d['since_hours']:g} h: {len(r['recent'])}")
        for s in r["recent"]:
            print(f"  #{s['id']}  {s['status']:<10} {s['prompt'][:90]}")
        lab = w["lab"]
        if lab["active"] or lab["waiting"] or lab["recent"]:
            print(
                f"Lab: {len(lab['active'])} on the cluster, "
                f"{len(lab['waiting'])} waiting for review, "
                f"{len(lab['recent'])} finished recently"
            )
            for x in lab["active"] + lab["waiting"] + lab["recent"]:
                title = x["title"] or x["report"]
                extra = f" ({x['outcome']})" if x["outcome"] else ""
                print(f"  run {x['id']}  {x['status']:<10} {title[:70]}{extra}")
                if x["stage"]:
                    print(f"           {x['stage'][:100]}")


def handle(args) -> int:
    from deepresearch.cli.jsonout import emit, json_flag

    d = collect(bool(getattr(args, "all_workspaces", False)), float(args.since))
    if json_flag(args):
        emit(d)
    else:
        _print(d)
    return 0
