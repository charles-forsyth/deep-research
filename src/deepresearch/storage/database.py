import sqlite3
import os


class DatabaseSchema:
    @staticmethod
    def init_db(db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    interaction_id TEXT,
                    prompt TEXT,
                    status TEXT,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP,
                    result TEXT,
                    files JSON
                )
            """)

            cursor = conn.execute("PRAGMA table_info(sessions)")
            columns = [col[1] for col in cursor.fetchall()]
            if "pid" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN pid INTEGER")
            if "parent_id" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN parent_id INTEGER")
            if "depth" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN depth INTEGER DEFAULT 1")
            if "embedding" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN embedding TEXT")

            conn.commit()
