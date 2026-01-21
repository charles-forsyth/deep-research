import pytest
import os
import sqlite3
from unittest.mock import patch, MagicMock
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

def test_list_sessions_avoids_n_plus_1_queries(test_db):
    """
    Tests that list_sessions is efficient and doesn't make N+1 SELECT queries.
    The optimized version should make 2 SELECT queries:
    1. The initial list of sessions.
    2. A single query to get all relevant parent statuses.
    The un-optimized version would make 1 + N queries.
    """
    mgr = SessionManager(test_db)

    # Create 3 parent sessions that are "crashed"
    parent1 = mgr.create_session("parent1", "p1", pid=99991)
    parent2 = mgr.create_session("parent2", "p2", pid=99992)
    parent3 = mgr.create_session("parent3", "p3", pid=99993)

    # Create 5 child sessions that are running but their parents are dead
    mgr.create_session("child1", "c1", parent_id=parent1)
    mgr.create_session("child2", "c2", parent_id=parent1)
    mgr.create_session("child3", "c3", parent_id=parent2)
    mgr.create_session("child4", "c4", parent_id=parent3)
    mgr.create_session("child5", "c5", parent_id=parent3)

    select_query_count = 0
    original_connect = sqlite3.connect

    def mocked_connect(db_path):
        nonlocal select_query_count
        real_conn = original_connect(db_path)

        class ConnectionProxy:
            def __init__(self, conn):
                self.__dict__['_conn'] = conn

            def execute(self, *args, **kwargs):
                nonlocal select_query_count
                sql = args[0]
                if "SELECT" in sql.upper():
                    select_query_count += 1
                return self._conn.execute(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def __setattr__(self, name, value):
                setattr(self._conn, name, value)

            def __enter__(self):
                self._conn.__enter__()
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                return self._conn.__exit__(exc_type, exc_val, exc_tb)

        return ConnectionProxy(real_conn)

    with patch("sqlite3.connect", new=mocked_connect):
        # We create a new SessionManager to ensure its _init_db runs with our mock
        # This is important as PRAGMA statements could be miscounted if not handled.
        # Our check is a simple "SELECT in sql", so it won't count PRAGMA.
        mgr_under_test = SessionManager(test_db)
        select_query_count = 0 # Reset after initialization

        with patch("os.kill", side_effect=OSError): # Mock os.kill to make PIDs appear dead
            sessions = mgr_under_test.list_sessions(limit=10)

    # Assert all children are marked as crashed, confirming logic ran
    crashed_children = [s for s in sessions if s['status'] == 'crashed' and s['parent_id'] is not None]
    assert len(crashed_children) == 5

    # Un-optimized: 1 (initial) + 5 (parent lookups) = 6 SELECTs
    # Optimized: 1 (initial) + 1 (batch parent lookup) = 2 SELECTs
    assert select_query_count == 2, f"Expected 2 SELECT queries, but found {select_query_count}. N+1 problem likely."
