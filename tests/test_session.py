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

import sqlite3

class ConnectionProxy:
    def __init__(self, connection):
        # Use super().__setattr__ to avoid recursion with our __setattr__
        super().__setattr__('_connection', connection)
        super().__setattr__('execute_count', 0)

    def execute(self, *args, **kwargs):
        self.execute_count += 1
        return self._connection.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def __setattr__(self, name, value):
        if name in ('_connection', 'execute_count'):
            super().__setattr__(name, value)
        else:
            # Delegate setting attributes to the real connection
            # This is important for things like conn.row_factory = sqlite3.Row
            setattr(self._connection, name, value)

    # The 'with' statement in SessionManager needs these
    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._connection.__exit__(exc_type, exc_val, exc_tb)


def test_list_sessions_avoids_n_plus_one_queries(test_db):
    """
    Tests that list_sessions is efficient and avoids N+1 queries.
    This test will fail before the optimization and pass after.
    """
    proxies = []
    real_connect = sqlite3.connect

    def proxied_connect(db_path, *args, **kwargs):
        conn = real_connect(db_path, *args, **kwargs)
        proxy = ConnectionProxy(conn)
        proxies.append(proxy)
        return proxy

    # Patch the connect call in the module where it's used
    with patch('deep_research.sqlite3.connect', new=proxied_connect):
        mgr = SessionManager(test_db)

        # Setup: 1 parent, 2 running child sessions whose parent process is "crashed"
        # We don't give the parent a PID, so os.kill won't be called for it.
        parent_id = mgr.create_session("parent", "Parent", pid=None)
        # Mark parent as completed
        mgr.update_session("parent", "completed", "Parent done.")

        # Children are running, but their parent is done, so they should be marked crashed.
        mgr.create_session("child1", "Child 1", parent_id=parent_id)
        mgr.create_session("child2", "Child 2", parent_id=parent_id)

        # Reset query counters on all connections created so far (from _init_db and create_session)
        # before we call the method under test.
        for p in proxies:
            p.execute_count = 0

        # We don't need to mock os.kill here, because the logic checks parent *status* first.
        sessions = mgr.list_sessions()

        total_queries = sum(p.execute_count for p in proxies)

        # Before optimization, we expect:
        # 1. SELECT * FROM sessions ... (initial list)
        # 2. SELECT ... FROM sessions WHERE id = ? (for child1's parent) -> N+1
        # 3. UPDATE sessions SET status='crashed' WHERE id = ? (for child1) -> another N
        # 4. SELECT ... FROM sessions WHERE id = ? (for child2's parent) -> N+1
        # 5. UPDATE sessions SET status='crashed' WHERE id = ? (for child2) -> another N
        # Total: 5 queries

        # After optimization, we expect:
        # 1. SELECT * FROM sessions ...
        # 2. SELECT id, pid, status FROM sessions WHERE id IN (...)
        # 3. UPDATE sessions SET status='crashed' WHERE id IN (...)
        # Total: 3 queries

        # This assertion will fail before the fix.
        assert total_queries <= 3, f"Expected 3 or fewer queries, but found {total_queries}. N+1 problem likely."
