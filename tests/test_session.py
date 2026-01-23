import pytest
import os
import sqlite3
from unittest.mock import patch

from deep_research import SessionManager

# A proxy class to count database queries.
# This is needed because sqlite3.Connection.execute is a read-only attribute and cannot be patched directly.
# This proxy correctly delegates all operations, including attribute setting (row_factory) and context management.
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
        if name in ('_conn', 'execute_count'):
            super().__setattr__(name, value)
        else:
            # Delegate attribute setting (e.g., row_factory) to the real connection
            setattr(self._conn, name, value)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Ensure the real connection's context management (commit/rollback) is called
        return self._conn.__exit__(exc_type, exc_val, exc_tb)

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
    fake_pid = 99999
    
    with patch("os.kill", side_effect=OSError):
        mgr.create_session("v1_D", "Test Dead PID", pid=fake_pid)
        sessions = mgr.list_sessions()

    assert sessions[0]['status'] == 'crashed'

def test_list_sessions_avoids_nplus1_query(test_db):
    """
    Tests that list_sessions avoids the N+1 query problem when checking parent statuses.
    """
    mgr = SessionManager(db_path=test_db)

    # 1. Setup: Create a parent and several child sessions that need parent status checks
    parent_id = mgr.create_session("parent_session", "Parent", pid=12345) # A running parent
    mgr.create_session("child_1", "Child 1", parent_id=parent_id)
    mgr.create_session("child_2", "Child 2", parent_id=parent_id)
    mgr.create_session("child_3", "Child 3", parent_id=parent_id)

    # Mock os.kill to prevent real process checks, all PIDs are considered alive
    with patch("os.kill", return_value=None):
        
        # 2. Patch sqlite3.connect to intercept queries and count them
        real_connect = sqlite3.connect

        proxy_container = []
        def mock_connect(db_path, *args, **kwargs):
            conn = real_connect(db_path, *args, **kwargs)
            proxy = ConnectionProxy(conn)
            proxy_container.append(proxy)
            return proxy

        # Patch where the object is looked up (in the deep_research module)
        with patch("deep_research.sqlite3.connect", side_effect=mock_connect):

            # 3. Action: Call the method we are testing
            sessions = mgr.list_sessions()

            # Get the actual proxy instance that was created inside the context
            assert len(proxy_container) > 0, "mock_connect was not called"
            proxy_instance = proxy_container[0]

            # 4. Assert: Check the number of queries
            # The optimized code should only perform 2 queries:
            # 1. Fetch all sessions.
            # 2. Fetch all unique parents for those sessions.
            # The unoptimized code would perform 1 + 3 = 4 queries.
            assert len(sessions) == 4
            assert proxy_instance.execute_count == 2, f"Should only use 2 queries (1 for sessions, 1 for all parents), but got {proxy_instance.execute_count}"
