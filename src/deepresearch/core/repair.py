"""Repair reports that were saved with only their last part (v0.38.2).

Long Deep Research reports arrive as several `model_output` steps. Before v0.38.2 the
stored report was the SDK's `output_text`, which holds only the last step, so the
first half (sometimes 88%) of long reports was lost. Google keeps interactions for a
limited time; while it still has them, this re-reads the full text and puts it back.

Safety: only rows where the stored report (before any appended follow-ups) is exactly
the last part are changed; follow-ups appended later are kept after the restored
report; synthesized recursive roots (text built locally from child reports) are
never touched; the embedding is cleared so search re-indexes the full text.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Any

FOLLOWUP_MARK = "\n\n---\n### Follow-up ("


def model_parts(interaction: Any) -> list[str]:
    parts = []
    for step in getattr(interaction, "steps", None) or []:
        if getattr(step, "type", None) == "model_output":
            text = "".join(
                getattr(c, "text", "") or ""
                for c in (getattr(step, "content", None) or [])
            )
            if text:
                parts.append(text)
    return parts


def plan_one(stored: str, parts: list[str]) -> dict[str, Any]:
    """What to do with one row: {"action": "repair"|"ok"|"skip", "new": str, ...}."""
    stored = stored or ""
    if len(parts) < 2:
        return {"action": "ok", "why": "single part"}
    cut = stored.find(FOLLOWUP_MARK)
    base, tail = (stored, "") if cut < 0 else (stored[:cut], stored[cut:])
    full = "".join(parts)
    if base.strip() == full.strip():
        return {"action": "ok", "why": "already complete"}
    if base.strip() != parts[-1].strip():
        # A synthesized recursive root (built locally from the main report and its
        # children): the main report that fed the synthesis was itself cut. Only a
        # new synthesis can fix it (repair --resynthesize).
        return {
            "action": "skip",
            "why": "synthesized from a cut report; use --resynthesize",
            "full_main": full,
            "tail": tail,
        }
    return {
        "action": "repair",
        "new": full + tail,
        "before": len(base),
        "after": len(full),
        "kept_followups": bool(tail),
    }


def scan(
    db_path: str, client: Any, ids: list[int] | None = None, workers: int = 8
) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as c:
        q = (
            "SELECT id, interaction_id, result FROM sessions WHERE status='completed' "
            "AND interaction_id IS NOT NULL AND interaction_id != ''"
        )
        rows = c.execute(q).fetchall()
    if ids:
        want = set(ids)
        rows = [r for r in rows if r[0] in want]

    def one(row):
        sid, iid, stored = row
        try:
            parts = model_parts(client.interactions.get(iid))
        except Exception as e:
            msg = str(e)
            gone = "404" in msg or "not found" in msg.lower()
            return {
                "id": sid,
                "action": "gone" if gone else "error",
                "why": "Google no longer has it" if gone else msg[:160],
            }
        return {"id": sid, **plan_one(stored, parts)}

    with ThreadPoolExecutor(workers) as ex:
        return sorted(ex.map(one, rows), key=lambda r: r["id"])


def resynthesize(
    db_path: str, plans: list[dict[str, Any]], agent: Any, log=print
) -> list[int]:
    """Rebuild synthesized recursive reports from their FULL main report plus their
    child reports, deepest first (a nested parent is itself a child of the next level
    up, so it must be rebuilt before its own parent is). One Flash call each."""
    todo = {p["id"]: p for p in plans if p.get("action") == "skip" and "full_main" in p}
    done: list[int] = []
    with sqlite3.connect(db_path, timeout=10) as c:
        depth = {
            sid: (
                c.execute("SELECT depth FROM sessions WHERE id=?", (sid,)).fetchone()
                or [1]
            )[0]
            or 1
            for sid in todo
        }
        for sid in sorted(todo, key=lambda i: -depth[i]):
            p = todo[sid]
            row = c.execute(
                "SELECT prompt FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
            kids = [
                r[0]
                for r in c.execute(
                    "SELECT result FROM sessions WHERE parent_id = ? AND status = "
                    "'completed' AND result IS NOT NULL ORDER BY id",
                    (sid,),
                )
            ]
            if not row or not kids:
                continue
            log(
                f"  #{sid}: re-synthesizing from the full report and {len(kids)} children"
            )
            text = agent.synthesize_findings(row[0], p["full_main"], kids)
            if not (text or "").strip():
                log(f"  #{sid}: synthesis returned nothing; left as it was")
                continue
            c.execute(
                "UPDATE sessions SET result = ?, embedding = NULL WHERE id = ?",
                (text + p.get("tail", ""), sid),
            )
            c.commit()
            done.append(sid)
    return done


def apply(db_path: str, plans: list[dict[str, Any]]) -> int:
    n = 0
    with sqlite3.connect(db_path, timeout=10) as c:
        for p in plans:
            if p.get("action") != "repair":
                continue
            c.execute(
                "UPDATE sessions SET result = ?, embedding = NULL WHERE id = ?",
                (p["new"], p["id"]),
            )
            n += 1
        c.commit()
    return n
