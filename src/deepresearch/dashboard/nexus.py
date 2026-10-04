"""Nexus reads for projects (v0.62.0): the project picker and "From Nexus" line.

Signs in as the pre-registered program client `nexus-deep-research` (max role read) with
the same stdlib MCP client as bifrost. Reads only search, labs/grants/gcp/projects show;
never dossier, tree or interactions (interaction text is private).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from deepresearch.dashboard import bifrost as bf

DEFAULT_URL = "https://nexus-mcp-server-492106370716.us-central1.run.app"
CLIENT_ID = "nexus-deep-research"
TOKEN_FILE = "nexus-token.json"

KINDS = {  # Nexus entity type -> (show tool, argument name)
    "lab": ("nexus_labs_show", "name"),
    "grant": ("nexus_grants_show", "c_number"),
    "gcp": ("nexus_gcp_show", "project_id"),
    "project": ("nexus_projects_show", "name"),
}
CACHE_S = 600


def client(state_dir: Path, url: str | None = None) -> bf.BifrostClient:
    return bf.BifrostClient(
        state_dir,
        url=url or os.getenv("DR_NEXUS_URL", DEFAULT_URL),
        client_id=CLIENT_ID,
        token_file=TOKEN_FILE,
        label="nexus",
        timeout=30.0,
    )


def _as_obj(x: Any) -> Any:
    if isinstance(x, str):
        try:
            return json.loads(x)
        except ValueError:
            return x
    return x


# Nexus search `_type` -> our kind. Researcher and Interaction rows are dropped:
# a project links to a lab, grant or GCP/research project, and interaction text is private.
TYPES = {"Lab": "lab", "Grant": "grant", "GCPProject": "gcp", "GcpProject": "gcp",
         "ResearchProject": "project"}  # fmt: skip


def search(c: bf.BifrostClient, q: str, limit: int = 12) -> list[dict]:
    """Labs, grants, GCP projects and research projects matching `q`:
    [{kind, id, name, sub}]; `id` is what the matching *_show tool takes."""
    rows = _as_obj(c.call("nexus_search", {"term": q, "limit": 25}))
    out: list[dict] = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        kind = TYPES.get(str(r.get("_type") or ""))
        if not kind:
            continue
        name = str(r.get("name") or r.get("title") or "").strip()
        rid = str(r.get("c_number") or r.get("project_id") or name).strip()
        if kind == "gcp":
            rid = str(r.get("project_id") or name).strip()
        if not rid:
            continue
        sub = (
            r.get("description")
            or r.get("summary")
            or r.get("title")
            or r.get("agency")
            or ""
        )
        if kind == "grant" and r.get("title"):
            name = f"{r.get('title')} ({rid})"
        out.append(
            {"kind": kind, "id": rid, "name": name or rid, "sub": str(sub)[:160]}
        )
        if len(out) >= limit:
            break
    return out


_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def show(c: bf.BifrostClient, kind: str, rid: str) -> dict:
    """The facts a project page shows for one linked Nexus entity, cached 10 min:
    {kind, id, name, lines: [str]} (PI, sponsor, dates, links), never interaction text."""
    if kind not in KINDS:
        raise ValueError(f"unknown Nexus kind {kind!r}")
    key = (c.url, kind, rid)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
    tool, arg = KINDS[kind]
    raw = _as_obj(c.call(tool, {arg: rid}))
    out = summarize(kind, rid, raw if isinstance(raw, dict) else {"text": str(raw)})
    with _cache_lock:
        _cache[key] = (time.time(), out)
    return out


def summarize(kind: str, rid: str, d: dict) -> dict:
    """Public facts only: name, description/status, PI or lead, member count and the
    linked grants / GCP and research projects. Interaction neighbours are skipped."""
    ent = d.get("unit") or d.get("grant") or d.get("project") or d.get("gcp_project")
    if not isinstance(ent, dict):
        ent = d.get("entity") if isinstance(d.get("entity"), dict) else d
    name = str(ent.get("name") or ent.get("title") or rid)
    lines: list[str] = []
    for label, keys in (
        ("About", ("description", "summary")),
        ("Title", ("title",) if kind == "grant" else ()),
        ("Sponsor", ("agency", "sponsor")),
        ("Status", ("status",)),
        ("Starts", ("start_date",)),
        ("Ends", ("end_date",)),
        ("GCP project", ("project_id",) if kind == "gcp" else ()),
    ):
        for k in keys:
            v = ent.get(k)
            if v not in (None, "", [], {}) and str(v) != name:
                lines.append(f"{label}: {str(v)[:200]}")
                break
    leads, members, linked = [], 0, []
    for cn in d.get("connections") or []:
        if not isinstance(cn, dict):
            continue
        via = str((cn.get("via") or {}).get("type") or "")
        nb = cn.get("neighbor") or {}
        t = str(nb.get("_type") or "")
        disp = str(nb.get("display") or nb.get("name") or "")
        # allow-list: only people and linkable entities; Interaction and Task
        # neighbours (private text) fall through every branch below
        if not disp:
            continue
        if t == "Researcher" and via in ("PI_OF", "LEADS", "PI", "OWNS"):
            if disp not in leads:
                leads.append(disp)
        elif t == "Researcher" and via == "MEMBER_OF":
            members += 1
        elif t in TYPES:
            linked.append(disp)
    by_type = d.get("connections_by_type") or {}
    members = int(by_type.get("MEMBER_OF", members) or 0)
    if leads:
        lines.insert(
            0, ("PI: " if kind in ("lab", "grant") else "Lead: ") + ", ".join(leads[:3])
        )
    if members:
        lines.append(f"Members: {members}")
    if linked:
        lines.append(
            "Linked: " + ", ".join(linked[:6]) + (" ..." if len(linked) > 6 else "")
        )
    return {"kind": kind, "id": rid, "name": name, "lines": lines[:8]}
