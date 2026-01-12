import pytest
import os
import sqlite3
from unittest.mock import patch, MagicMock

from deep_research import SessionManager

# Store the original connect function
_original_sqlite_connect = sqlite3.connect

class QueryCounter:
    def __init__(self):
        self.count = 0
    def __call__(self, *args, **kwargs):
        self.count += 1

class ConnectionProxy:
    def __init__(self, real_conn, counter):
        self._real_conn = real_conn
        self._counter = counter

    def execute(self, *args, **kwargs):
        self._counter()
        return self._real_conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        # Delegate everything else to the real connection
        return getattr(self._real_conn, name)

    def __setattr__(self, name, value):
        # Handle attribute setting on the proxy vs the real object
        if name in ['_real_conn', '_counter']:
            super().__setattr__(name, value)
        else:
            setattr(self._real_conn, name, value)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._real_conn.close()


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
    # Use a likely unused PID
    fake_pid = 99999
    
    with patch("os.kill", side_effect=OSError):
        mgr.create_session("v1_D", "Test Dead PID", pid=fake_pid)
        sessions = mgr.list_sessions()

    assert sessions[0]['status'] == 'crashed'

def test_list_sessions_avoids_n_plus_1_queries(test_db):
    """
    Ensures that checking the status of child processes does not
    result in N+1 database queries.
    """
    mgr = SessionManager(test_db)

    # 1. Create a parent and several running child sessions without PIDs
    parent_id = mgr.create_session("parent_1", "Parent", pid=12345)
    mgr.create_session("child_1", "Child 1", parent_id=parent_id)
    mgr.create_session("child_2", "Child 2", parent_id=parent_id)
    mgr.create_session("child_3", "Child 3", parent_id=parent_id)

    query_counter = QueryCounter()

    def connect_proxy(db_path):
        real_conn = _original_sqlite_connect(db_path)
        return ConnectionProxy(real_conn, query_counter)

    with patch("sqlite3.connect", side_effect=connect_proxy):
        # We need to re-initialize the manager to use the patched connect
        # This will run _init_db and increment the counter with setup queries
        mgr_patched = SessionManager(test_db)
        
        # Isolate the query count to only the list_sessions method call
        queries_before = query_counter.count

        with patch("os.kill"):
            mgr_patched.list_sessions()

        queries_after = query_counter.count

    queries_in_list_sessions = queries_after - queries_before

    # EXPECTATION for list_sessions() only:
    # Optimized:
    # 1. SELECT * FROM sessions...
    # 2. SELECT id, pid, status FROM sessions WHERE id IN (...)
    # Total: 2
    #
    # Unoptimized:
    # 1. SELECT * FROM sessions...
    # 2. SELECT ... WHERE id = ?
    # 3. SELECT ... WHERE id = ?
    # 4. SELECT ... WHERE id = ?
    # Total: 1 + N = 4
    assert queries_in_list_sessions <= 2, f"Expected <= 2 queries in list_sessions, but got {queries_in_list_sessions}."
