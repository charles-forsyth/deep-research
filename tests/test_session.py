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


from unittest.mock import MagicMock


def test_list_sessions_avoids_n_plus_1_query(test_db):
    """
    Ensures that list_sessions is not making a query for each parent session inside the loop.
    This test is designed to FAIL before the optimization (expecting 3 calls)
    and PASS after the optimization (expecting 2 calls).
    """
    mgr = SessionManager(test_db)

    # We don't need to populate the real DB, we will mock the return values.
    # Patch os.kill to prevent it from raising OSError, which simplifies our test
    # by avoiding the 'crashed' status update logic and its extra DB calls.
    with patch('os.kill'), patch('deep_research.sqlite3.connect') as mock_connect:
        # Arrange: Mock the connection and cursor
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__.return_value = mock_conn

        # This is the list of sessions that the first `execute` call should return.
        mock_sessions_data = [
            # The code uses dict-like access, so a list of dicts is fine for mocking.
            {'id': 1, 'pid': 123, 'status': 'running', 'parent_id': None, 'prompt': 'p'},
            {'id': 2, 'pid': None, 'status': 'running', 'parent_id': 1, 'prompt': 'c1'},
            {'id': 3, 'pid': None, 'status': 'running', 'parent_id': 1, 'prompt': 'c2'},
        ]

        # This simulates the parent data fetched inside the loop (the N+1 query).
        # The code expects a dict-like row, so we'll mock it as such.
        mock_parent_data = {'pid': 123, 'status': 'running'}

        # The first call to execute() fetches all sessions.
        # Subsequent calls (the N+1) fetch parent data.
        # We can simulate this by changing the return value of the mock cursor.
        mock_cursor_list = MagicMock()
        mock_cursor_list.fetchall.return_value = mock_sessions_data

        mock_cursor_parent = MagicMock()
        mock_cursor_parent.fetchone.return_value = mock_parent_data

        mock_conn.execute.side_effect = [
            mock_cursor_list,    # For SELECT * FROM sessions...
            mock_cursor_parent,  # For SELECT ... WHERE id=? for child 1
            mock_cursor_parent,  # For SELECT ... WHERE id=? for child 2
        ]

        # Act: Call the method under test
        mgr.list_sessions()

        # Assert: Check how many times the database was queried.
        # Before optimization, we expect 3 SELECT queries.
        # After optimization, we expect only 2 SELECT queries.
        # We will assert that the number of calls is 2, so the test fails initially.
        # os.kill is patched to prevent side effects, so no 'crashed' status updates are expected,
        # which simplifies the test by removing UPDATE/COMMIT calls.
        assert mock_conn.execute.call_count == 2, "Expected 2 DB executes (optimized), but found more (likely N+1)."
