"""Provenance: what a report, answer or Lab result was built from, and a fingerprint.

The fingerprint is a SHA-256 over a canonical JSON of the inputs that decide what a run
could see: the prompt, uploaded file names and sizes, File Search Store names, data
sources with the manifest hash they had when used, and (for Lab runs) the script,
install list and resources. Two runs with the same fingerprint had the same inputs; a
changed data source (new manifest hash) changes the fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def fingerprint(inputs: dict[str, Any]) -> str:
    raw = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _sources_used(db_path: str, kind: str, ident: int) -> list[dict[str, Any]]:
    try:
        with sqlite3.connect(db_path, timeout=10) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT s.name, s.kind, s.uri, u.manifest_hash, u.created_at "
                "FROM data_source_uses u JOIN data_sources s ON s.id = u.source_id "
                "WHERE u.used_by_kind = ? AND u.used_by_id = ? ORDER BY u.id",
                (kind, ident),
            ).fetchall()
    except sqlite3.OperationalError:  # registry tables not created yet
        return []
    seen: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        seen[(r["name"], r["manifest_hash"])] = {
            "name": r["name"],
            "kind": r["kind"],
            "uri": r["uri"],
            "manifest_hash": r["manifest_hash"],
            "used_at": r["created_at"],
        }
    return list(seen.values())


def session_provenance(db_path: str, session: dict[str, Any]) -> dict[str, Any]:
    """Provenance for a research session (report and its follow-ups)."""
    files = session.get("files")
    if isinstance(files, str):
        try:
            files = json.loads(files or "[]")
        except ValueError:
            files = []
    uploads = []
    for f in files or []:
        p = Path(str(f))
        uploads.append(
            {"name": p.name, "bytes": p.stat().st_size if p.is_file() else None}
        )
    sources = _sources_used(db_path, "session", int(session["id"]))
    inputs = {
        "prompt": session.get("prompt") or "",
        "uploads": uploads,
        "sources": [
            {"name": s["name"], "uri": s["uri"], "manifest_hash": s["manifest_hash"]}
            for s in sources
        ],
    }
    return {
        "fingerprint": fingerprint(inputs),
        "uploads": uploads,
        "sources": sources,
        "interaction_id": session.get("interaction_id"),
    }


def lab_provenance(db_path: str, run: dict[str, Any]) -> dict[str, Any]:
    plan = run.get("plan") or {}
    sources = _sources_used(db_path, "lab_run", int(run["id"]))
    inputs = {
        "script": plan.get("script") or "",
        "install": plan.get("install") or {},
        "resources": plan.get("resources") or {},
        "parameters": plan.get("parameters") or {},
        "sources": [
            {"name": s["name"], "uri": s["uri"], "manifest_hash": s["manifest_hash"]}
            for s in sources
        ],
    }
    return {"fingerprint": fingerprint(inputs), "sources": sources}
