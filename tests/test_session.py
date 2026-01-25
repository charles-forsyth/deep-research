import pytest
import os
from unittest.mock import patch
from deep_research import SessionManager

@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_history.db"
    return str(db_file)

# ⚡ Test for performance optimization
class ConnectionProxy:
    """A proxy for sqlite3.Connection that counts execute calls and delegates all other calls."""
    def __init__(self, conn):
        self._conn = conn
        self.execute_count = 0

    def execute(self, *args, **kwargs):
        self.execute_count += 1
        return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        # Allow setting attributes on the proxy itself, otherwise delegate to the real connection.
        if name in ['_conn', 'execute_count']:
            super().__setattr__(name, value)
        else:
            setattr(self._conn, name, value)

    def __enter__(self):
        # The SessionManager uses the connection as a context manager.
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # The real connection's context manager is responsible for closing.
        # We don't want the proxy to interfere with it.
        pass

def test_list_sessions_avoids_nplus1_query_issue(test_db):
    """
    Verifies that list_sessions() is optimized to avoid N+1 queries.
    It should perform a constant number of SELECT queries regardless of the number of child sessions.
    """
    # 1. Setup: Create a parent and 5 child sessions that need parent status checks.
    mgr = SessionManager(test_db)
    parent_id = mgr.create_session("parent_session", "Parent prompt", pid=os.getpid())
    for i in range(5):
        # These children have no PID, so they will trigger the parent lookup logic.
        mgr.create_session(f"child_{i}", f"Child {i}", parent_id=parent_id)

    # 2. Use a proxy to count DB queries.
    import sqlite3
    real_conn = sqlite3.connect(test_db)
    proxy = ConnectionProxy(real_conn)

    # 3. Patch the 'connect' call within the deep_research module to return our proxy.
    with patch('deep_research.sqlite3.connect', return_value=proxy):
        mgr_patched = SessionManager(test_db)

        # Reset the counter after the _init_db call that happens during SessionManager instantiation.
        proxy.execute_count = 0

        # 4. Execute the function under test.
        mgr_patched.list_sessions()

        # 5. Assert the query count.
        # The optimized version should make exactly 2 SELECT queries:
        #   - 1 for the initial list of sessions.
        #   - 1 for fetching all required parent sessions in a single batch.
        # The parent PID is alive, so no UPDATE queries should be triggered.
        assert proxy.execute_count == 2, "Expected 2 queries (initial list + parent batch). N+1 issue may exist."


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
