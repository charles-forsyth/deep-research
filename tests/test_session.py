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


def test_list_sessions_n_plus_one_optimization(test_db):
    """
    Verify that list_sessions does not suffer from the N+1 query problem.
    This test will fail before the optimization is applied because the query count will be high.
    """
    mgr = SessionManager(test_db)

    # We don't need to create real sessions; we'll mock the DB return value.
    # This setup simulates 3 child sessions that need to look up their parent's status.
    mock_sessions_data = [
        {'id': 4, 'status': 'running', 'pid': None, 'parent_id': 1, 'prompt': 'c1'},
        {'id': 3, 'status': 'running', 'pid': None, 'parent_id': 1, 'prompt': 'c2'},
        {'id': 2, 'status': 'running', 'pid': None, 'parent_id': 1, 'prompt': 'c3'},
        {'id': 1, 'status': 'running', 'pid': 12345, 'parent_id': None, 'prompt': 'p1'},
    ]
    # This is the data that the N+1 lookups would return for the parent.
    mock_parent_data = {'pid': 12345, 'status': 'running'}

    with patch('sqlite3.connect') as mock_connect:
        mock_conn = mock_connect.return_value.__enter__.return_value
        mock_cursor = mock_conn.execute.return_value

        # The first query gets the list of all sessions.
        mock_cursor.fetchall.return_value = mock_sessions_data
        # The subsequent N+1 queries get the parent's status.
        mock_cursor.fetchone.return_value = mock_parent_data

        # Mock os.kill to avoid errors when checking the fake PID.
        with patch('os.kill'):
            mgr.list_sessions()

        # Before optimization, the query count is: 1 (for all sessions) + 3 (one for each child) = 4
        # After optimization, the query count will be: 1 (for all sessions) + 1 (for all parents) = 2
        # This assertion is for the OPTIMIZED case, so it will fail before the fix.
        assert mock_conn.execute.call_count == 2, "Should only make 2 queries (one for sessions, one for parents)"
