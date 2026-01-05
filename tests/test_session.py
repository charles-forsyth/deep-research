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


def test_list_sessions_child_process_dead_parent(test_db):
    """
    Tests that a child session is marked 'crashed' if its running parent's PID is dead.
    This is the primary scenario that the N+1 optimization was written for.
    """
    mgr = SessionManager(test_db)
    fake_parent_pid = 99998

    # 1. Create a running parent with a fake (soon-to-be-dead) PID
    parent_id = mgr.create_session("parent_1", "Parent", pid=fake_parent_pid)

    # 2. Create a child session linked to the parent, with no PID of its own
    mgr.create_session("child_1", "Child", parent_id=parent_id)

    # 3. Create another healthy, unrelated session to ensure it is not affected
    mgr.create_session("healthy_1", "Healthy", pid=os.getpid())

    # 4. Mock os.kill to simulate ONLY the parent process not existing.
    # The mock will raise OSError for the parent's fake PID, and do nothing
    # for any other PID (like the healthy process), simulating a live process.
    def mock_kill(pid, sig):
        if pid == fake_parent_pid:
            raise OSError

    with patch("os.kill", side_effect=mock_kill):
        sessions = mgr.list_sessions()

    # 5. Verify the statuses
    sessions_by_id = {s['interaction_id']: s for s in sessions}

    # The parent, whose PID is dead, should be marked crashed
    assert sessions_by_id['parent_1']['status'] == 'crashed'

    # The child, whose parent is dead, should also be marked crashed
    assert sessions_by_id['child_1']['status'] == 'crashed'

    # The healthy process should remain running
    assert sessions_by_id['healthy_1']['status'] == 'running'
