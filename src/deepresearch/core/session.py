from deepresearch.utils.retry import db_retry
import os
import json
import sqlite3
from datetime import datetime, timedelta

from deepresearch.storage.database import DatabaseSchema
from deepresearch.core.config import user_db_path


class SessionManager:
    def __init__(self, db_path: str = user_db_path):
        self.db_path = db_path
        DatabaseSchema.init_db(self.db_path)

    @db_retry()
    def create_session(
        self,
        interaction_id: str,
        prompt: str,
        files: list[str] | None = None,
        pid: int | None = None,
        parent_id: int | None = None,
        depth: int = 1,
    ) -> int:
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            cursor = conn.execute(
                "INSERT INTO sessions (interaction_id, prompt, status, created_at, updated_at, files, pid, parent_id, depth) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    interaction_id,
                    prompt,
                    "running",
                    datetime.now().isoformat(),
                    datetime.now().isoformat(),
                    json.dumps(files or []),
                    pid
                    if pid is not None
                    else (os.getpid() if parent_id is None else None),
                    parent_id,
                    depth,
                ),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def update_session_pid(self, session_id: int, pid: int):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute("UPDATE sessions SET pid = ? WHERE id = ?", (pid, session_id))
            conn.commit()

    def update_session_interaction_id(self, session_id: int, interaction_id: str):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute(
                "UPDATE sessions SET interaction_id = ?, status = 'running', updated_at = ?, "
                "pid = CASE WHEN parent_id IS NULL THEN COALESCE(pid, ?) ELSE pid END WHERE id = ?",
                (interaction_id, datetime.now().isoformat(), os.getpid(), session_id),
            )
            conn.commit()

    @db_retry()
    def update_session(
        self, interaction_id: str, status: str, result: str | None = None
    ):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            query = "UPDATE sessions SET status = ?, updated_at = ?"
            params = [status, datetime.now().isoformat()]
            if result:
                query += ", result = ?"
                params.append(result)
            query += " WHERE interaction_id = ?"
            params.append(interaction_id)

            conn.execute(query, tuple(params))
            conn.commit()

    @db_retry()
    def fail_session_id(self, session_id: int, message: str):
        """Mark a row failed by its local id (used before an interaction id exists)."""
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute(
                "UPDATE sessions SET status = 'failed', result = ?, updated_at = ? "
                "WHERE id = ? AND status = 'running'",
                (message, datetime.now().isoformat(), session_id),
            )
            conn.commit()

    def append_to_result(self, interaction_id: str, new_content: str):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            row = conn.execute(
                "SELECT result FROM sessions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            if row:
                current_result = row[0] or ""
                updated_result = f"{current_result}\n\n{new_content}"
                conn.execute(
                    "UPDATE sessions SET result = ?, updated_at = ? WHERE interaction_id = ?",
                    (updated_result, datetime.now().isoformat(), interaction_id),
                )
                conn.commit()

    def get_children(self, session_id: int):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM sessions WHERE parent_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()

    # A Deep Research interaction is capped at 60 minutes by the API; a row with no process to
    # check that has shown no activity for this long cannot still be running.
    STALE_AFTER = timedelta(hours=3)

    @staticmethod
    def _pid_alive(pid) -> bool:
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError, TypeError):
            return False
        return True

    @classmethod
    def _stale(cls, row) -> bool:
        stamp = row["updated_at"] or row["created_at"]
        try:
            last = datetime.fromisoformat(str(stamp))
        except (TypeError, ValueError):
            return True
        if last.tzinfo is not None:
            last = last.replace(tzinfo=None)
        return datetime.now() - last > cls.STALE_AFTER

    def _is_dead(self, conn, s) -> bool:
        if s["pid"]:
            return not self._pid_alive(s["pid"])
        if s["parent_id"]:
            parent = conn.execute(
                "SELECT pid, status, updated_at, created_at FROM sessions WHERE id = ?",
                (s["parent_id"],),
            ).fetchone()
            if parent:
                if parent["status"] in ("completed", "crashed", "failed", "cancelled"):
                    return True
                if parent["pid"]:
                    return not self._pid_alive(parent["pid"])
        # No process recorded (runs started before pids were tracked): judge by activity.
        return self._stale(s)

    @db_retry()
    def list_sessions(self, limit: int = 10):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            sessions = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()

            result = []
            for s in sessions:
                s_dict = dict(s)
                if s["status"] == "running" and self._is_dead(conn, s):
                    s_dict["status"] = "crashed"
                    conn.execute(
                        "UPDATE sessions SET status = 'crashed' WHERE id = ?",
                        (s["id"],),
                    )
                    conn.commit()

                result.append(s_dict)
            return result

    @db_retry()
    def get_session(self, session_id_or_interaction_id: str):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            if str(session_id_or_interaction_id).isdigit():
                return conn.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id_or_interaction_id,),
                ).fetchone()
            return conn.execute(
                "SELECT * FROM sessions WHERE interaction_id = ?",
                (session_id_or_interaction_id,),
            ).fetchone()

    def delete_session(self, session_id_or_interaction_id: str) -> bool:
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            if str(session_id_or_interaction_id).isdigit():
                cursor = conn.execute(
                    "DELETE FROM sessions WHERE id = ?", (session_id_or_interaction_id,)
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM sessions WHERE interaction_id = ?",
                    (session_id_or_interaction_id,),
                )
            conn.commit()
            return cursor.rowcount > 0

    def update_embedding(self, session_id: int, embedding_json: str):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute(
                # Indexing is not activity: leave updated_at alone so run
                # timing and ordering stay truthful.
                "UPDATE sessions SET embedding = ? WHERE id = ?",
                (embedding_json, session_id),
            )
            conn.commit()

    def get_completed_sessions_without_embeddings(self):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT id, prompt, result FROM sessions WHERE status = 'completed' AND result IS NOT NULL AND embedding IS NULL"
            ).fetchall()

    def get_all_embeddings(self):
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT id, prompt, result, embedding FROM sessions WHERE status = 'completed' AND result IS NOT NULL AND embedding IS NOT NULL"
            ).fetchall()
