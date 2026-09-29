"""Data source registry: a table in the history DB (additive, like the dashboard's)."""

from __future__ import annotations

import builtins
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from deepresearch.core.config import user_db_path
from deepresearch.sources.model import DataSource, Manifest
from deepresearch.storage.database import DatabaseSchema

_COLS = (
    "name title description tags kind uri options auth_ref protection_level staging "
    "temporary status last_checked last_error manifest created_at updated_at"
).split()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SourceRegistry:
    def __init__(self, db_path: str = user_db_path):
        self.db_path = db_path
        DatabaseSchema.init_db(db_path)
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS data_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    kind TEXT NOT NULL,
                    uri TEXT NOT NULL,
                    options TEXT NOT NULL DEFAULT '{}',
                    auth_ref TEXT NOT NULL DEFAULT '',
                    protection_level TEXT NOT NULL DEFAULT 'P2',
                    staging TEXT NOT NULL DEFAULT 'auto',
                    temporary INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'unchecked',
                    last_checked TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    manifest TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS data_source_uses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL,
                    manifest_hash TEXT NOT NULL DEFAULT '',
                    used_by_kind TEXT NOT NULL,
                    used_by_id INTEGER NOT NULL,
                    role TEXT NOT NULL DEFAULT 'input',
                    created_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_ds_uses_source
                    ON data_source_uses(source_id);
                CREATE INDEX IF NOT EXISTS idx_ds_uses_user
                    ON data_source_uses(used_by_kind, used_by_id);
                """
            )
            # Snapshot of what the source was when it was used, so deleting or
            # renaming a source never rewrites an old report's provenance.
            have = {r[1] for r in c.execute("PRAGMA table_info(data_source_uses)")}
            for col in ("source_name", "source_kind", "source_uri"):
                if col not in have:
                    c.execute(
                        f"ALTER TABLE data_source_uses ADD COLUMN {col} TEXT "
                        "NOT NULL DEFAULT ''"
                    )
            # Back-fill snapshots for rows written before the columns existed.
            c.execute(
                "UPDATE data_source_uses SET source_name = (SELECT name FROM "
                "data_sources WHERE id = source_id), source_kind = (SELECT kind FROM "
                "data_sources WHERE id = source_id), source_uri = (SELECT uri FROM "
                "data_sources WHERE id = source_id) WHERE source_name = '' AND "
                "EXISTS (SELECT 1 FROM data_sources WHERE id = source_id)"
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

    @staticmethod
    def _row(r: sqlite3.Row) -> DataSource:
        d: dict[str, Any] = dict(r)
        d["tags"] = json.loads(d.get("tags") or "[]")
        d["options"] = json.loads(d.get("options") or "{}")
        d["temporary"] = bool(d.get("temporary"))
        m = d.get("manifest")
        d["manifest"] = Manifest.model_validate_json(m) if m else None
        return DataSource(**d)

    @staticmethod
    def _values(s: DataSource) -> list[Any]:
        return [
            s.name,
            s.title,
            s.description,
            json.dumps(s.tags),
            s.kind,
            s.uri,
            json.dumps(s.options),
            s.auth_ref,
            s.protection_level,
            s.staging,
            int(s.temporary),
            s.status,
            s.last_checked,
            s.last_error,
            s.manifest.model_dump_json() if s.manifest else None,
            s.created_at,
            s.updated_at,
        ]

    def add(self, s: DataSource) -> DataSource:
        s.created_at = s.updated_at = _now()
        if self.get_by_name(s.name):
            raise ValueError(f"A source named '{s.name}' already exists")
        with self._conn() as c:
            cur = c.execute(
                f"INSERT INTO data_sources ({', '.join(_COLS)}) "
                f"VALUES ({', '.join('?' * len(_COLS))})",
                self._values(s),
            )
            s.id = cur.lastrowid
        return s

    def update(self, s: DataSource) -> DataSource:
        if s.id is None:
            raise ValueError("source has no id")
        s.updated_at = _now()
        with self._conn() as c:
            c.execute(
                f"UPDATE data_sources SET {', '.join(f'{k}=?' for k in _COLS)} "
                "WHERE id=?",
                [*self._values(s), s.id],
            )
        return s

    def get(self, ref: str | int) -> DataSource | None:
        """By name, or by id (an int, or digits that are not some source's name)."""
        with self._conn() as c:
            r = None
            if not isinstance(ref, int):
                r = c.execute(
                    "SELECT * FROM data_sources WHERE name=?", (str(ref),)
                ).fetchone()
            if r is None and (isinstance(ref, int) or str(ref).isdigit()):
                r = c.execute(
                    "SELECT * FROM data_sources WHERE id=?", (int(ref),)
                ).fetchone()
        return self._row(r) if r else None

    def get_by_name(self, name: str) -> DataSource | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM data_sources WHERE name=?", (name,)).fetchone()
        return self._row(r) if r else None

    def set_options(self, s: DataSource, **changes: Any) -> DataSource:
        """Merge option keys into the stored row (None removes a key), re-reading it
        first so concurrent writers do not drop each other's keys."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute(
                "SELECT options FROM data_sources WHERE id=?", (s.id,)
            ).fetchone()
            opts = json.loads(r["options"] or "{}") if r else dict(s.options)
            for k, v in changes.items():
                if v is None:
                    opts.pop(k, None)
                else:
                    opts[k] = v
            c.execute(
                "UPDATE data_sources SET options=?, updated_at=? WHERE id=?",
                (json.dumps(opts), _now(), s.id),
            )
        s.options = opts
        return s

    def save_check(self, s: DataSource) -> None:
        """Store only what a test/check produced (status and manifest), so a check
        running next to an index build never overwrites the options it saved."""
        if s.id is None:
            return
        with self._conn() as c:
            c.execute(
                "UPDATE data_sources SET status=?, last_checked=?, last_error=?, "
                "manifest=?, updated_at=? WHERE id=?",
                (
                    s.status,
                    s.last_checked,
                    s.last_error,
                    s.manifest.model_dump_json() if s.manifest else None,
                    _now(),
                    s.id,
                ),
            )

    def require(self, ref: str | int) -> DataSource:
        s = self.get(ref)
        if s is None:
            raise KeyError(f"No data source '{ref}'")
        return s

    def list(self, include_temporary: bool = False) -> list[DataSource]:
        q = "SELECT * FROM data_sources"
        if not include_temporary:
            q += " WHERE temporary=0"
        with self._conn() as c:
            return [self._row(r) for r in c.execute(q + " ORDER BY name").fetchall()]

    def delete(self, ref: str | int) -> bool:
        s = self.get(ref)
        if not s:
            return False
        with self._conn() as c:
            # Keep the uses rows: they carry a snapshot (name, kind, uri, hash) and are
            # the provenance record of reports and Lab runs that used this source.
            c.execute("DELETE FROM data_sources WHERE id=?", (s.id,))
        return True

    def record_use(
        self, s: DataSource, used_by_kind: str, used_by_id: int, role: str = "input"
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO data_source_uses (source_id, manifest_hash, used_by_kind, "
                "used_by_id, role, created_at, source_name, source_kind, source_uri) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    s.id,
                    s.manifest.hash if s.manifest else "",
                    used_by_kind,
                    used_by_id,
                    role,
                    _now(),
                    s.name,
                    s.kind,
                    s.uri,
                ),
            )

    def uses(self, s: DataSource) -> builtins.list[dict[str, Any]]:
        with self._conn() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT used_by_kind, used_by_id, role, manifest_hash, created_at "
                    "FROM data_source_uses WHERE source_id=? ORDER BY id DESC",
                    (s.id,),
                ).fetchall()
            ]

    def used_by(
        self, used_by_kind: str, used_by_id: int
    ) -> builtins.list[dict[str, Any]]:
        with self._conn() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT COALESCE(NULLIF(u.source_name, ''), s.name) AS name, "
                    "COALESCE(NULLIF(u.source_kind, ''), s.kind) AS kind, "
                    "COALESCE(NULLIF(u.source_uri, ''), s.uri) AS uri, u.role, "
                    "u.manifest_hash FROM data_source_uses u "
                    "LEFT JOIN data_sources s ON s.id=u.source_id "
                    "WHERE u.used_by_kind=? AND u.used_by_id=? ORDER BY u.id",
                    (used_by_kind, used_by_id),
                ).fetchall()
            ]
