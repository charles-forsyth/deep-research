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

class ConnectionProxy:
    """
    A proxy for sqlite3.Connection that counts execute calls and correctly
    delegates attribute setting (like row_factory).
    """
    def __init__(self, conn):
        # Use object.__setattr__ to avoid recursion with our own __setattr__
        object.__setattr__(self, '_conn', conn)
        object.__setattr__(self, 'execute_count', 0)

    def execute(self, *args, **kwargs):
        self.execute_count += 1
        return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name in ('_conn', 'execute_count'):
            # Set our own attributes directly
            object.__setattr__(self, name, value)
        else:
            # Delegate other attributes to the wrapped connection
            setattr(self._conn, name, value)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._conn.__exit__(exc_type, exc_val, exc_tb)

def test_list_sessions_avoids_n_plus_1_query_issue(test_db):
    """
    Tests that list_sessions() is efficient and doesn't perform N+1 queries
    when checking the status of child sessions' parents.
    """
    # 1. Setup: Populate the database with a parent and 3 child sessions.
    # The children are running but have no PID, forcing a parent check.
    mgr = SessionManager(test_db)
    parent_id = mgr.create_session("parent", "Parent", pid=os.getpid())
    mgr.create_session("child1", "Child 1 running", parent_id=parent_id)
    mgr.create_session("child2", "Child 2 running", parent_id=parent_id)
    mgr.create_session("child3", "Child 3 running", parent_id=parent_id)

    # 2. Patch sqlite3.connect to intercept the connection
    proxy_conn_holder = []
    original_connect = sqlite3.connect

    def connect_proxy(db_path, *args, **kwargs):
        real_conn = original_connect(db_path, *args, **kwargs)
        proxy = ConnectionProxy(real_conn)
        proxy_conn_holder.append(proxy)
        return proxy

    # We patch 'deep_research.sqlite3.connect' because that's where it's imported and used.
    with patch('deep_research.sqlite3.connect', side_effect=connect_proxy):
        # 3. Act: Call the method under test
        mgr.list_sessions()

    # 4. Assert: Check the number of queries executed.
    assert len(proxy_conn_holder) > 0, "connect was not called"
    proxy_conn = proxy_conn_holder[0]

    # Expected queries in the initial, un-optimized state:
    #   1 for "SELECT * FROM sessions..."
    #   3 for "SELECT pid, status FROM sessions WHERE id = ?" (one for each child)
    #   = 4 total queries
    #
    # Expected queries in the optimized state:
    #   1 for "SELECT * FROM sessions..."
    #   1 for "SELECT id, pid, status FROM sessions WHERE id IN (?, ?, ?)"
    #   = 2 total queries
    #
    # The test asserts for the optimized state, so it should fail initially.
    # Note: If a parent is dead, an UPDATE query might also run. We use a live PID
    # to prevent this and isolate the N+1 SELECT problem.
    assert proxy_conn.execute_count <= 2, f"Expected 2 queries for efficiency, but {proxy_conn.execute_count} were made."
