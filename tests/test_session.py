import pytest
import os
from unittest.mock import patch
from deep_research import SessionManager
import sqlite3

# Store the original connect function before it's patched
original_sqlite3_connect = sqlite3.connect

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

# Test helper to count queries
class QueryCounter:
    """A proxy for a sqlite3.Connection that counts execute calls and handles context management."""
    def __init__(self, conn):
        self._conn = conn
        self.count = 0

    def execute(self, *args, **kwargs):
        self.count += 1
        return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        # Proxy attribute setting (like row_factory) to the real connection
        if name in ('_conn', 'count'):
            super().__setattr__(name, value)
        else:
            setattr(self._conn, name, value)

    def __enter__(self):
        # Proxy context manager entry to the real connection to handle transactions
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Proxy context manager exit to the real connection to handle commit/rollback
        self._conn.__exit__(exc_type, exc_val, exc_tb)

@patch('sqlite3.connect')
def test_list_sessions_is_optimized(mock_connect, test_db):
    # This test verifies that the N+1 query problem has been fixed by
    # counting the exact number of queries executed.

    # Use the original connect to create a real database connection
    real_conn = original_sqlite3_connect(test_db, check_same_thread=False)
    query_counter = QueryCounter(real_conn)
    mock_connect.return_value = query_counter

    mgr = SessionManager(test_db) # This will create tables, etc.

    # --- Setup Data ---
    # Create 1 parent with a dead pid and 5 children who will reference it.
    # All are 'running', so they will be checked.
    parent_id = mgr.create_session("parent", "Parent", pid=99999)
    for i in range(5):
        mgr.create_session(f"child_{i}", f"Child {i}", parent_id=parent_id)
    # --- End Setup ---

    # Reset the counter after setup is done.
    query_counter.count = 0

    # Mock os.kill to simulate dead processes
    with patch("os.kill", side_effect=OSError):
        mgr.list_sessions()

    # --- Assertions ---
    # With the optimization, we now expect exactly 3 queries:
    # 1. One query to SELECT all sessions.
    #       `SELECT * FROM sessions ...`
    # 2. One query to SELECT all relevant parents in one go.
    #       `SELECT id, pid, status FROM sessions WHERE id IN (...)`
    # 3. One query to batch UPDATE all crashed sessions.
    #       `UPDATE sessions SET status = 'crashed' WHERE id IN (...)`
    #
    # This count is constant regardless of the number of children.
    expected_optimized_count = 3
    assert query_counter.count == expected_optimized_count, f"Expected {expected_optimized_count} queries for optimized version, but got {query_counter.count}"

    real_conn.close()
