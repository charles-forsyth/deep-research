from unittest.mock import MagicMock, patch, mock_open
import pytest
from deepresearch import (
    SessionManager,
    DataExporter,
    FileManager,
    DeepResearchAgent,
    DeepResearchConfig,
    ResearchRequest,
    FollowUpRequest,
    detach_process
)

@pytest.fixture
def test_db(tmp_path):
    return str(tmp_path / "test_history.db")

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
    mgr.update_session("c2", "running") # c2 has no pid, relies on parent
    
    with patch("os.kill", side_effect=OSError):
        sessions = mgr.list_sessions()
        for s in sessions:
            if s["interaction_id"] == "c2":
                assert s["status"] == "crashed" # Parent is crashed, so child crashed

    # delete_session
    assert mgr.delete_session(sid_child1) is True
    assert mgr.delete_session("c2") is True
    assert mgr.get_session(sid_child1) is None
    assert mgr.get_session("c2") is None


def test_data_exporter_coverage():
    # save_csv
    with patch("builtins.open", mock_open()) as mock_file:
        DataExporter.save_csv("csv,data", "out.csv")
        mock_file.assert_called_with("out.csv", "w")

    # save_csv exception
    with patch("builtins.open", side_effect=Exception("Disk error")):
        DataExporter.save_csv("csv,data", "out.csv") # Should print error but not raise

    # export
    with (
        patch("deepresearch.DataExporter.save_json") as mock_json,
        patch("deepresearch.DataExporter.save_csv") as mock_csv,
        patch("builtins.open", mock_open()) as mock_file
    ):
        DataExporter.export("json_data", "file.json")
        mock_json.assert_called_once()
        
        DataExporter.export("csv_data", "file.csv")
        mock_csv.assert_called_once()
        
        DataExporter.export("text_data", "file.txt")
        mock_file.assert_called_with("file.txt", "w")


def test_file_manager_coverage():
    client = MagicMock()
    fm = FileManager(client)
    
    # invalid path
    with patch("os.path.isdir", return_value=False), patch("os.path.isfile", return_value=False):
        fm.create_store_from_paths(["invalid_path"])
        client.file_search_stores.create.assert_called_once()
        # Should skip invalid path
    
    # upload exception
    with patch("os.path.isdir", return_value=False), patch("os.path.isfile", return_value=True):
        client.file_search_stores.upload_to_file_search_store.side_effect = Exception("Upload error")
        with pytest.raises(Exception):
            fm.create_store_from_paths(["valid_path"])

    # cleanup exception
    fm.created_stores = ["store1"]
    client.file_search_stores.documents.list.side_effect = Exception("List error")
    client.file_search_stores.delete.side_effect = Exception("Delete error")
    fm.cleanup() # Should swallow exceptions


def test_deep_research_agent_error_coverage():
    config = DeepResearchConfig(api_key="fake")
    agent = DeepResearchAgent(config)
    agent.client = MagicMock()
    
    req = ResearchRequest(prompt="Test")
    
    # start_research_stream exception
    agent.client.interactions.create.side_effect = Exception("API Down")
    interaction_id = agent.start_research_stream(req)
    assert interaction_id is None
    
    # start_research_poll exception
    agent.client.interactions.create.side_effect = Exception("API Down")
    interaction_id = agent.start_research_poll(req)
    assert interaction_id is None # Or it might return MagicMock.id if it errored later, but here create fails
    
    # KeyboardInterrupt in stream
    agent.client.interactions.create.side_effect = KeyboardInterrupt()
    agent.start_research_stream(req) # Should handle and return None
    
    # KeyboardInterrupt in poll
    agent.client.interactions.create.side_effect = KeyboardInterrupt()
    agent.start_research_poll(req) # Should handle
    
    # follow_up exception
    agent.client.interactions.create.side_effect = Exception("API Down")
    agent.follow_up(FollowUpRequest(interaction_id="123", prompt="Test")) # Should handle

    # analyze_gaps exception
    agent.client.models.generate_content.side_effect = Exception("API Down")
    assert agent.analyze_gaps("prompt", "report") == []
    
    # synthesize_findings exception
    assert "ERROR: Synthesis failed" in agent.synthesize_findings("prompt", "main", ["sub"])

def test_detach_process():
    with (
        patch("subprocess.Popen") as mock_popen,
        patch("builtins.open", mock_open())
    ):
        mock_popen.return_value.pid = 9999
        pid = detach_process(["arg1"], "/tmp/log.txt")
        assert pid == 9999
