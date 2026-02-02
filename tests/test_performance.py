import sqlite3
import pytest
from unittest.mock import patch
from deep_research import SessionManager

class ConnectionProxy:
    def __init__(self, real_conn, counter):
        super().__setattr__('_conn', real_conn)
        super().__setattr__('_counter', counter)

    def execute(self, *args, **kwargs):
        self._counter['count'] += 1
        return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._conn.__exit__(exc_type, exc_val, exc_tb)

@pytest.fixture
def query_counter():
    counter = {'count': 0}
    real_connect = sqlite3.connect

    def connect_proxy(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        return ConnectionProxy(conn, counter)

    with patch("deep_research.sqlite3.connect", side_effect=connect_proxy):
        # Reset SessionManager state
        if hasattr(SessionManager, '_initialized_dbs'):
            SessionManager._initialized_dbs.clear()
        yield counter

def test_list_sessions_batch_update(query_counter, tmp_path):
    db_path = str(tmp_path / "batch.db")
    mgr = SessionManager(db_path)

    # Create 5 sessions with dead PIDs
    for i in range(5):
        mgr.create_session(f"dead_{i}", f"Dead {i}", pid=99990+i)

    # Reset count
    query_counter['count'] = 0

    # Mock os.kill to raise OSError (dead process)
    with patch("os.kill", side_effect=OSError):
        sessions = mgr.list_sessions(limit=10)

    # Should be 1 query to fetch + 1 query to batch update = 2 queries
    print(f"\nQuery count with 5 crashed: {query_counter['count']}")
    assert query_counter['count'] == 2
    assert all(s['status'] == 'crashed' for s in sessions)

def test_list_sessions_query_count(query_counter, tmp_path):
    db_path = str(tmp_path / "perf.db")
    mgr = SessionManager(db_path)

    # Create a parent
    mgr.create_session("parent", "Parent", pid=12345)

    # Create 5 children (running, no PID, has parent_id=1)
    for i in range(5):
        mgr.create_session(f"child_{i}", f"Child {i}", parent_id=1)

    # Reset count before list_sessions
    query_counter['count'] = 0

    import unittest.mock
    with unittest.mock.patch("os.kill", return_value=None):
        mgr.list_sessions(limit=10)

    # Optimized implementation:
    # 1 (initial SELECT with JOIN)
    print(f"\nQuery count: {query_counter['count']}")
    assert query_counter['count'] == 1, "Should resolve N+1 problem"

def test_init_db_redundancy(query_counter, tmp_path):
    db_path = str(tmp_path / "init.db")

    # First init
    query_counter['count'] = 0
    _ = SessionManager(db_path)
    count1 = query_counter['count']
    assert count1 > 0

    # Second init (same path)
    query_counter['count'] = 0
    _ = SessionManager(db_path)
    count2 = query_counter['count']

    print(f"\nFirst init queries: {count1}, Second init queries: {count2}")
    # Optimized: Should NOT re-run any queries
    assert count2 == 0, "Should skip migrations if already initialized"
