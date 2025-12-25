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


def test_list_sessions_with_parent_child_logic(test_db):
    """
    Tests the N+1 query fix for parent/child status checks.
    """
    mgr = SessionManager(test_db)
    parent_pid = os.getpid()

    # 1. Create a running parent and a running child
    parent_id = mgr.create_session("parent_1", "Parent", pid=parent_pid)
    child_id = mgr.create_session("child_1", "Child", parent_id=parent_id)

    # With a live parent, child should be 'running'
    sessions = mgr.list_sessions()
    child_session = next(s for s in sessions if s['id'] == child_id)
    assert child_session['status'] == 'running'

    # 2. Test with a completed parent
    mgr.update_session("parent_1", "completed", "Parent finished")

    # After parent is complete, child should be marked as 'crashed'
    sessions = mgr.list_sessions()
    child_session = next(s for s in sessions if s['id'] == child_id)
    assert child_session['status'] == 'crashed'
