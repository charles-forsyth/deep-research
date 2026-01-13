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


# --- Performance Test for N+1 Query ---

# We need a proxy to count queries, as sqlite3.Connection.execute is read-only
# and cannot be patched directly on an instance.
_real_connect = sqlite3.connect

class QueryCountingProxy:
    def __init__(self, *args, **kwargs):
        # Store the real connection in a way that doesn't trigger our __setattr__
        super().__setattr__('_conn', _real_connect(*args, **kwargs))
        super().__setattr__('execute_count', 0)

    def execute(self, *args, **kwargs):
        # Increment count and delegate
        super().__setattr__('execute_count', self.execute_count + 1)
        return self._conn.execute(*args, **kwargs)

    # --- Boilerplate for acting like a connection ---
    def __getattr__(self, attr):
        # Delegate attribute access to the real connection
        return getattr(self._conn, attr)

    def __setattr__(self, name, value):
        # Delegate attribute setting (e.g., row_factory) to the real connection
        setattr(self._conn, name, value)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # This will be called by 'with sqlite3.connect(...)'
        self._conn.close()

def test_list_sessions_performance_nplus1(test_db):
    """
    Tests that list_sessions is efficient and doesn't suffer from N+1 query bugs.
    """
    # 1. Setup Data: Create a parent and 3 running children with "dead" PIDs
    # This setup is outside the query counting, as it's just test prep.
    setup_mgr = SessionManager(test_db)
    parent_id = setup_mgr.create_session("parent", "Parent", pid=99999)
    setup_mgr.create_session("child1", "Child 1", parent_id=parent_id)
    setup_mgr.create_session("child2", "Child 2", parent_id=parent_id)
    setup_mgr.create_session("child3", "Child 3", parent_id=parent_id)
    setup_mgr.create_session("unrelated", "Unrelated Running", pid=os.getpid())

    # 2. Use a closure to capture the proxy instance our mocked connect creates
    conn_proxy = None
    def mocked_connect(*args, **kwargs):
        nonlocal conn_proxy
        # We need to use the real connect, which is stored in a global
        # because this function will be used in a patch.
        conn_proxy = QueryCountingProxy(*args, **kwargs)
        return conn_proxy

    with patch('sqlite3.connect', new=mocked_connect):
        # A new manager instance is needed to pick up the patched connect
        perf_mgr = SessionManager(test_db)

        # Mock os.kill to only fail for our fake PID
        def selective_kill(pid, sig):
            if pid == 99999:
                raise OSError("PID not found")
            # For the real pid (os.getpid()), do nothing to simulate it being alive.
            return None

        with patch("os.kill", side_effect=selective_kill):
            # 3. Run the function under test
            sessions = perf_mgr.list_sessions()

    # 4. Assert the query count is high (the bug is present)
    # With the N+1 fix, we expect a constant number of queries.
    # 1 query: SELECT * FROM sessions (initial fetch)
    # 1 query: SELECT * FROM sessions WHERE id IN (...) (parent pre-fetch)
    # 1 query: UPDATE sessions SET status = 'crashed' WHERE id IN (...) (batch update)
    # Total = 3
    assert conn_proxy is not None
    assert conn_proxy.execute_count == 3, f"Expected 3 queries after optimization, but got {conn_proxy.execute_count}"

    # 5. Check the logic was still correct
    crashed_count = sum(1 for s in sessions if s['status'] == 'crashed')
    assert crashed_count == 4 # Parent + 3 children