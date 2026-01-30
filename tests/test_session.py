import pytest
import os
import sqlite3
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


# Hold a reference to the original connect function to avoid recursion in the patch
_original_sqlite_connect = sqlite3.connect


def test_list_sessions_avoids_n_plus_1_query(test_db):
    """
    Tests that list_sessions does not perform N+1 queries when checking
    parent statuses for running child sessions.
    """
    # 1. Setup: Create a proxy for sqlite3.Connection to count execute calls
    # We patch 'deep_research.sqlite3.connect' because that's where the lookup happens.

    execute_counts = []

    class ConnectionProxy:
        """A proxy to wrap the real sqlite3 connection and count execute calls."""
        def __init__(self, *args, **kwargs):
            # Connect to the real database using the original connect function
            self._connection = _original_sqlite_connect(*args, **kwargs)
            self.execute_count = 0
            execute_counts.append(self)

        def execute(self, *args, **kwargs):
            self.execute_count += 1
            return self._connection.execute(*args, **kwargs)

        def __setattr__(self, name, value):
            if name in ('_connection', 'execute_count'):
                super().__setattr__(name, value)
            else:
                setattr(self._connection, name, value)

        def __getattr__(self, name):
            # Delegate all other calls to the real connection
            return getattr(self._connection, name)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            self._connection.close()

    with patch("deep_research.sqlite3.connect", new=ConnectionProxy):
        mgr = SessionManager(test_db)

        # 2. Arrange: Create a crashed parent and 3 running children
        parent_id = mgr.create_session("parent", "Parent", pid=99999)

        # Mark parent as crashed (list_sessions will do this, but we do it manually for setup)
        with sqlite3.connect(test_db) as conn:
            conn.execute("UPDATE sessions SET status = 'crashed' WHERE id = ?", (parent_id,))
            conn.commit()

        mgr.create_session("child1", "Child 1", parent_id=parent_id)
        mgr.create_session("child2", "Child 2", parent_id=parent_id)
        mgr.create_session("child3", "Child 3", parent_id=parent_id)

        # Reset counters after setup queries
        for counter in execute_counts:
            counter.execute_count = 0

        # 3. Act: Call the method we are testing
        sessions = mgr.list_sessions()

    # 4. Assert
    # Without the fix:
    # 1 query for list_sessions SELECT *
    # 3 queries in the loop (one for each child checking the parent)
    # 3 queries to UPDATE the status of children of a dead parent if parent is dead
    # Total would be high.
    #
    # With the fix:
    # 1 query to get all sessions
    # 1 query to get all relevant parent statuses
    # 1 query to bulk-update any crashed sessions.
    # So, we expect a low, constant number of queries.
    total_queries = sum(c.execute_count for c in execute_counts)

    assert len(sessions) == 4
    # The exact number is less important than it being small and not scaling with N.
    # Let's assert it's less than N (4 in this case).
    assert total_queries < 4, f"Expected a low number of queries, but got {total_queries}. Likely an N+1 problem."
