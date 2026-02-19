import pytest
import os
from unittest.mock import patch
from deepresearch.core.session import SessionManager


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_history.db"
    return str(db_file)


def test_create_session(test_db):
    mgr = SessionManager(test_db)
    sid = mgr.create_session("v1_123", "Test prompt", ["file1.txt"])

    assert sid == 1
    session = mgr.get_session(1)
    assert session["interaction_id"] == "v1_123"
    assert session["prompt"] == "Test prompt"
    assert session["status"] == "running"
    assert "file1.txt" in session["files"]


def test_update_session(test_db):
    mgr = SessionManager(test_db)
    mgr.create_session("v1_123", "Test")

    mgr.update_session("v1_123", "completed", "Result Text")

    session = mgr.get_session("v1_123")
    assert session["status"] == "completed"
    assert session["result"] == "Result Text"


def test_list_sessions(test_db):
    mgr = SessionManager(test_db)
    mgr.create_session("v1_A", "Test A")
    import time

    time.sleep(0.1)
    mgr.create_session("v1_B", "Test B")

    sessions = mgr.list_sessions(limit=5)
    assert len(sessions) == 2
    assert sessions[0]["interaction_id"] == "v1_B"


def test_pid_tracking_alive(test_db):
    mgr = SessionManager(test_db)
    pid = os.getpid()
    mgr.create_session("v1_C", "Test PID", pid=pid)
    sessions = mgr.list_sessions()
    assert sessions[0]["status"] == "running"
    assert sessions[0]["pid"] == pid


def test_pid_tracking_dead(test_db):
    mgr = SessionManager(test_db)
    fake_pid = 99999
    with patch("os.kill", side_effect=OSError):
        mgr.create_session("v1_D", "Test Dead PID", pid=fake_pid)
        sessions = mgr.list_sessions()
    assert sessions[0]["status"] == "crashed"


def test_session_manager_coverage(test_db):
    mgr = SessionManager(test_db)
    # create sessions
    pid_parent = 1001
    sid_parent = mgr.create_session("p1", "parent", pid=pid_parent)
    sid_child1 = mgr.create_session("c1", "child1", parent_id=sid_parent)
    _ = mgr.create_session("c2", "child2", parent_id=sid_parent)

    # get_children
    children = mgr.get_children(sid_parent)
    assert len(children) == 2
    assert children[0]["interaction_id"] == "c1"

    # update_session_pid
    mgr.update_session_pid(sid_child1, 1002)
    assert mgr.get_session(sid_child1)["pid"] == 1002

    # update_session_interaction_id
    mgr.update_session_interaction_id(sid_child1, "c1_new")
    assert mgr.get_session(sid_child1)["interaction_id"] == "c1_new"

    # append_to_result
    mgr.update_session("p1", "completed", "initial")
    mgr.append_to_result("p1", "more stuff")
    assert "initial" in mgr.get_session("p1")["result"]
    assert "more stuff" in mgr.get_session("p1")["result"]

    # list_sessions dead checking via parent
    mgr.update_session("p1", "crashed")
    mgr.update_session("c2", "running")  # c2 has no pid, relies on parent

    with patch("os.kill", side_effect=OSError):
        sessions = mgr.list_sessions()
        for s in sessions:
            if s["interaction_id"] == "c2":
                assert s["status"] == "crashed"  # Parent is crashed, so child crashed

    # delete_session
    assert mgr.delete_session(sid_child1) is True
    assert mgr.delete_session("c2") is True
    assert mgr.get_session(sid_child1) is None
    assert mgr.get_session("c2") is None
