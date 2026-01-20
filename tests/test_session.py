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
    def __init__(self, conn):
        self._conn = conn
        self.execute_count = 0

    def execute(self, *args, **kwargs):
        self.execute_count += 1
        return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name in ['_conn', 'execute_count']:
            super().__setattr__(name, value)
        else:
            setattr(self._conn, name, value)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._conn.__exit__(exc_type, exc_val, exc_tb)

def test_list_sessions_avoids_n_plus_1(test_db):
    mgr = SessionManager(test_db)

    parent_id = mgr.create_session("parent_1", "Parent", pid=os.getpid())
    mgr.create_session("child_1", "Child 1", parent_id=parent_id)
    mgr.create_session("child_2", "Child 2", parent_id=parent_id)
    mgr.create_session("child_3", "Child 3", parent_id=parent_id)

    real_connect = sqlite3.connect
    proxy_instance = [None]

    def proxy_connect(db_path, *args, **kwargs):
        conn = real_connect(db_path, *args, **kwargs)
        proxy = ConnectionProxy(conn)
        proxy_instance[0] = proxy
        return proxy

    with patch('sqlite3.connect', new=proxy_connect):
        mgr_patched = SessionManager(test_db)
        mgr_patched.list_sessions()

        query_count = proxy_instance[0].execute_count
        # Baseline: 1 (list sessions)
        # N+1 would be: 1 (list) + 3 (parent checks) = 4
        # Optimized should be: 1 (list) + 1 (batch parent check) = 2
        assert query_count <= 2
