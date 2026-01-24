import pytest
import os
from unittest.mock import patch
from deep_research import SessionManager
import sqlite3

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

class ConnectionProxyWithContext:
    """A proxy for sqlite3.Connection that counts queries and respects row_factory."""
    def __init__(self, db_path):
        self._conn = sqlite3.connect(db_path)
        self.select_count = 0

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    def execute(self, sql, *args, **kwargs):
        if sql.strip().upper().startswith("SELECT"):
            self.select_count += 1
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._conn.close()


def test_list_sessions_n_plus_one_problem(test_db):
    """
    This test verifies the N+1 query problem in list_sessions.
    It should fail before the fix by making too many SELECT queries,
    and pass after the fix with an optimized number of queries.
    """
    mgr = SessionManager(test_db)

    # Create a parent and 3 child sessions that are "running" but have a "dead" parent
    parent_id = mgr.create_session("parent_session", "Parent", pid=99998)
    mgr.create_session("child_1", "Child 1", parent_id=parent_id)
    mgr.create_session("child_2", "Child 2", parent_id=parent_id)
    mgr.create_session("child_3", "Child 3", parent_id=parent_id)

    # We need to use a proxy that is instantiated once and passed to the mock
    proxy = ConnectionProxyWithContext(test_db)

    with patch('deep_research.sqlite3.connect', return_value=proxy):
        # Mock os.kill to make the parent process appear dead
        with patch('os.kill', side_effect=OSError):
            mgr.list_sessions()

    # Before the fix, we expect 4 SELECT queries (1 for sessions + 3 for parents).
    # After the fix, we should only have 2 SELECT queries (1 for sessions + 1 for all parents).

    # This assertion will fail until the optimization is implemented.
    assert proxy.select_count == 2
