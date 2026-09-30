"""Claims board (v0.45.0): what a project's reports claim, and what its Lab runs showed.

Deterministic, no model calls. Each finished Lab run tested one question drawn from a
report; its verdict checks say which parts held. A claim here is one Lab run's question,
with:

- outcome: the run's assessed outcome (confirmed / refuted / inconclusive / broken)
- evidence: the claim checks (and the validation / informative checks that make the
  result trustworthy), each with expected vs got
- report: the session it came from, and the Lab run id to open

When the same report question was tested more than once (reruns, re-plans), the runs are
grouped under one claim and the newest meaningful outcome leads: confirmed/refuted beat
inconclusive, which beats broken, so a broken first attempt does not hide a later result.
Drafts and runs still in progress are listed as "pending" with their stage.
"""

from __future__ import annotations

import re
from typing import Any

from deepresearch.dashboard.labverdict import assess

RANK = {"confirmed": 4, "refuted": 4, "inconclusive": 2, "broken": 1, "pending": 0}
# a draft or a plan being written is not a test yet; it shows up once submitted
ACTIVE = ("submitting", "smoke", "queued", "running", "fetching", "analyzing")
NOT_YET = ("planning", "draft", "cancelled", "plan_failed")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _outcome(run: dict) -> dict[str, Any]:
    status = str(run.get("status") or "")
    if status in ACTIVE:
        return {"outcome": "pending", "why": run.get("stage") or status, "groups": {}}
    if status in NOT_YET:
        return {"outcome": "none", "why": run.get("stage") or status, "groups": {}}
    v = run.get("verdict")
    a = run.get("assessment") or assess(v if isinstance(v, dict) else None, status)
    return a or {"outcome": "none", "why": "No checks were recorded.", "groups": {}}


def _checks(a: dict) -> list[dict]:
    out = []
    for kind in ("claim", "validation", "informative"):
        for c in (a.get("groups") or {}).get(kind) or []:
            out.append(
                {
                    "kind": kind,
                    "name": str(c.get("name") or "")[:120],
                    "expected": c.get("expected"),
                    "got": c.get("got"),
                    "pass": c.get("pass"),
                }
            )
    return out


def board(lab_runs: list[dict], sessions: dict[int, dict] | None = None) -> dict:
    """The claims board for a project's Lab runs (newest first in `lab_runs` or not)."""
    sessions = sessions or {}
    groups: dict[tuple[int, str], dict] = {}
    for r in sorted(lab_runs, key=lambda x: int(x.get("id") or 0)):
        plan = r.get("plan") or {}
        q = str(
            plan.get("question") or plan.get("title") or r.get("request") or ""
        ).strip()
        if not q:
            continue
        # reruns keep the question; re-plans may reword it, so group on rerun links too
        root = r.get("rerun_of")
        key = None
        if root:
            for k, g in groups.items():
                if int(root) in g["run_ids"]:
                    key = k
                    break
        if key is None:
            key = (int(r.get("session_id") or 0), _norm(q)[:160])
        a = _outcome(r)
        if a["outcome"] == "none":
            continue
        g = groups.setdefault(
            key,
            {
                "question": q,
                "title": str(plan.get("title") or "")[:160],
                "session_id": r.get("session_id"),
                "report": str(
                    (sessions.get(int(r.get("session_id") or 0)) or {}).get("prompt")
                    or ""
                )[:160],
                "run_ids": [],
                "attempts": [],
                "lead": None,
            },
        )
        g["run_ids"].append(int(r["id"]))
        att = {
            "run_id": int(r["id"]),
            "status": r.get("status"),
            "outcome": a["outcome"],
            "why": str(a.get("why") or "")[:400],
            "checks": _checks(a),
            "finished_at": r.get("finished_at"),
        }
        g["attempts"].append(att)
        lead = g["lead"]
        # best outcome leads, the newest among equals (runs are visited oldest first);
        # a new attempt in progress outranks earlier broken ones: it is the live try
        if (
            lead is None
            or RANK[att["outcome"]] >= RANK[lead["outcome"]]
            or (att["outcome"] == "pending" and lead["outcome"] == "broken")
        ):
            g["lead"] = att
    claims = []
    for g in groups.values():
        lead = g["lead"]
        claims.append(
            {
                "question": g["question"],
                "title": g["title"],
                "session_id": g["session_id"],
                "report": g["report"],
                "outcome": lead["outcome"],
                "why": lead["why"],
                "run_id": lead["run_id"],
                "checks": lead["checks"],
                "attempts": [
                    {k: a[k] for k in ("run_id", "outcome", "status")}
                    for a in reversed(g["attempts"])
                ],
            }
        )
    order = {"refuted": 0, "confirmed": 1, "inconclusive": 2, "pending": 3, "broken": 4}
    claims.sort(key=lambda c: (order.get(c["outcome"], 9), -int(c["run_id"])))
    counts = {k: sum(1 for c in claims if c["outcome"] == k) for k in order}
    return {"claims": claims, "counts": counts, "total": len(claims)}


def board_md(b: dict) -> str:
    """The board as Markdown, for the dossier and the research package."""
    if not b.get("claims"):
        return ""
    lines = ["## Claims tested by Lab runs", ""]
    c = b["counts"]
    lines.append(
        f"{c['confirmed']} confirmed, {c['refuted']} refuted, {c['inconclusive']} "
        f"inconclusive, {c['pending']} pending, {c['broken']} broken."
    )
    lines.append("")
    for cl in b["claims"]:
        lines.append(
            f"- **{cl['outcome'].upper()}**: {cl['question']} (Lab run #{cl['run_id']}, report #{cl['session_id']})"
        )
        if cl["why"]:
            lines.append(f"  - {cl['why']}")
        for ch in cl["checks"]:
            if ch["kind"] != "claim":
                continue
            mark = "pass" if ch["pass"] else "fail" if ch["pass"] is False else "?"
            lines.append(
                f"  - {ch['name']}: expected {ch['expected']}, got {ch['got']} ({mark})"
            )
    lines.append("")
    return "\n".join(lines)
