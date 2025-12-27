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

def test_list_sessions_avoids_n_plus_one_queries(test_db):
    """
    Tests that listing sessions with children doesn't perform N+1 queries
    to check parent statuses. This test uses a real database and a connection
    wrapper to count the number of SELECT queries.
    """
    import sqlite3

    # Define a wrapper for the connection that counts SELECT queries
    class CountingConnection:
        def __init__(self, db_path):
            self._conn = sqlite3.connect(db_path)
            self._conn.row_factory = sqlite3.Row
            self.select_count = 0
        def execute(self, sql, *args):
            if "select" in sql.lower():
                self.select_count += 1
            return self._conn.execute(sql, *args)
        def __getattr__(self, name):
            return getattr(self._conn, name)
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            self._conn.close()

    # 1. Setup database with a parent and three children
    mgr = SessionManager(test_db)
    parent_pid = os.getpid()  # A known live PID
    parent_id = mgr.create_session("parent", "Parent", pid=parent_pid)
    # Child sessions have no PID and will need to check their parent's status
    mgr.create_session("child1", "Child 1", parent_id=parent_id)
    mgr.create_session("child2", "Child 2", parent_id=parent_id)
    mgr.create_session("child3", "Child 3", parent_id=parent_id)

    # 2. Patch `sqlite3.connect` to intercept the connection
    counting_conn_wrapper = CountingConnection(test_db)
    with patch('deep_research.sqlite3.connect', return_value=counting_conn_wrapper):
        with patch('os.kill'): # Mock os.kill to avoid ProcessLookupError
            mgr.list_sessions()

    # 3. Assert the number of queries.
    # The optimized code should only perform 2 SELECTs:
    #  - 1 for the initial list of sessions.
    #  - 1 to pre-fetch all required parent statuses.
    # The unoptimized code will perform 4 (1 + 3 children), causing this test to fail.
    assert counting_conn_wrapper.select_count == 2, \
        f"Expected 2 SELECTs (optimized), but found {counting_conn_wrapper.select_count} (N+1 problem)"
