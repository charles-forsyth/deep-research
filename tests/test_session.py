import pytest
import os
import sqlite3
from unittest.mock import patch

from deep_research import SessionManager

# A proxy to count DB calls
class ConnectionProxy:
    def __init__(self, conn):
        self._conn = conn
        self.execute_count = 0

    def execute(self, *args, **kwargs):
        self.execute_count += 1
        return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name in ['_conn', 'execute_count']:
            super().__setattr__(name, value)
        else:
            setattr(self._conn, name, value)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._conn:
            self._conn.close()


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

# Store the original connect function
original_connect = sqlite3.connect

def test_list_sessions_avoids_n_plus_1_query(test_db):
    mgr = SessionManager(test_db)

    # 1. Setup: 1 Parent, 3 Children
    parent_id = mgr.create_session("parent", "Parent prompt", pid=12345)
    mgr.create_session("child1", "Child 1", parent_id=parent_id)
    mgr.create_session("child2", "Child 2", parent_id=parent_id)
    mgr.create_session("child3", "Child 3", parent_id=parent_id)

    # Use a real connection wrapped in our proxy
    real_conn = original_connect(test_db)
    proxy_conn = ConnectionProxy(real_conn)

    with patch("sqlite3.connect", return_value=proxy_conn):
        with patch("os.kill", side_effect=OSError): # Make parent PID appear dead
            mgr.list_sessions()

    # Before fix: 1 (list) + 3 (parent lookups) + 4 (updates) = 8 queries
    # After fix:  1 (list) + 1 (parent lookup) + 4 (updates) = 6 queries
    # The key is avoiding the 3 separate parent lookups.
    # We assert for the OPTIMIZED number of queries, so this test will FAIL
    # until the code is fixed.
    assert proxy_conn.execute_count <= 6, f"Expected 6 or fewer queries (optimized) but got {proxy_conn.execute_count} (N+1 issue)"
