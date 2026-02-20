import os
import json
import sqlite3
from datetime import datetime

from deepresearch.storage.database import DatabaseSchema
from deepresearch.core.config import user_db_path


class SessionManager:
    def __init__(self, db_path: str = user_db_path):
        self.db_path = db_path
        DatabaseSchema.init_db(self.db_path)

    def create_session(
        self,
        interaction_id: str,
        prompt: str,
        files: list[str] | None = None,
        pid: int | None = None,
        parent_id: int | None = None,
        depth: int = 1,
    ) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO sessions (interaction_id, prompt, status, created_at, updated_at, files, pid, parent_id, depth) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    interaction_id,
                    prompt,
                    "running",
                    datetime.now().isoformat(),
                    datetime.now().isoformat(),
                    json.dumps(files or []),
                    pid,
                    parent_id,
                    depth,
                ),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def update_session_pid(self, session_id: int, pid: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE sessions SET pid = ? WHERE id = ?", (pid, session_id))
            conn.commit()

    def update_session_interaction_id(self, session_id: int, interaction_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE sessions SET interaction_id = ?, status = 'running', updated_at = ? WHERE id = ?",
                (interaction_id, datetime.now().isoformat(), session_id),
            )
            conn.commit()

    def update_session(
        self, interaction_id: str, status: str, result: str | None = None
    ):
        with sqlite3.connect(self.db_path) as conn:
            query = "UPDATE sessions SET status = ?, updated_at = ?"
            params = [status, datetime.now().isoformat()]
            if result:
                query += ", result = ?"
                params.append(result)
            query += " WHERE interaction_id = ?"
            params.append(interaction_id)

            conn.execute(query, tuple(params))
            conn.commit()

    def append_to_result(self, interaction_id: str, new_content: str):
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM sessions WHERE parent_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()

    def list_sessions(self, limit: int = 10):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            sessions = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()

            result = []
            for s in sessions:
                s_dict = dict(s)
                if s["status"] == "running":
                    is_dead = False
                    if s["pid"]:
                        try:
                            os.kill(s["pid"], 0)
                        except OSError:
                            is_dead = True
                    elif s["parent_id"]:
                        parent = conn.execute(
                            "SELECT pid, status FROM sessions WHERE id = ?",
                            (s["parent_id"],),
                        ).fetchone()
                        if parent:
                            if parent["status"] in [
                                "completed",
                                "crashed",
                                "failed",
                                "cancelled",
                            ]:
                                is_dead = True
                            elif parent["pid"]:
                                try:
                                    os.kill(parent["pid"], 0)
                                except OSError:
                                    is_dead = True

                    if is_dead:
                        s_dict["status"] = "crashed"
                        conn.execute(
                            "UPDATE sessions SET status = 'crashed' WHERE id = ?",
                            (s["id"],),
                        )
                        conn.commit()

                result.append(s_dict)
            return result

    def get_session(self, session_id_or_interaction_id: str):
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE sessions SET embedding = ?, updated_at = ? WHERE id = ?",
                (embedding_json, datetime.now().isoformat(), session_id),
            )
            conn.commit()

    def get_completed_sessions_without_embeddings(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT id, prompt, result FROM sessions WHERE status = 'completed' AND result IS NOT NULL AND embedding IS NULL"
            ).fetchall()

    def get_all_embeddings(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT id, prompt, result, embedding FROM sessions WHERE status = 'completed' AND result IS NOT NULL AND embedding IS NOT NULL"
            ).fetchall()
