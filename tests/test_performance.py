import sqlite3
import unittest
import tempfile
import os
from unittest.mock import patch

from deep_research import SessionManager

class TestSessionManagerPerformance(unittest.TestCase):

    def setUp(self):
        # Create a temporary file that will act as the database
        self.db_fd, self.db_path = tempfile.mkstemp()

        # This instance is just for setup.
        manager = SessionManager(db_path=self.db_path)

        # Now, connect to the same database file to add test data
        with sqlite3.connect(self.db_path) as conn:
            # Create 5 parent sessions that are "running"
            for i in range(1, 6):
                # Using a dummy PID of 99999 which is unlikely to exist
                conn.execute(
                    "INSERT INTO sessions (id, prompt, status, created_at, updated_at, pid) VALUES (?, ?, ?, ?, ?, ?)",
                    (i, f"Parent prompt {i}", "running", "2023-01-01", "2023-01-01", 99999)
                )
            # Create 10 child sessions linked to the parents
            for i in range(10):
                parent_id = (i % 5) + 1
                conn.execute(
                    "INSERT INTO sessions (prompt, status, created_at, updated_at, parent_id) VALUES (?, ?, ?, ?, ?)",
                    (f"Child prompt {i}", "running", "2023-01-01", "2023-01-01", parent_id)
                )
            conn.commit()

    def tearDown(self):
        # Ensure the file descriptor is closed and the temp file is deleted
        os.close(self.db_fd)
        os.unlink(self.db_path)

    @patch('os.kill', return_value=None) # Mock os.kill to prevent side effects
    def test_list_sessions_avoids_n_plus_one_queries(self, mock_os_kill):
        """
        Verify that list_sessions() uses a constant number of queries.
        """
        query_count = 0

        # Keep a reference to the original connect function
        original_connect = sqlite3.connect

        # This proxy class wraps a real connection and counts execute calls
        class ConnectionProxy:
            def __init__(self, connection):
                self._connection = connection

            def execute(self, *args, **kwargs):
                nonlocal query_count
                query_count += 1
                return self._connection.execute(*args, **kwargs)

            # --- Delegate all other attributes to the real connection ---
            def __getattr__(self, name):
                return getattr(self._connection, name)

            def __setattr__(self, name, value):
                if name == '_connection':
                    super().__setattr__(name, value)
                else:
                    setattr(self._connection, name, value)

            def __enter__(self):
                self._connection.__enter__()
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                self._connection.__exit__(exc_type, exc_val, exc_tb)

        # The proxy function that creates the proxied connection
        def connect_proxy(*args, **kwargs):
            conn = original_connect(*args, **kwargs)
            return ConnectionProxy(conn)

        # Patch 'connect' in the module where it's being used
        with patch('deep_research.sqlite3.connect', new=connect_proxy):
            # Instantiate the manager *inside* the patch context
            manager = SessionManager(db_path=self.db_path)

            # Reset counter after manager initialization to ignore DB setup queries
            query_count = 0

            # Call the method under test
            manager.list_sessions(limit=15)

        # The optimized version should make 2 queries:
        # 1. Fetch the initial list of sessions.
        # 2. Fetch all unique parent sessions.
        self.assertEqual(query_count, 2, f"Expected 2 queries, but got {query_count}")

if __name__ == "__main__":
    unittest.main()
