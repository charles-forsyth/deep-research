"""The Lab's Cluster view (v0.57.0): cluster facts through bifrost, cached per panel.

Each panel is one bifrost read tool. Answers are cached for the panel's TTL and refreshed
in the background (stale-while-revalidate), so the page never waits on a slow read
(storage_usage takes ~100 s) more than once, and many open tabs cost one call per TTL.
The `lab` summary is local: the workspace's own Lab runs, never a cluster call.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

# name -> (bifrost tool, arguments, seconds before a background refresh)
PANELS: dict[str, tuple[str, dict, int]] = {
    "now": ("cluster_status", {}, 30),
    "jobs": ("jobs_list", {"since": "now-14days", "limit": 200}, 60),
    "usage": ("my_usage", {"since": "now-30days", "group_by": "partition"}, 600),
    "waste": ("waste_report", {"since": "now-7days"}, 600),
    "storage": ("storage_usage", {}, 1800),
}
FIRST_WAIT_S = 8.0  # the first ever load waits this long, then the page polls


class ClusterView:
    def __init__(self, now: Callable[[], float] = time.time):
        self._now = now
        self._cache: dict[str, dict] = {}
        self._busy: set[str] = set()
        self._lock = threading.Lock()

    def get(self, name: str, client: Any, wait: float = FIRST_WAIT_S) -> dict:
        """The panel's latest answer: {data, error, fetched_at, age_s, refreshing}, or
        {loading: true} while the first read is still running, or {signed_in: false}."""
        if name not in PANELS:
            raise KeyError(name)
        if client is None:
            return {"signed_in": False}
        _, _, ttl = PANELS[name]
        with self._lock:
            e = self._cache.get(name)
            fresh = e is not None and self._now() - e["at"] < ttl
            start = not fresh and name not in self._busy
            if start:
                self._busy.add(name)
        if start:
            t = threading.Thread(
                target=self._fetch, args=(name, client), daemon=True, name=f"clv-{name}"
            )
            t.start()
            if e is None and wait > 0:
                t.join(wait)
        with self._lock:
            e = self._cache.get(name)
            busy = name in self._busy
        if e is None:
            return {"loading": True}
        return {
            "data": e["data"],
            "error": e.get("error"),
            "fetched_at": datetime.fromtimestamp(e["at"], timezone.utc).isoformat(),
            "age_s": int(self._now() - e["at"]),
            "refreshing": busy,
        }

    def _fetch(self, name: str, client: Any) -> None:
        tool, args, _ = PANELS[name]
        try:
            data = client.call(tool, dict(args))
            entry: dict = {"at": self._now(), "data": data}
        except Exception as ex:  # noqa: BLE001  shown on the panel; old data is kept
            with self._lock:
                old = self._cache.get(name)
            entry = {
                "at": self._now(),
                "data": old["data"] if old else None,
                "error": str(ex)[:300],
            }
        finally:
            with self._lock:
                self._busy.discard(name)
        with self._lock:
            self._cache[name] = entry


VIEW = ClusterView()

LIVE = ("planning", "submitting", "smoke", "queued", "running", "fetching", "analyzing")


def lab_summary(runs: list[dict], days: int = 30, now: datetime | None = None) -> dict:
    """The workspace's Lab runs for the Cluster view: counts, outcomes, worst-case and AI
    spend over `days`, and every Slurm job id the Lab started (job -> run), so the
    cluster's job list can say which jobs are Lab runs."""
    now = now or datetime.now()
    since = now - timedelta(days=days)
    status: dict[str, int] = {}
    outcome: dict[str, int] = {}
    per_day: dict[str, dict[str, int]] = {}
    estimate = ai = 0.0
    n = 0
    jobs: dict[str, dict] = {}
    live = []
    for r in runs:
        plan = r.get("plan") if isinstance(r.get("plan"), dict) else {}
        title = str((plan or {}).get("title") or "")
        ref = {"run_id": r.get("id"), "session_id": r.get("session_id"), "title": title}
        for j in r.get("cluster_jobs") or []:
            if isinstance(j, dict) and j.get("job_id"):
                jobs[str(j["job_id"])] = {**ref, "why": str(j.get("why") or "")}
        jid = str(r.get("job_id") or "")
        if jid.isdigit() and jid not in jobs:
            jobs[jid] = {**ref, "why": "full"}
        sm = r.get("smoke") if isinstance(r.get("smoke"), dict) else None
        pj = str((sm or {}).get("job_id") or "")
        if pj and pj not in jobs:
            jobs[pj] = {**ref, "why": "pilot"}
        if r.get("status") in LIVE:
            live.append(
                {
                    **ref,
                    "status": r.get("status"),
                    "stage": r.get("stage"),
                    "job_id": jid,
                }
            )
        try:
            created = datetime.fromisoformat(str(r.get("created_at") or ""))
        except ValueError:
            continue
        if created.tzinfo is not None:
            created = created.astimezone().replace(tzinfo=None)
        if created < since:
            continue
        n += 1
        st = str(r.get("status") or "")
        status[st] = status.get(st, 0) + 1
        oc = (r.get("assessment") or {}).get("outcome")
        if oc:
            outcome[oc] = outcome.get(oc, 0) + 1
        day = created.strftime("%Y-%m-%d")
        bucket = per_day.setdefault(day, {})
        key = st if st in ("completed", "failed") else "other"
        bucket[key] = bucket.get(key, 0) + 1
        if st not in ("draft", "plan_failed", "cancelled") or r.get("submitted_at"):
            estimate += float(r.get("estimate_usd") or 0)
        ai += float(r.get("ai_cost_usd") or 0)
    return {
        "days": days,
        "runs": n,
        "status": status,
        "outcome": outcome,
        "per_day": per_day,
        "estimate_usd": round(estimate, 2),
        "ai_usd": round(ai, 2),
        "jobs": jobs,
        "live": live,
    }
