"""Projects: a container above reports, data sources, notebooks and Lab runs.

A project is how a researcher already thinks about work: one per grant, paper,
proposal or thesis. It remembers defaults (data sources, Lab target and partition,
protection level) that new research, Ask and Lab runs start with.

Filing rules (docs/SPEC.md section 22):
- Top-level reports, data sources and notebooks are filed by hand.
- Follow-ups live inside their report; sub-reports (children) follow their root
  report; Lab runs and highlights follow their report. None of these are filed.
- A report can sit in several projects. Exactly one is its *home*, which supplies
  its defaults; the others only link to it.
- The Inbox is not a project: it is the set of top-level reports in no project.
- The protection level is a label, shown and never enforced. A project shows the
  strictest of its own level and its data sources' levels.
- A Nexus grant or lab id is stored as plain text. Nothing here writes to Nexus.
"""

from __future__ import annotations

import csv
import io
import json
import math
import operator
import re
import sqlite3
import zipfile
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

KINDS = ("session", "source", "notebook")
COLORS = ("cyan", "amber", "magenta", "green", "red")
LEVELS = ("P1", "P2", "P3", "P4")
EDITABLE = (
    "title",
    "description",
    "color",
    "protection_level",
    "nexus_ref",
    "lab_target",
    "lab_partition",
    "archived",
)

# gemini-3.8-flash rates, USD per 1M tokens (same as features.py)
FLASH_IN_1M = 0.75
FLASH_OUT_1M = 3.75

# Prompt budgets: a project can hold hundreds of reports, so each gets a share.
SUMMARY_TOTAL_CHARS = 400_000
SUMMARY_PER_REPORT_MIN = 4_000
ASK_TOP_REPORTS = 6
ASK_REPORT_CHARS = 40_000


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _loads(v: Any, default: Any) -> Any:
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def strictest(levels: list[str]) -> str:
    known = [lv for lv in levels if lv in LEVELS]
    return max(known, key=LEVELS.index) if known else "P2"


def flash_cost(resp: Any) -> float | None:
    u = getattr(resp, "usage_metadata", None)
    if not u:
        return None
    out = (getattr(u, "candidates_token_count", 0) or 0) + (
        getattr(u, "thoughts_token_count", 0) or 0
    )  # thinking is billed as output
    return round(
        (getattr(u, "prompt_token_count", 0) or 0) / 1e6 * FLASH_IN_1M
        + out / 1e6 * FLASH_OUT_1M,
        4,
    )


def strip_sources(md: str) -> str:
    """Drop the trailing numbered source list (links, not prose)."""
    m = re.search(r"\n\*\*Sources:?\*\*\s*\n", md or "")
    if m:
        return md[: m.start()]
    m = re.search(r"\n#+\s*Sources\s*\n", md or "")
    return md[: m.start()] if m else (md or "")


class ProjectStore:
    """Projects and their memberships, in the same SQLite file as the history."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    color TEXT NOT NULL DEFAULT 'cyan',
                    protection_level TEXT NOT NULL DEFAULT 'P2',
                    nexus_ref TEXT NOT NULL DEFAULT '',
                    lab_target TEXT NOT NULL DEFAULT '',
                    lab_partition TEXT NOT NULL DEFAULT '',
                    archived INTEGER NOT NULL DEFAULT 0,
                    summary TEXT,
                    summary_at TEXT,
                    summary_cost REAL,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS project_items (
                    project_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    ref_id INTEGER NOT NULL,
                    is_home INTEGER NOT NULL DEFAULT 0,
                    added_at TEXT,
                    PRIMARY KEY (project_id, kind, ref_id)
                );
                CREATE INDEX IF NOT EXISTS idx_project_items_ref
                    ON project_items(kind, ref_id);
                """
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _has_sessions(self, conn: sqlite3.Connection) -> bool:
        return bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
            ).fetchone()
        )

    # ---- validation -------------------------------------------------------

    def _clean(self, fields: dict, current_id: int | None = None) -> dict:
        out: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in EDITABLE or v is None:
                continue
            if k == "title":
                v = " ".join(str(v).split())[:120]
                if not v:
                    raise ValueError("A project needs a title")
                with self._conn() as conn:
                    clash = conn.execute(
                        "SELECT id FROM projects WHERE lower(title) = lower(?) "
                        "AND archived = 0 AND id != ?",
                        (v, current_id or -1),
                    ).fetchone()
                if clash:
                    raise ValueError(f"A project called '{v}' already exists")
            elif k == "description":
                v = str(v).strip()[:4000]
            elif k == "color":
                if v not in COLORS:
                    raise ValueError(f"color must be one of {', '.join(COLORS)}")
            elif k == "protection_level":
                v = str(v).upper()
                if v not in LEVELS:
                    raise ValueError(
                        f"protection level must be one of {', '.join(LEVELS)}"
                    )
            elif k == "nexus_ref":
                v = str(v).strip()[:120]
            elif k in ("lab_target", "lab_partition"):
                v = str(v).strip()
                if v and not re.fullmatch(r"[\w.-]{1,64}", v):
                    raise ValueError(f"{k} has characters it cannot contain")
            elif k == "archived":
                v = 1 if v else 0
            out[k] = v
        return out

    # ---- projects ---------------------------------------------------------

    def create(self, title: str, **fields: Any) -> dict:
        data = self._clean({"title": title, **fields})
        now = _now()
        data.update(created_at=now, updated_at=now)
        cols = ", ".join(data)
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO projects ({cols}) VALUES ({', '.join('?' * len(data))})",
                list(data.values()),
            )
            pid = cur.lastrowid or 0
        return self.get(pid) or {}

    def update(self, pid: int, **fields: Any) -> dict:
        if not self.get(pid):
            raise KeyError(pid)
        data = self._clean(fields, current_id=pid)
        if data:
            data["updated_at"] = _now()
            with self._conn() as conn:
                conn.execute(
                    f"UPDATE projects SET {', '.join(f'{k} = ?' for k in data)} "
                    "WHERE id = ?",
                    [*data.values(), pid],
                )
        return self.get(pid) or {}

    def delete(self, pid: int) -> bool:
        """Remove the project. Its reports, sources and notebooks are untouched."""
        with self._conn() as conn:
            members = [
                r["ref_id"]
                for r in conn.execute(
                    "SELECT ref_id FROM project_items WHERE project_id = ? "
                    "AND kind = 'session' AND is_home = 1",
                    (pid,),
                )
            ]
            conn.execute("DELETE FROM project_items WHERE project_id = ?", (pid,))
            gone = conn.execute("DELETE FROM projects WHERE id = ?", (pid,)).rowcount
        for sid in members:
            self._rehome(sid)
        return bool(gone)

    def get(self, pid: int) -> dict | None:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["archived"] = bool(d["archived"])
        return d

    def all(self, include_archived: bool = False) -> list[dict]:
        with self._conn() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM projects"
                    + ("" if include_archived else " WHERE archived = 0")
                    + " ORDER BY updated_at DESC, id DESC"
                )
            ]
            counts = {
                (r["project_id"], r["kind"]): r["n"]
                for r in conn.execute(
                    "SELECT project_id, kind, COUNT(*) AS n FROM project_items "
                    "GROUP BY project_id, kind"
                )
            }
        for d in rows:
            d["archived"] = bool(d["archived"])
            d["counts"] = {k: counts.get((d["id"], k), 0) for k in KINDS}
            d.pop("summary", None)  # large; fetched with the project
        return rows

    def touch(self, pid: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?", (_now(), pid)
            )

    def save_summary(self, pid: int, markdown: str, cost: float | None) -> dict:
        with self._conn() as conn:
            conn.execute(
                "UPDATE projects SET summary = ?, summary_at = ?, summary_cost = ? "
                "WHERE id = ?",
                (markdown, _now(), cost, pid),
            )
        return self.get(pid) or {}

    # ---- sessions: roots and trees ---------------------------------------

    def root_of(self, sid: int) -> int:
        """The top-level report a session belongs to (itself when it is one)."""
        with self._conn() as conn:
            if not self._has_sessions(conn):
                return sid
            seen = set()
            cur = sid
            while cur not in seen:
                seen.add(cur)
                r = conn.execute(
                    "SELECT parent_id FROM sessions WHERE id = ?", (cur,)
                ).fetchone()
                if not r or not r["parent_id"]:
                    return cur
                cur = int(r["parent_id"])
        return cur

    def descendants(self, roots: list[int]) -> list[int]:
        out: list[int] = []
        frontier = list(roots)
        with self._conn() as conn:
            if not self._has_sessions(conn):
                return []
            while frontier:
                marks = ",".join("?" * len(frontier))
                rows = conn.execute(
                    f"SELECT id FROM sessions WHERE parent_id IN ({marks})", frontier
                ).fetchall()
                frontier = [r["id"] for r in rows if r["id"] not in out]
                out += frontier
        return out

    # ---- memberships ------------------------------------------------------

    def items(self, pid: int, kind: str) -> list[dict]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT ref_id, is_home, added_at FROM project_items "
                    "WHERE project_id = ? AND kind = ? ORDER BY added_at, ref_id",
                    (pid, kind),
                )
            ]

    def item_ids(self, pid: int, kind: str) -> list[int]:
        return [r["ref_id"] for r in self.items(pid, kind)]

    def session_ids(self, pid: int, with_descendants: bool = True) -> list[int]:
        roots = self.item_ids(pid, "session")
        return roots + (self.descendants(roots) if with_descendants else [])

    def add_item(self, pid: int, kind: str, ref_id: int, home: bool = False) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if not self.get(pid):
            raise KeyError(pid)
        if kind == "session":
            ref_id = self.root_of(ref_id)  # sub-reports follow their root
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO project_items (project_id, kind, ref_id, "
                "is_home, added_at) VALUES (?, ?, ?, 0, ?)",
                (pid, kind, ref_id, _now()),
            )
            if kind == "session":
                has_home = conn.execute(
                    "SELECT 1 FROM project_items WHERE kind = 'session' AND ref_id = ? "
                    "AND is_home = 1",
                    (ref_id,),
                ).fetchone()
                if home or not has_home:
                    conn.execute(
                        "UPDATE project_items SET is_home = (project_id = ?) "
                        "WHERE kind = 'session' AND ref_id = ?",
                        (pid, ref_id),
                    )
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?", (_now(), pid)
            )
        return {"project_id": pid, "kind": kind, "id": ref_id}

    def remove_item(self, pid: int, kind: str, ref_id: int) -> bool:
        if kind == "session":
            ref_id = self.root_of(ref_id)
        with self._conn() as conn:
            gone = conn.execute(
                "DELETE FROM project_items WHERE project_id = ? AND kind = ? "
                "AND ref_id = ?",
                (pid, kind, ref_id),
            ).rowcount
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?", (_now(), pid)
            )
        if kind == "session":
            self._rehome(ref_id)
        return bool(gone)

    def set_home(self, pid: int, sid: int) -> None:
        self.add_item(pid, "session", sid, home=True)

    def _rehome(self, sid: int) -> None:
        """A report whose home went away gets its oldest remaining project as home."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT project_id, is_home FROM project_items WHERE kind = 'session' "
                "AND ref_id = ? ORDER BY added_at, project_id",
                (sid,),
            ).fetchall()
            if rows and not any(r["is_home"] for r in rows):
                conn.execute(
                    "UPDATE project_items SET is_home = 1 WHERE kind = 'session' "
                    "AND ref_id = ? AND project_id = ?",
                    (sid, rows[0]["project_id"]),
                )

    def purge(self, kind: str, ids: list[int]) -> None:
        """Forget memberships of deleted reports, sources or notebooks."""
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        with self._conn() as conn:
            conn.execute(
                f"DELETE FROM project_items WHERE kind = ? AND ref_id IN ({marks})",
                [kind, *ids],
            )

    def projects_for(self, kind: str, ref_id: int) -> list[dict]:
        if kind == "session":
            ref_id = self.root_of(ref_id)
        with self._conn() as conn:
            return [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "color": r["color"],
                    "is_home": bool(r["is_home"]),
                }
                for r in conn.execute(
                    "SELECT p.id, p.title, p.color, i.is_home FROM project_items i "
                    "JOIN projects p ON p.id = i.project_id WHERE i.kind = ? "
                    "AND i.ref_id = ? ORDER BY i.is_home DESC, p.title",
                    (kind, ref_id),
                )
            ]

    def home_project(self, sid: int) -> dict | None:
        for p in self.projects_for("session", sid):
            if p["is_home"]:
                return self.get(p["id"])
        return None

    def membership_map(self) -> dict[int, list[dict]]:
        """root session id -> [{id, title, color, is_home}] for the archive list."""
        out: dict[int, list[dict]] = {}
        with self._conn() as conn:
            for r in conn.execute(
                "SELECT i.ref_id, i.is_home, p.id, p.title, p.color FROM project_items i "
                "JOIN projects p ON p.id = i.project_id WHERE i.kind = 'session' "
                "ORDER BY i.is_home DESC, p.title"
            ):
                out.setdefault(r["ref_id"], []).append(
                    {
                        "id": r["id"],
                        "title": r["title"],
                        "color": r["color"],
                        "is_home": bool(r["is_home"]),
                    }
                )
        return out

    def inbox_ids(self) -> list[int]:
        with self._conn() as conn:
            if not self._has_sessions(conn):
                return []
            return [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM sessions WHERE parent_id IS NULL AND id NOT IN "
                    "(SELECT ref_id FROM project_items WHERE kind = 'session') "
                    "ORDER BY id DESC"
                )
            ]

    def last_change(self, pid: int) -> str:
        """Latest change to anything the summary reads (reports, memberships)."""
        ids = self.session_ids(pid)
        latest = ""
        with self._conn() as conn:
            r = conn.execute(
                "SELECT MAX(added_at) FROM project_items WHERE project_id = ?", (pid,)
            ).fetchone()
            latest = r[0] or ""
            if ids and self._has_sessions(conn):
                marks = ",".join("?" * len(ids))
                r = conn.execute(
                    f"SELECT MAX(updated_at) FROM sessions WHERE id IN ({marks})", ids
                ).fetchone()
                latest = max(latest, str(r[0] or "").replace(" ", "T")[:19])
        return latest


# ---------------------------------------------------------------- citations


LINK_RE = re.compile(r"\[([^\]]{1,200})\]\((https?://[^\s)]+)\)")


def citations(reports: list[dict]) -> list[dict]:
    """Unique cited links across reports: label, url, the sessions citing it."""
    seen: dict[str, dict] = {}
    for r in reports:
        for label, url in LINK_RE.findall(r.get("result") or ""):
            label = label.strip()
            if re.fullmatch(r"\d+", label):
                continue
            e = seen.setdefault(url, {"label": label, "url": url, "sessions": []})
            if r["id"] not in e["sessions"]:
                e["sessions"].append(r["id"])
    return sorted(seen.values(), key=lambda e: (-len(e["sessions"]), e["label"]))


def _bib_escape(s: str) -> str:
    return re.sub(r"([{}%&$#_])", r"\\\1", s)


def bibtex(project: dict, cites: list[dict]) -> str:
    lines = [
        f'% Sources cited in the deep-research project "{project["title"]}"',
        f"% Exported {_now()}. Grounding links may be redirect URLs; resolve them",
        "% before citing in a paper.",
        "",
    ]
    for i, c in enumerate(cites, 1):
        sessions = ", ".join(f"#{s}" for s in c["sessions"])
        lines += [
            f"@misc{{dr{project['id']}_{i},",
            f"  title = {{{_bib_escape(c['label'])}}},",
            f"  howpublished = {{\\url{{{c['url']}}}}},",
            f"  note = {{Cited in deep-research session {sessions}}},",
            "}",
            "",
        ]
    return "\n".join(lines)


def citations_csv(cites: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["label", "url", "sessions"])
    for c in cites:
        w.writerow([c["label"], c["url"], " ".join(str(s) for s in c["sessions"])])
    return buf.getvalue()


# ---------------------------------------------------------------- dossier


LAB_NOTE_CHARS = 6_000  # per Lab run write-up fed to summaries and audio


def lab_findings(runs: list[dict], limit: int = LAB_NOTE_CHARS) -> str:
    """Plain-text Lab findings for summaries and audio: each run's title, status,
    verdict and its written-up result (outputs, what it showed), newest first."""
    out = []
    for x in runs:
        if x.get("status") not in ("completed", "failed"):
            continue
        title = (x.get("plan") or {}).get("title") or "untitled"
        note = strip_sources(x.get("result_md") or "").strip()
        out.append(
            f"--- Lab run #{x['id']} on Session #{x.get('session_id')}: {title} "
            f"(status {x['status']}; verdict {verdict_line(x)}) ---\n"
            + (note[:limit] if note else "(no write-up)")
        )
    return "\n\n".join(out)


def verdict_line(run: dict) -> str:
    """The run's outcome (CONFIRMED / REFUTED / INCONCLUSIVE / BROKEN) and why, plus the
    raw check count; summaries and audio say what a run showed, not just pass/fail."""
    from deepresearch.dashboard.labverdict import assess

    v = run.get("verdict") or None
    a = run.get("assessment") or assess(
        v if isinstance(v, dict) else None, str(run.get("status") or "")
    )
    checks = (v or {}).get("checks") or [] if isinstance(v, dict) else []
    count = (
        f" ({sum(1 for c in checks if c.get('pass'))}/{len(checks)} checks passed)"
        if checks
        else ""
    )
    if a:
        return f"{a['outcome'].upper()}{count}: {a['why']}"
    return "no verdict"


def dossier_markdown(
    project: dict,
    reports: list[dict],
    notes: dict[int, list[dict]],
    lab_runs: list[dict],
    sources: list[dict],
    notebooks: list[dict],
    effective_level: str,
    full_reports: bool = True,
) -> str:
    """One Markdown document for the whole project."""
    p = project
    out = [f"# {p['title']}", ""]
    meta = [f"Protection level: {effective_level} (label only)"]
    if p.get("nexus_ref"):
        meta.append(f"Nexus: {p['nexus_ref']}")
    meta.append(f"Exported: {_now()}")
    out += [" | ".join(meta), ""]
    if p.get("description"):
        out += [p["description"], ""]
    if p.get("summary"):
        out += [
            "## Project summary",
            "",
            f"*AI summary generated {p.get('summary_at') or ''}.*",
            "",
            p["summary"].strip(),
            "",
        ]
    out += ["## Contents", ""]
    for r in reports:
        out.append(f"- Session #{r['id']}: {one_line(r['prompt'])} ({r['status']})")
    if lab_runs:
        out.append(f"- {len(lab_runs)} Lab run(s)")
    if sources:
        out.append(f"- {len(sources)} data source(s)")
    if notebooks:
        out.append(f"- {len(notebooks)} notebook(s)")
    out.append("")
    if sources:
        out += ["## Data sources", ""]
        for s in sources:
            out.append(
                f"- **{s['name']}** ({s['kind']}, {s.get('protection_level', 'P2')}): "
                f"`{s['uri']}`" + (f" - {s['title']}" if s.get("title") else "")
            )
        out.append("")
    if lab_runs:
        out += ["## Lab runs", ""]
        for run in lab_runs:
            plan = run.get("plan") or {}
            out.append(
                f"### Lab run #{run['id']}: {plan.get('title') or 'untitled'}\n\n"
                f"Report: Session #{run['session_id']} | status: {run['status']} | "
                f"verdict: {verdict_line(run)}"
            )
            v = run.get("verdict") or {}
            for c in v.get("checks") or [] if isinstance(v, dict) else []:
                mark = "PASS" if c.get("pass") else "FAIL"
                out.append(f"- [{mark}] {c.get('name')}: {c.get('got')}")
            if run.get("result_md"):
                out += ["", run["result_md"].strip()]
            out.append("")
    for r in reports:
        out += [f"## Session #{r['id']}: {one_line(r['prompt'])}", ""]
        body = r.get("result") or "*(no report yet)*"
        out += [body if full_reports else strip_sources(body)[:3000], ""]
        for a in notes.get(r["id"], []):
            out.append(f"> {a['quote']}")
            if a.get("note"):
                out += ["", a["note"]]
            out.append("")
    for nb in notebooks:
        out += [f"## Notebook: {nb['title']}", "", nb.get("content") or "", ""]
    return "\n".join(out).rstrip() + "\n"


def one_line(s: str, n: int = 160) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def slug(s: str, n: int = 50) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:n] or "item"


def research_package(
    project: dict,
    dossier_md: str,
    bundle: dict,
    reports: list[dict],
    notebooks: list[dict],
    lab_files: dict[str, bytes],
    audio_files: dict[str, bytes],
    cites: list[dict],
) -> bytes:
    """ZIP: dossier, reports as an Obsidian-friendly folder, notebooks, Lab results,
    citations (BibTeX + CSV), audio, and RO-Crate metadata (FAIR packaging)."""
    files: dict[str, tuple[bytes, str]] = {}
    root = slug(project["title"])

    def put(name: str, data: bytes | str, fmt: str) -> None:
        files[name] = (data.encode() if isinstance(data, str) else data, fmt)

    put("README.md", _readme(project, reports, notebooks, lab_files), "text/markdown")
    put("dossier.md", dossier_md, "text/markdown")
    put("project.json", json.dumps(bundle, indent=2, default=str), "application/json")
    put("citations.bib", bibtex(project, cites), "application/x-bibtex")
    put("citations.csv", citations_csv(cites), "text/csv")
    index = [f"# {project['title']}", "", "## Reports", ""]
    for r in reports:
        name = f"reports/Session {r['id']} - {slug(r['prompt'], 40)}.md"
        front = (
            "---\n"
            f"session: {r['id']}\nstatus: {r['status']}\n"
            f"created: {r.get('created_at') or ''}\n"
            f'project: "{project["title"]}"\n---\n\n'
        )
        put(
            name,
            front + f"# {one_line(r['prompt'], 300)}\n\n" + (r.get("result") or ""),
            "text/markdown",
        )
        index.append(f"- [[{name[len('reports/') : -3]}]]")
    for nb in notebooks:
        put(
            f"notebooks/{slug(nb['title'])}-{nb['id']}.md",
            nb.get("content") or "",
            "text/markdown",
        )
    put("reports/Index.md", "\n".join(index) + "\n", "text/markdown")
    for name, data in lab_files.items():
        put(f"lab/{name}", data, _mime(name))
    for name, data in audio_files.items():
        put(f"audio/{name}", data, _mime(name))
    crate = {
        "@context": "https://w3id.org/ro/crate/1.1/context",
        "@graph": [
            {
                "@id": "ro-crate-metadata.json",
                "@type": "CreativeWork",
                "conformsTo": {"@id": "https://w3id.org/ro/crate/1.1"},
                "about": {"@id": "./"},
            },
            {
                "@id": "./",
                "@type": "Dataset",
                "name": project["title"],
                "description": project.get("description")
                or f"deep-research project exported {_now()}",
                "datePublished": _now()[:10],
                "keywords": ["deep-research", project.get("protection_level", "P2")],
                "hasPart": [{"@id": n} for n in sorted(files)],
            },
            *(
                {"@id": n, "@type": "File", "name": n, "encodingFormat": fmt}
                for n, (_, fmt) in sorted(files.items())
            ),
        ],
    }
    put("ro-crate-metadata.json", json.dumps(crate, indent=2), "application/ld+json")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, (data, _) in sorted(files.items()):
            z.writestr(f"{root}/{name}", data)
    return buf.getvalue()


def _mime(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower()
    return {
        "md": "text/markdown",
        "json": "application/json",
        "csv": "text/csv",
        "txt": "text/plain",
        "png": "image/png",
        "jpg": "image/jpeg",
        "svg": "image/svg+xml",
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "log": "text/plain",
    }.get(ext, "application/octet-stream")


def _readme(project: dict, reports: list, notebooks: list, lab_files: dict) -> str:
    return (
        f"# {project['title']}\n\n"
        "Exported from deep-research. Contents:\n\n"
        "- `dossier.md`: the whole project in one document (summary, Lab results, "
        "every report with its highlights, notebooks)\n"
        f"- `reports/`: {len(reports)} report(s), one Markdown file each with YAML "
        "front matter; `reports/Index.md` links them with [[wiki links]], so the "
        "folder opens as an Obsidian vault\n"
        f"- `notebooks/`: {len(notebooks)} notebook(s)\n"
        f"- `lab/`: {len({k.split('/')[0] for k in lab_files})} Lab run(s): write-up, "
        "verdict and small outputs (large files stay in the Lab run folder)\n"
        "- `citations.bib`, `citations.csv`: every cited link, with the reports "
        "citing it\n"
        "- `project.json`: machine-readable bundle (metadata, reports, highlights, "
        "Lab runs, data source references; never credentials)\n"
        "- `ro-crate-metadata.json`: RO-Crate 1.1 description of the package\n\n"
        f"Protection level label: {project.get('protection_level', 'P2')}. Handle "
        "the package accordingly.\n"
    )


# ---------------------------------------------------------------- AI: summary, ask


def summary_prompt(project: dict, reports: list[dict], lab_runs: list[dict]) -> str:
    budget = max(SUMMARY_PER_REPORT_MIN, SUMMARY_TOTAL_CHARS // max(1, len(reports)))
    parts = []
    for r in reports:
        body = strip_sources(r.get("result") or "")[:budget]
        if body.strip():
            parts.append(
                f"--- Session #{r['id']}: {one_line(r['prompt'], 300)} ---\n{body}"
            )
    labs = [lab_findings(lab_runs)] if lab_findings(lab_runs) else []
    return (
        "You are writing the overview page of a research project that contains the "
        "reports below. Write Markdown with these sections:\n"
        "1. **Bottom line** (two or three sentences).\n"
        "2. **Key findings** across the reports, as bullets.\n"
        "3. **Where the reports agree and where they disagree.**\n"
        "4. **Computational evidence**: what the Lab runs checked, and whether each "
        "supports, contradicts or cannot decide the claim (skip if there are none).\n"
        "5. **Open questions and gaps.**\n"
        "6. **Suggested next steps** (next research questions or Lab runs).\n"
        "Cite the report behind every claim as *Session #N* and a Lab run as "
        "*Lab run #N*. Use only what is in the material; say so when it is thin. "
        "Plain, direct language.\n\n"
        f"PROJECT: {project['title']}\n"
        + (
            f"DESCRIPTION: {project['description']}\n"
            if project.get("description")
            else ""
        )
        + (
            "\nLAB RUNS (computational checks and their write-ups):\n" + labs[0] + "\n"
            if labs
            else ""
        )
        + "\nREPORTS:\n"
        + "\n\n".join(parts)
    )


def ask_prompt(
    question: str, project: dict, ranked: list[tuple[float, dict]], sources_ctx: str
) -> str:
    ctx = "".join(
        f"--- SESSION {d['id']} (relevance {sc:.2f}) ---\nPROMPT: {d['prompt']}\n"
        f"RESULT:\n{strip_sources(d.get('result') or '')[:ASK_REPORT_CHARS]}\n\n"
        for sc, d in ranked
    )
    return (
        f'Question about the research project "{project["title"]}": {question}\n\n'
        f"Reports in this project, most relevant first:\n{ctx}\n"
        + (f"Project data sources:\n{sources_ctx}\n\n" if sources_ctx else "")
        + "INSTRUCTIONS:\n1. Answer using ONLY the material above.\n"
        '2. Cite the report for every fact as "[Session #12]", and a data source '
        "by its name.\n"
        "3. If the answer is not in the material, say so plainly and suggest a "
        "research question that would answer it."
    )


def cosine_rank(
    qvec: list[float], docs: list[dict], limit: int
) -> list[tuple[float, dict]]:
    qn = math.sqrt(sum(x * x for x in qvec)) or 1.0
    scored = []
    for d in docs:
        v = _loads(d.get("embedding"), None)
        if not v:
            continue
        vn = math.sqrt(sum(x * x for x in v)) or 1.0
        scored.append((sum(map(operator.mul, qvec, v)) / (qn * vn), d))
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored[:limit]


# ---------------------------------------------------------------- suggestions

STOP = set(
    """a an and are as at be best by can deep do does explain for from how i in into
    is it its me my of on or research researching summarize summerize analize the
    this to use using what when where which who why with you your about all get
    these those their there them than then also any make most new way ways vs
    following context topics search each more some our out over under via will""".split()
)


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


MONTHS = set(
    "jan feb mar apr may jun jul aug sep sept oct nov dec january february march "
    "april june july august september october november december".split()
)


def _words(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z][a-z0-9+-]{2,}", text.lower())
        if w not in STOP and w not in MONTHS and not w.isdigit()
    }


def label_for(prompts: list[str], corpus: list[str] | None = None) -> str:
    """A short name from words common in the group and rare elsewhere (no AI, free)."""
    df: Counter = Counter()
    for p in prompts:
        df.update(_words(p))
    cdf: Counter = Counter()
    for p in corpus or prompts:
        cdf.update(_words(p))
    n_corpus = max(1, len(corpus or prompts))
    need = max(2, len(prompts) // 3) if len(prompts) > 2 else 1
    scored = [
        (n / len(prompts) * math.log((1 + n_corpus) / (1 + cdf[w])), w)
        for w, n in df.items()
        if n >= need
    ]
    top = [w for _, w in sorted(scored, reverse=True)[:3]]
    if not top:
        top = [w for w, _ in df.most_common(3)]
    joined = " ".join(prompts)

    def show(w: str) -> str:
        # keep acronyms the way the prompts wrote them (HPC, UCR, GCP)
        m = re.search(rf"\b({re.escape(w)})\b", joined, re.I)
        if m and m.group(1).isupper() and len(w) <= 6:
            return m.group(1)
        return w.capitalize()

    return " ".join(show(w) for w in top) or "Related reports"


def suggest_groups(
    rows: list[dict],
    threshold: float = 0.78,
    min_size: int = 3,
    limit: int = 12,
    corpus: list[str] | None = None,
) -> list[dict]:
    """Average-linkage clusters of report embeddings. rows: {id, prompt, embedding}."""
    ids, vecs, prompts = [], [], {}
    for r in rows:
        v = _loads(r.get("embedding"), None)
        if not v:
            continue
        ids.append(r["id"])
        vecs.append(_unit(v))
        prompts[r["id"]] = r.get("prompt") or ""
    n = len(vecs)
    if n < min_size:
        return []
    sim = [[0.0] * n for _ in range(n)]
    for a in range(n):
        for b in range(a + 1, n):
            sim[a][b] = sim[b][a] = sum(map(operator.mul, vecs[a], vecs[b]))
    clusters: list[list[int]] = [[i] for i in range(n)]
    # link sums between clusters, updated on merge (keeps this O(n^2) per merge)
    link = {(i, j): sim[i][j] for i in range(n) for j in range(i + 1, n)}
    alive = {i: [i] for i in range(n)}
    while len(alive) > 1:
        best, pair = -1.0, None
        keys = sorted(alive)
        for x, i in enumerate(keys):
            for j in keys[x + 1 :]:
                s = link[(i, j)] / (len(alive[i]) * len(alive[j]))
                if s > best:
                    best, pair = s, (i, j)
        if pair is None or best < threshold:
            break
        i, j = pair
        for k in alive:
            if k in (i, j):
                continue
            a, b = sorted((i, k))
            c, d = sorted((j, k))
            link[(a, b)] = link[(a, b)] + link[(c, d)]
        alive[i] = alive[i] + alive.pop(j)
    clusters = sorted(alive.values(), key=len, reverse=True)
    out = []
    for members in clusters:
        if len(members) < min_size:
            continue
        sids = sorted((ids[k] for k in members), reverse=True)
        tight = [sim[a][b] for x, a in enumerate(members) for b in members[x + 1 :]]
        out.append(
            {
                "label": label_for([prompts[s] for s in sids], corpus),
                "sessions": sids,
                "prompts": {s: one_line(prompts[s], 140) for s in sids},
                "cohesion": round(sum(tight) / len(tight), 3) if tight else 1.0,
            }
        )
    return out[:limit]


def similar_to(
    member_rows: list[dict],
    candidate_rows: list[dict],
    limit: int = 8,
    floor: float = 0.72,
) -> list[dict]:
    """Unfiled reports closest to the project's centroid."""
    vecs = [_unit(v) for r in member_rows if (v := _loads(r.get("embedding"), None))]
    if not vecs:
        return []
    dim = len(vecs[0])
    centroid = _unit([sum(v[k] for v in vecs) / len(vecs) for k in range(dim)])
    scored = []
    for r in candidate_rows:
        v = _loads(r.get("embedding"), None)
        if not v or len(v) != dim:
            continue
        s = sum(map(operator.mul, centroid, _unit(v)))
        if s >= floor:
            scored.append(
                {
                    "id": r["id"],
                    "prompt": one_line(r["prompt"], 160),
                    "score": round(s, 3),
                }
            )
    scored.sort(key=lambda d: d["score"], reverse=True)
    return scored[:limit]
