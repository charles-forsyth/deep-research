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


def test_list_sessions_n_plus_one_fix(test_db):
    """
    Tests that list_sessions uses a constant number of queries
    regardless of the number of child sessions, verifying the N+1 fix.
    """

    # This proxy class intercepts calls to the real database connection
    # to count how many times `execute` is called.
    class QueryCountingConnectionProxy:
        def __init__(self, connection):
            self._connection = connection
            self.execute_count = 0

        def execute(self, *args, **kwargs):
            self.execute_count += 1
            # print(f"QUERY: {args[0]}") # Uncomment for debugging
            return self._connection.execute(*args, **kwargs)

        def __setattr__(self, name, value):
            # Intercept attribute setting.
            # Attributes '_connection' and 'execute_count' are owned by the proxy itself.
            if name in ('_connection', 'execute_count'):
                # Use super().__setattr__ to avoid recursion
                super().__setattr__(name, value)
            # Delegate all other assignments (like 'row_factory') to the real connection.
            else:
                setattr(self._connection, name, value)

        def __getattr__(self, name):
            # Delegate all other attribute access (e.g., commit, close)
            # to the real connection object.
            return getattr(self._connection, name)

        def __enter__(self):
            # Support the context manager protocol (`with` statement)
            self._connection.__enter__()
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            # Delegate the exit call to the real connection to handle transactions
            return self._connection.__exit__(exc_type, exc_val, exc_tb)

    # 1. Setup a real connection and wrap it with the proxy
    real_conn = sqlite3.connect(test_db)
    proxy_conn = QueryCountingConnectionProxy(real_conn)

    # 2. Patch sqlite3.connect to return our proxy instead of a real connection
    with patch('deep_research.sqlite3.connect', return_value=proxy_conn):
        mgr = SessionManager(test_db)

        # 3. Setup test data: 1 parent, 5 running child sessions
        parent_id = mgr.create_session("parent_interaction", "Parent", pid=os.getpid())
        for i in range(5):
            mgr.create_session(f"child_{i}", f"Child {i}", parent_id=parent_id)

        # 4. Reset counter to ignore setup queries (CREATE, INSERT)
        proxy_conn.execute_count = 0

        # 5. Patch os.kill to isolate the test to only database logic
        with patch("os.kill"):
            mgr.list_sessions()

    # ⚡ VERIFY:
    # We expect exactly 2 SELECT queries now, thanks to the optimization:
    # 1. The initial `SELECT * FROM sessions ...`
    # 2. The single `SELECT ... FROM sessions WHERE id IN (...)` for all parents
    # This number should NOT scale with the number of children (which is 5).
    assert proxy_conn.execute_count == 2, "Should use a constant number of queries (2) for the parent check"
