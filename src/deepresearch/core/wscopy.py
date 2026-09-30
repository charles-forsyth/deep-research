"""Copy projects and reports from one workspace into another (v0.42.0).

The source workspace is opened read-only and never changed. Everything lands in the
target inside one transaction: if anything fails, the target is left as it was.

What a copy brings along, with every id remapped to the target's own numbering:
  - projects (title, description, colour, level, Nexus ref, Lab defaults, AI summary)
    and their memberships (reports, data sources, notebooks) with home flags
  - each report with all its sub-reports and follow-ups (the whole tree), session meta
    (stars, tags), token usage, launch meta, highlights and notes (annotations)
  - Lab runs of those reports (plan, script, verdict, pilot rounds, write-up, files) and
    their fetched output folders; their Lab suggestions
  - data sources used by those reports and Lab runs, or members of the projects
    (a same-named source already in the target is reused, never overwritten); the
    source's saved Gemini index is dropped (rebuilt on first use), provenance rows kept
  - notebooks that belong to the copied projects
"Session #N" and "Lab run #N" mentions in copied text (project summaries, notebooks,
Lab write-ups and notes) are rewritten to the new numbers. Audio is not copied (it is
regenerated on demand). Copied Lab runs keep status "completed"/"failed"/"draft"; a run
that was still in flight in the source is copied as a draft so the target never watches
a job it did not submit.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from deepresearch.core import workspace as W

ACTIVE = (
    "planning",
    "submitting",
    "smoke",
    "queued",
    "running",
    "fetching",
    "analyzing",
)


class CopyError(ValueError):
    pass


@dataclass
class CopyPlan:
    projects: list[int] = field(default_factory=list)
    sessions: list[int] = field(default_factory=list)  # roots and descendants
    lab_runs: list[int] = field(default_factory=list)
    sources: list[int] = field(default_factory=list)
    notebooks: list[int] = field(default_factory=list)

    def counts(self) -> dict:
        return {
            "projects": len(self.projects),
            "reports": len(self.sessions),
            "lab_runs": len(self.lab_runs),
            "sources": len(self.sources),
            "notebooks": len(self.notebooks),
        }


def _ro(db: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _has(c: sqlite3.Connection, table: str) -> bool:
    return bool(
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _descendants(c: sqlite3.Connection, roots: list[int]) -> list[int]:
    out: list[int] = []
    frontier = list(roots)
    while frontier:
        marks = ",".join("?" * len(frontier))
        rows = c.execute(
            f"SELECT id FROM sessions WHERE parent_id IN ({marks})", frontier
        ).fetchall()
        frontier = [
            r["id"] for r in rows if r["id"] not in out and r["id"] not in roots
        ]
        out += frontier
    return out


def _root(c: sqlite3.Connection, sid: int) -> int:
    seen = set()
    while True:
        r = c.execute("SELECT parent_id FROM sessions WHERE id=?", (sid,)).fetchone()
        if not r or r["parent_id"] is None or sid in seen:
            return sid
        seen.add(sid)
        sid = int(r["parent_id"])


def plan(
    src_db: str, projects: list[int] | None = None, sessions: list[int] | None = None
) -> CopyPlan:
    """What copying these projects and reports would bring along (read-only)."""
    p = CopyPlan()
    c = _ro(src_db)
    try:
        proj = sorted({int(x) for x in projects or []})
        for pid in proj:
            if not c.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
                raise CopyError(f"project {pid} not found in the source workspace")
        roots: set[int] = set()
        src_ids: set[int] = set()
        nb_ids: set[int] = set()
        if proj and _has(c, "project_items"):
            marks = ",".join("?" * len(proj))
            for r in c.execute(
                f"SELECT kind, ref_id FROM project_items WHERE project_id IN ({marks})",
                proj,
            ):
                if r["kind"] == "session":
                    roots.add(int(r["ref_id"]))
                elif r["kind"] == "source":
                    src_ids.add(int(r["ref_id"]))
                elif r["kind"] == "notebook":
                    nb_ids.add(int(r["ref_id"]))
        for sid in sessions or []:
            if not c.execute(
                "SELECT 1 FROM sessions WHERE id=?", (int(sid),)
            ).fetchone():
                raise CopyError(f"report {sid} not found in the source workspace")
            roots.add(_root(c, int(sid)))
        roots_l = sorted(roots)
        all_s = sorted(set(roots_l) | set(_descendants(c, roots_l)))
        runs: list[int] = []
        if all_s and _has(c, "lab_runs"):
            marks = ",".join("?" * len(all_s))
            runs = [
                r["id"]
                for r in c.execute(
                    f"SELECT id FROM lab_runs WHERE session_id IN ({marks}) ORDER BY id",
                    all_s,
                )
            ]
        if _has(c, "data_source_uses"):
            for kind, ids in (("session", all_s), ("lab_run", runs)):
                if ids:
                    marks = ",".join("?" * len(ids))
                    for r in c.execute(
                        f"SELECT source_id FROM data_source_uses WHERE used_by_kind=? "
                        f"AND used_by_id IN ({marks})",
                        [kind, *ids],
                    ):
                        src_ids.add(int(r["source_id"]))
        if runs:
            marks = ",".join("?" * len(runs))
            names: set[str] = set()
            for r in c.execute(
                f"SELECT data_sources FROM lab_runs WHERE id IN ({marks})", runs
            ):
                try:
                    names |= set(json.loads(r["data_sources"] or "[]"))
                except ValueError:
                    pass
            for n in names:
                row = c.execute(
                    "SELECT id FROM data_sources WHERE name=?", (n,)
                ).fetchone()
                if row:
                    src_ids.add(int(row["id"]))
        if _has(c, "data_sources") and src_ids:
            marks = ",".join("?" * len(src_ids))
            src_ids = {
                int(r["id"])
                for r in c.execute(
                    f"SELECT id FROM data_sources WHERE id IN ({marks})", list(src_ids)
                )
            }
        p.projects, p.sessions, p.lab_runs = proj, all_s, runs
        p.sources, p.notebooks = sorted(src_ids), sorted(nb_ids)
        return p
    finally:
        c.close()


def _cols(c: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


def _insert(dst: sqlite3.Connection, table: str, row: dict, skip=("id",)) -> int:
    cols = [k for k in _cols(dst, table) if k in row and k not in skip]
    cur = dst.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [row[k] for k in cols],
    )
    return int(cur.lastrowid or 0)


def _remap_text(
    text: str | None, smap: dict[int, int], lmap: dict[int, int]
) -> str | None:
    if not text:
        return text

    def s(m: re.Match) -> str:
        n = int(m.group(2))
        return f"{m.group(1)}{smap.get(n, n)}" if n in smap else m.group(0)

    def lab(m: re.Match) -> str:
        n = int(m.group(2))
        return f"{m.group(1)}{lmap.get(n, n)}" if n in lmap else m.group(0)

    text = re.sub(r"(Session #)(\d+)", s, text)
    text = re.sub(r"(Lab run #)(\d+)", lab, text)
    return text


def _ensure_schema(db: str) -> None:
    """Every table the dashboard creates, so the target can take any row."""
    from deepresearch.dashboard.features import Features
    from deepresearch.dashboard.lab import Lab
    from deepresearch.dashboard.projects import ProjectStore
    from deepresearch.dashboard.store import DashboardStore
    from deepresearch.sources.registry import SourceRegistry

    DashboardStore(db)
    SourceRegistry(db)
    ProjectStore(db)
    Features(db, lambda: None, Path(db).parent / "audio")
    Lab(db, lambda: None, W.base_dir(), targets={}, results_dir=Path(db).parent / "lab")
    with sqlite3.connect(db) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS run_meta (session_id INTEGER PRIMARY KEY, depth "
            "INTEGER, breadth INTEGER, estimate_usd REAL, rerun_of INTEGER, launched_at TEXT)"
        )


def copy(src_slug: str, dst_slug: str, projects: list[int] | None = None,
         sessions: list[int] | None = None) -> dict:  # fmt: skip
    """Copy projects and/or reports from workspace `src_slug` into `dst_slug`."""
    if src_slug == dst_slug:
        raise CopyError("pick a different workspace to copy into")
    src, dst = W.get(src_slug), W.get(dst_slug)
    if dst.archived:
        raise CopyError(f"workspace {dst.slug!r} is archived")
    if not projects and not sessions:
        raise CopyError("choose at least one project or report to copy")
    cp = plan(src.db_path, projects, sessions)
    _ensure_schema(dst.db_path)
    s = _ro(src.db_path)
    d = sqlite3.connect(dst.db_path, timeout=30)
    d.row_factory = sqlite3.Row
    smap: dict[int, int] = {}
    lmap: dict[int, int] = {}
    srcmap: dict[int, int] = {}
    pmap: dict[int, int] = {}
    nbmap: dict[int, int] = {}
    reused: list[str] = []
    copied_dirs: list[tuple[Path, Path]] = []
    try:
        d.execute("BEGIN IMMEDIATE")
        # data sources: reuse a same-named one in the target, else copy (index dropped)
        for sid in cp.sources:
            r = dict(
                s.execute("SELECT * FROM data_sources WHERE id=?", (sid,)).fetchone()
            )
            have = d.execute(
                "SELECT id FROM data_sources WHERE name=?", (r["name"],)
            ).fetchone()
            if have:
                srcmap[sid] = int(have["id"])
                reused.append(r["name"])
                continue
            opts = json.loads(r.get("options") or "{}")
            for k in ("store", "store_hash", "db_path"):
                opts.pop(k, None)
            r["options"] = json.dumps(opts)
            srcmap[sid] = _insert(d, "data_sources", r)
        # sessions: parents before children (sorted by depth, then id)
        rows = []
        if cp.sessions:
            marks = ",".join("?" * len(cp.sessions))
            rows = [
                dict(x)
                for x in s.execute(
                    f"SELECT * FROM sessions WHERE id IN ({marks}) "
                    "ORDER BY COALESCE(depth, 1), id",
                    cp.sessions,
                )
            ]
        pending = rows
        guard = 0
        while pending and guard < 50:
            guard += 1
            later = []
            for r in pending:
                par = r.get("parent_id")
                if par is not None and par in cp.sessions and par not in smap:
                    later.append(r)
                    continue
                old = r["id"]
                r["parent_id"] = smap.get(par) if par is not None else None
                r["pid"] = (
                    None  # a process id from another workspace means nothing here
                )
                if r.get("status") == "running":
                    r["status"] = "crashed"
                smap[old] = _insert(d, "sessions", r)
            pending = later
        if pending:
            raise CopyError("could not order the report tree (parent loop)")
        for table in ("session_meta", "session_usage"):
            if _has(s, table):
                for old, new in smap.items():
                    r = s.execute(
                        f"SELECT * FROM {table} WHERE session_id=?", (old,)
                    ).fetchone()
                    if r:
                        row = dict(r)
                        row["session_id"] = new
                        _insert(d, table, row, skip=())
        if _has(s, "run_meta"):
            for old, new in smap.items():
                r = s.execute(
                    "SELECT * FROM run_meta WHERE session_id=?", (old,)
                ).fetchone()
                if r:
                    row = dict(r)
                    row["session_id"] = new
                    ro = row.get("rerun_of")
                    row["rerun_of"] = smap.get(int(ro)) if ro is not None else None
                    _insert(d, "run_meta", row, skip=())
        # Lab runs (rerun_of fixed in a second pass once every id is known)
        for rid in cp.lab_runs:
            r = dict(s.execute("SELECT * FROM lab_runs WHERE id=?", (rid,)).fetchone())
            r["session_id"] = smap[r["session_id"]]
            if r.get("status") in ACTIVE:
                r["status"] = "draft"
                r["stage"] = (
                    "Copied from another workspace while it was running; review, then submit"
                )
                r["job_id"] = None
                r["slurm_state"] = None
            r["rerun_of"] = None
            lmap[rid] = _insert(d, "lab_runs", r)
        for rid in cp.lab_runs:
            ro = s.execute(
                "SELECT rerun_of FROM lab_runs WHERE id=?", (rid,)
            ).fetchone()["rerun_of"]
            if ro and ro in lmap:
                d.execute(
                    "UPDATE lab_runs SET rerun_of=? WHERE id=?", (lmap[ro], lmap[rid])
                )
        if _has(s, "lab_suggestions"):
            for old, new in smap.items():
                r = s.execute(
                    "SELECT * FROM lab_suggestions WHERE session_id=?", (old,)
                ).fetchone()
                if r:
                    row = dict(r)
                    row["session_id"] = new
                    d.execute("DELETE FROM lab_suggestions WHERE session_id=?", (new,))
                    _insert(d, "lab_suggestions", row, skip=())
        # annotations (after Lab runs so "Lab run #N (" notes can be renumbered)
        if _has(s, "annotations") and smap:
            marks = ",".join("?" * len(smap))
            for r in s.execute(
                f"SELECT * FROM annotations WHERE session_id IN ({marks}) ORDER BY id",
                list(smap),
            ):
                row = dict(r)
                row["session_id"] = smap[row["session_id"]]
                row["note"] = _remap_text(row.get("note"), smap, lmap)
                _insert(d, "annotations", row)
        # Lab write-ups mention runs and reports
        for old, new in lmap.items():
            r = d.execute(
                "SELECT result_md, plan FROM lab_runs WHERE id=?", (new,)
            ).fetchone()
            d.execute(
                "UPDATE lab_runs SET result_md=?, plan=? WHERE id=?",
                (
                    _remap_text(r["result_md"], smap, lmap),
                    _remap_plan(r["plan"], lmap),
                    new,
                ),
            )
        # provenance rows
        if _has(s, "data_source_uses"):
            for kind, mp in (("session", smap), ("lab_run", lmap)):
                if not mp:
                    continue
                marks = ",".join("?" * len(mp))
                for r in s.execute(
                    f"SELECT * FROM data_source_uses WHERE used_by_kind=? AND used_by_id IN ({marks})",
                    [kind, *mp],
                ):
                    row = dict(r)
                    if row["source_id"] not in srcmap:
                        continue
                    row["source_id"] = srcmap[row["source_id"]]
                    row["used_by_id"] = mp[row["used_by_id"]]
                    _insert(d, "data_source_uses", row)
        # notebooks
        for nid in cp.notebooks:
            r = s.execute("SELECT * FROM notebooks WHERE id=?", (nid,)).fetchone()
            if r:
                row = dict(r)
                row["content"] = _remap_text(row.get("content"), smap, lmap)
                nbmap[nid] = _insert(d, "notebooks", row)
        # projects and memberships
        for pid in cp.projects:
            row = dict(
                s.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
            )
            row["summary"] = _remap_text(row.get("summary"), smap, lmap)
            pmap[pid] = _insert(d, "projects", row)
            for it in s.execute(
                "SELECT * FROM project_items WHERE project_id=?", (pid,)
            ):
                it = dict(it)
                mp = {"session": smap, "source": srcmap, "notebook": nbmap}.get(
                    it["kind"], {}
                )
                if it["ref_id"] not in mp:
                    continue
                it["project_id"] = pmap[pid]
                it["ref_id"] = mp[it["ref_id"]]
                d.execute(
                    "INSERT OR IGNORE INTO project_items (project_id, kind, ref_id, is_home, added_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        it["project_id"],
                        it["kind"],
                        it["ref_id"],
                        it["is_home"],
                        it.get("added_at"),
                    ),
                )
        # Lab output folders: copy before commit; on failure they are removed again
        for old, new in lmap.items():
            a = src.lab_dir / f"run_{old}"
            b = dst.lab_dir / f"run_{new}"
            if a.is_dir():
                if b.exists():
                    shutil.move(str(b), str(b) + f".prev-{new}")
                shutil.copytree(a, b, symlinks=False)
                copied_dirs.append((a, b))
        d.commit()
    except Exception:
        d.rollback()
        for _, b in copied_dirs:
            shutil.rmtree(b, ignore_errors=True)
        raise
    finally:
        d.close()
        s.close()
    return {
        "from": src.slug,
        "to": dst.slug,
        "counts": cp.counts(),
        "projects": pmap,
        "reports": smap,
        "lab_runs": lmap,
        "sources": srcmap,
        "notebooks": nbmap,
        "sources_reused": reused,
    }


def _remap_plan(plan_json: str | None, lmap: dict[int, int]) -> str | None:
    """auto_replan_of inside a copied plan points at the new run numbers."""
    if not plan_json:
        return plan_json
    try:
        p = json.loads(plan_json)
    except ValueError:
        return plan_json
    if isinstance(p, dict) and p.get("auto_replan_of") in lmap:
        p["auto_replan_of"] = lmap[p["auto_replan_of"]]
        return json.dumps(p)
    return plan_json
