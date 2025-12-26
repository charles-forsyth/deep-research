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


def test_child_pid_tracking_dead_parent(test_db):
    mgr = SessionManager(test_db)

    fake_parent_pid = 99998

    # 1. Create a parent session that will be marked as "dead"
    parent_id = mgr.create_session("v1_PARENT", "Parent", pid=fake_parent_pid)

    # 2. Create a child session that is "running" but has no PID itself
    # It relies on the parent's status.
    child_id = mgr.create_session("v1_CHILD", "Child", parent_id=parent_id)

    # Mock os.kill to be more robust: only fail for the specific fake parent PID.
    # This avoids issues with the order of sessions being processed.
    def kill_side_effect(pid, sig):
        if pid == fake_parent_pid:
            raise OSError
        # For any other PID, do nothing to avoid unintended side effects.
        return

    with patch("os.kill", side_effect=kill_side_effect):
        sessions = mgr.list_sessions()

    # Find the sessions from the result list
    parent_session = next((s for s in sessions if s['id'] == parent_id), None)
    child_session = next((s for s in sessions if s['id'] == child_id), None)

    assert parent_session is not None, "Parent session not found in results"
    assert child_session is not None, "Child session not found in results"

    # The parent should be crashed because its PID is dead
    assert parent_session['status'] == 'crashed'

    # The child should ALSO be marked as crashed because its parent is dead
    assert child_session['status'] == 'crashed'
