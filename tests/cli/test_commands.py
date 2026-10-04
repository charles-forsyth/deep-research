from unittest.mock import patch, MagicMock
import pytest
from pydantic import ValidationError
from deepresearch.__main__ import main
from deepresearch.cli.commands import detach_process


@pytest.fixture
def mock_agent():
    with patch("deepresearch.cli.commands.DeepResearchAgent") as mock:
        yield mock


@pytest.fixture
def mock_session_manager():
    with patch("deepresearch.cli.commands.SessionManager") as mock:
        yield mock


@patch(
    "sys.argv", ["deepresearch", "start", "My Prompt", "--depth", "2", "--breadth", "3"]
)
@patch("deepresearch.cli.commands.detach_process", return_value=1234)
def test_main_start(mock_detach, mock_session_manager):
    mgr_instance = mock_session_manager.return_value
    mgr_instance.create_session.return_value = 10

    main()

    mgr_instance.create_session.assert_called_with("pending_start", "My Prompt", None)
    mock_detach.assert_called_once()
    mgr_instance.update_session_pid.assert_called_with(10, 1234)


@patch("sys.argv", ["deepresearch", "research", "My Prompt", "--depth", "2", "--quiet"])
def test_main_research_recursive(mock_agent):
    main()
    agent_instance = mock_agent.return_value
    agent_instance.start_recursive_research.assert_called_once()


@patch(
    "sys.argv",
    ["deepresearch", "research", "P", "--max", "--plan-id", "plan-abc12345", "--quiet"],
)
def test_main_research_max_and_plan_reach_the_request(mock_agent):
    """v0.61.0: --max and --plan-id end up on the ResearchRequest."""
    main()
    req = mock_agent.return_value.start_research_poll.call_args[0][0]
    assert req.agent == "max" and req.previous_interaction_id == "plan-abc12345"


@patch(
    "sys.argv",
    ["deepresearch", "start", "P", "--max", "--plan-id", "plan-abc12345"],
)
@patch("deepresearch.cli.commands.detach_process", return_value=1)
def test_main_start_passes_max_and_plan_to_the_child(mock_detach, mock_session_manager):
    mock_session_manager.return_value.create_session.return_value = 3
    main()
    child = mock_detach.call_args[0][0]
    assert "--max" in child and child[child.index("--plan-id") + 1] == "plan-abc12345"


@patch("sys.argv", ["deepresearch", "followup", "5", "Follow up prompt"])
def test_main_followup_numeric_id(mock_session_manager, mock_agent):
    mgr_instance = mock_session_manager.return_value
    mgr_instance.get_session.return_value = {"interaction_id": "real_id_123"}

    main()

    agent_instance = mock_agent.return_value
    agent_instance.follow_up.assert_called_once()
    assert agent_instance.follow_up.call_args[0][0].interaction_id == "real_id_123"


@patch("sys.argv", ["deepresearch", "list"])
def test_main_list(mock_session_manager):
    mgr_instance = mock_session_manager.return_value
    mgr_instance.list_sessions.return_value = [
        {
            "id": 1,
            "status": "completed",
            "created_at": "2023-01-01 10:00:00",
            "prompt": "test prompt",
        }
    ]
    main()
    mgr_instance.list_sessions.assert_called_once()


@patch("sys.argv", ["deepresearch", "show", "1", "--save", "out.html", "--recursive"])
def test_main_show_recursive_html(mock_session_manager):
    mgr_instance = mock_session_manager.return_value
    mgr_instance.get_session.return_value = {
        "id": 1,
        "depth": 1,
        "prompt": "test",
        "status": "completed",
        "result": "Markdown text",
        "interaction_id": "1",
        "created_at": "now",
        "files": "[]",
    }
    mgr_instance.get_children.return_value = []

    with patch("deepresearch.cli.commands.Console.save_html") as mock_save:
        main()
        mock_save.assert_called_once()


@patch("sys.argv", ["deepresearch", "delete", "1"])
def test_main_delete(mock_session_manager):
    mgr_instance = mock_session_manager.return_value
    mgr_instance.get_session.return_value = None  # not found: nothing deleted
    main()
    mgr_instance.get_session.assert_called_with("1")
    mgr_instance.delete_session.assert_not_called()


@patch("sys.argv", ["deepresearch", "cleanup", "--force"])
@patch("deepresearch.cli.commands.genai.Client")
@patch("deepresearch.cli.commands.DeepResearchConfig")
def test_main_cleanup(mock_config, mock_client, mock_session_manager):
    client_instance = mock_client.return_value
    store_mock = MagicMock()
    store_mock.name = "stores/123"
    client_instance.file_search_stores.list.return_value = [store_mock]

    main()
    client_instance.file_search_stores.delete.assert_called_with(name="stores/123")


def _store(name, display=None):
    m = MagicMock()
    m.name = name
    m.display_name = display
    return m


@patch("deepresearch.cli.commands._protected_stores", return_value={"stores/src-by-id"})
@patch("deepresearch.cli.commands.genai.Client")
@patch("deepresearch.cli.commands.DeepResearchConfig")
def test_cleanup_keeps_named_and_source_stores(
    mock_config, mock_client, _prot, mock_session_manager, monkeypatch
):
    monkeypatch.setattr("sys.argv", ["deepresearch", "cleanup", "--force"])
    c = mock_client.return_value
    c.file_search_stores.list.return_value = [
        _store("stores/temp", "deep-research-temp-1"),
        _store("stores/old"),
        _store("stores/src", "deep-research-source-ceph"),
        _store("stores/mine", "my team docs"),
        _store("stores/src-by-id"),
    ]
    main()
    deleted = {k.kwargs["name"] for k in c.file_search_stores.delete.call_args_list}
    assert deleted == {"stores/temp", "stores/old"}

    c.file_search_stores.delete.reset_mock()
    monkeypatch.setattr("sys.argv", ["deepresearch", "cleanup", "--force", "--all"])
    main()
    deleted = {k.kwargs["name"] for k in c.file_search_stores.delete.call_args_list}
    assert deleted == {
        "stores/temp",
        "stores/old",
        "stores/src",
        "stores/mine",
        "stores/src-by-id",
    }


@patch("sys.argv", ["deepresearch", "tree", "1"])
def test_main_tree_single(mock_session_manager):
    mgr_instance = mock_session_manager.return_value
    mgr_instance.get_session.return_value = {
        "id": 1,
        "depth": 1,
        "status": "running",
        "prompt": "test prompt",
    }
    mgr_instance.get_children.return_value = []

    main()
    mgr_instance.get_session.assert_called_with("1")


@patch("sys.argv", ["deepresearch", "auth", "logout"])
def test_main_auth_logout(tmp_path, monkeypatch):
    from deepresearch.cli import commands

    p = tmp_path / ".env"
    p.write_text("GEMINI_API_KEY=x\nOTHER=1\n")
    monkeypatch.setattr(commands, "user_config_path", str(p))
    main()
    assert p.read_text() == "OTHER=1\n"


@patch(
    "sys.argv",
    ["deepresearch", "estimate", "My prompt", "--depth", "2", "--breadth", "2"],
)
def test_main_estimate():
    main()
    # No exceptions should be thrown


@patch("sys.argv", ["deepresearch", "research", "My prompt"])
def test_main_validation_error():
    with patch(
        "deepresearch.cli.commands.ResearchRequest",
        side_effect=ValidationError.from_exception_data("error", []),
    ):
        main()  # Should catch ValidationError and print it


@patch("subprocess.Popen")
@patch("builtins.open")
def test_detach_process(mock_open, mock_popen):
    mock_popen.return_value.pid = 9999
    pid = detach_process(["arg1"], "/tmp/log.txt")
    assert pid == 9999
