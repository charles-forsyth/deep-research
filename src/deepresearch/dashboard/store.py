"""Workspace persistence for the dashboard: notebooks, annotations, session meta.

Lives in the same SQLite file as the session history (additive tables only;
the CLI never reads them).
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from deepresearch.core.config import user_db_path
from deepresearch.storage.database import DatabaseSchema


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DashboardStore:
    def __init__(self, db_path: str = user_db_path):
        self.db_path = db_path
        DatabaseSchema.init_db(db_path)
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS notebooks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS annotations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    quote TEXT NOT NULL,
                    occurrence INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    color TEXT NOT NULL DEFAULT 'amber',
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_annotations_session
                    ON annotations(session_id);
                CREATE TABLE IF NOT EXISTS session_meta (
                    session_id INTEGER PRIMARY KEY,
                    starred INTEGER NOT NULL DEFAULT 0,
                    tags TEXT NOT NULL DEFAULT '[]'
                );
                """
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Commit on success, roll back on error, always close."""
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

    # ---- sessions (dashboard-specific queries) --------------------------

    def session_rows(self, q: str | None = None, limit: int = 500) -> list[dict]:
        sql = (
            "SELECT s.id, s.interaction_id, s.prompt, s.status, s.created_at, "
            "s.updated_at, s.files, s.pid, s.parent_id, s.depth, "
            "LENGTH(COALESCE(s.result, '')) AS result_chars, "
            "(SELECT COUNT(*) FROM sessions c WHERE c.parent_id = s.id) AS children, "
            "(SELECT COUNT(*) FROM annotations a WHERE a.session_id = s.id) AS annotations, "
            "COALESCE(m.starred, 0) AS starred, COALESCE(m.tags, '[]') AS tags "
            "FROM sessions s LEFT JOIN session_meta m ON m.session_id = s.id"
        )
        params: list[Any] = []
        if q:
            sql += " WHERE s.prompt LIKE ? OR s.result LIKE ?"
            params += [f"%{q}%", f"%{q}%"]
        sql += " ORDER BY s.id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for r in rows:
            r["tags"] = _loads(r["tags"], [])
            r["files"] = _loads(r["files"], [])
            r["starred"] = bool(r["starred"])
        return rows

    def set_status(self, session_id: int, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), session_id),
            )

    def descendants(self, session_id: int) -> list[int]:
        out: list[int] = []
        frontier = [session_id]
        with self._conn() as conn:
            while frontier:
                marks = ",".join("?" * len(frontier))
                rows = conn.execute(
                    f"SELECT id FROM sessions WHERE parent_id IN ({marks})", frontier
                ).fetchall()
                frontier = [r["id"] for r in rows if r["id"] not in out]
                out += frontier
        return out

    def purge_session_workspace(self, session_ids: list[int]) -> None:
        if not session_ids:
            return
        marks = ",".join("?" * len(session_ids))
        with self._conn() as conn:
            conn.execute(
                f"DELETE FROM annotations WHERE session_id IN ({marks})", session_ids
            )
            conn.execute(
                f"DELETE FROM session_meta WHERE session_id IN ({marks})", session_ids
            )

    def get_meta(self, session_id: int) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT starred, tags FROM session_meta WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return {"starred": False, "tags": []}
        return {"starred": bool(row["starred"]), "tags": _loads(row["tags"], [])}

    def set_meta(
        self, session_id: int, starred: bool | None = None, tags: list | None = None
    ) -> dict:
        cur = self.get_meta(session_id)
        if starred is not None:
            cur["starred"] = bool(starred)
        if tags is not None:
            cur["tags"] = [str(t).strip() for t in tags if str(t).strip()][:20]
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO session_meta (session_id, starred, tags) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET starred = excluded.starred, "
                "tags = excluded.tags",
                (session_id, int(cur["starred"]), json.dumps(cur["tags"])),
            )
        return cur

    # ---- notebooks -------------------------------------------------------

    def list_notebooks(self) -> list[dict]:
        with self._conn() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT id, title, created_at, updated_at, LENGTH(content) AS chars "
                    "FROM notebooks ORDER BY updated_at DESC"
                ).fetchall()
            ]

    def get_notebook(self, nb_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM notebooks WHERE id = ?", (nb_id,)
            ).fetchone()
        return dict(row) if row else None

    def create_notebook(self, title: str, content: str = "") -> dict:
        now = _now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO notebooks (title, content, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ((title or "Untitled").strip()[:200], content, now, now),
            )
            nb_id = cur.lastrowid or 0
        return self.get_notebook(nb_id) or {}

    def update_notebook(
        self, nb_id: int, title: str | None = None, content: str | None = None
    ) -> dict | None:
        nb = self.get_notebook(nb_id)
        if not nb:
            return None
        with self._conn() as conn:
            conn.execute(
                "UPDATE notebooks SET title = ?, content = ?, updated_at = ? WHERE id = ?",
                (
                    (title if title is not None else nb["title"]).strip()[:200]
                    or "Untitled",
                    content if content is not None else nb["content"],
                    _now(),
                    nb_id,
                ),
            )
        return self.get_notebook(nb_id)

    def delete_notebook(self, nb_id: int) -> bool:
        with self._conn() as conn:
            return (
                conn.execute("DELETE FROM notebooks WHERE id = ?", (nb_id,)).rowcount
                > 0
            )

    # ---- annotations -----------------------------------------------------

    def list_annotations(self, session_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM annotations"
        params: list[Any] = []
        if session_id is not None:
            sql += " WHERE session_id = ?"
            params.append(session_id)
        sql += " ORDER BY id ASC"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def create_annotation(
        self,
        session_id: int,
        quote: str,
        occurrence: int = 0,
        note: str = "",
        color: str = "amber",
    ) -> dict:
        if color not in ANNOTATION_COLORS:
            color = "amber"
        now = _now()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO annotations (session_id, quote, occurrence, note, color, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, quote, max(0, int(occurrence)), note, color, now, now),
            )
            ann_id = cur.lastrowid
            row = conn.execute(
                "SELECT * FROM annotations WHERE id = ?", (ann_id,)
            ).fetchone()
        return dict(row)

    def update_annotation(
        self, ann_id: int, note: str | None = None, color: str | None = None
    ) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM annotations WHERE id = ?", (ann_id,)
            ).fetchone()
            if not row:
                return None
            new_color = color if color in ANNOTATION_COLORS else row["color"]
            conn.execute(
                "UPDATE annotations SET note = ?, color = ?, updated_at = ? WHERE id = ?",
                (
                    note if note is not None else row["note"],
                    new_color,
                    _now(),
                    ann_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM annotations WHERE id = ?", (ann_id,)
            ).fetchone()
        return dict(row)

    def delete_annotation(self, ann_id: int) -> bool:
        with self._conn() as conn:
            return (
                conn.execute("DELETE FROM annotations WHERE id = ?", (ann_id,)).rowcount
                > 0
            )


ANNOTATION_COLORS = ("amber", "cyan", "magenta", "green")


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
