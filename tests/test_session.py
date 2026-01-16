import pytest
import os
from unittest.mock import patch
from deep_research import SessionManager

@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_history.db"
    return str(db_file)

def test_create_session(test_db):
    mgr = SessionManager(test_db)
    sid = mgr.create_session("v1_123", "Test prompt", ["file1.txt"])
    
    assert sid == 1
    session = mgr.get_session(1)
    assert session['interaction_id'] == "v1_123"
    assert session['prompt'] == "Test prompt"
    assert session['status'] == "running"
    assert "file1.txt" in session['files']

def test_update_session(test_db):
    mgr = SessionManager(test_db)
    mgr.create_session("v1_123", "Test")
    
    mgr.update_session("v1_123", "completed", "Result Text")
    
    session = mgr.get_session("v1_123")
    assert session['status'] == "completed"
    assert session['result'] == "Result Text"

def test_list_sessions(test_db):
    mgr = SessionManager(test_db)
    mgr.create_session("v1_A", "Test A")
    import time
    time.sleep(0.1) 
    mgr.create_session("v1_B", "Test B")
    
    sessions = mgr.list_sessions(limit=5)
    assert len(sessions) == 2
    assert sessions[0]['interaction_id'] == "v1_B"

def test_pid_tracking_alive(test_db):

    mgr = SessionManager(test_db)

    pid = os.getpid()

    mgr.create_session("v1_C", "Test PID", pid=pid)

    

    sessions = mgr.list_sessions()

    assert sessions[0]['status'] == 'running'

    assert sessions[0]['pid'] == pid



def test_pid_tracking_dead(test_db):

    mgr = SessionManager(test_db)

    # Use a likely unused PID (max pid is usually 32k or higher, but let's just mock os.kill)

    fake_pid = 99999

    

    with patch("os.kill", side_effect=OSError):
        mgr.create_session("v1_D", "Test Dead PID", pid=fake_pid)
        sessions = mgr.list_sessions()

    assert sessions[0]['status'] == 'crashed'

# Keep the original connect function for our proxy to use
import sqlite3
_original_sqlite3_connect = sqlite3.connect

def test_list_sessions_performance_n_plus_one(test_db):
    """
    Tests the N+1 query problem in list_sessions.
    Before optimization, it should make 1 (list) + N (parent check) queries.
    """
    # 1. Setup Data: 1 parent, 3 running children who need to check parent status
    mgr = SessionManager(test_db)
    parent_id = mgr.create_session("parent", "Parent", pid=os.getpid())
    # Create 3 children that will trigger the parent check
    for i in range(3):
        # The 'running' status is set by default on creation.
        mgr.create_session(f"child_{i}", f"Child {i}", parent_id=parent_id)

    # 2. Setup a proxy connection to count queries
    class ConnProxy:
        def __init__(self, conn):
            self._conn = conn
            self.query_count = 0

        def execute(self, *args, **kwargs):
            self.query_count += 1
            # Uncomment for debugging:
            # print(f"QUERY: {args[0]}")
            return self._conn.execute(*args, **kwargs)

        def __setattr__(self, name, value):
            # Special handling for our own attributes
            if name in ('_conn', 'query_count'):
                super().__setattr__(name, value)
            else:
                # Delegate all other attribute settings to the real connection
                setattr(self._conn, name, value)

        def __getattr__(self, name):
            # Delegate other attributes like .commit(), .row_factory, etc.
            return getattr(self._conn, name)
        
        def __enter__(self):
            # Allow the proxy to be used as a context manager
            self._conn.__enter__()
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            # Pass through to the real connection
            self._conn.__exit__(exc_type, exc_val, exc_tb)

    # Intercept sqlite3.connect and return our proxy wrapping a real connection.
    real_conn = _original_sqlite3_connect(test_db)
    proxy = ConnProxy(real_conn)

    with patch("sqlite3.connect", return_value=proxy):
        mgr_under_test = SessionManager(test_db)

        # Reset counter right before the action to be measured
        proxy.query_count = 0

        mgr_under_test.list_sessions()

        # Expected queries before fix:
        # 1. SELECT * FROM sessions...
        # 2. SELECT pid, status FROM sessions WHERE id = ?  (for child 1)
        # 3. SELECT pid, status FROM sessions WHERE id = ?  (for child 2)
        # 4. SELECT pid, status FROM sessions WHERE id = ?  (for child 3)
        # Expected queries AFTER fix:
        # 1. SELECT * FROM sessions...
        # 2. SELECT id, pid, status FROM sessions WHERE id IN (...)
        # 3. UPDATE sessions SET status = 'crashed' WHERE id IN (...) [if any crashed]
        # Total should be at most 3.
        assert proxy.query_count <= 3, f"Should be efficient, but was {proxy.query_count} queries"

    real_conn.close()
