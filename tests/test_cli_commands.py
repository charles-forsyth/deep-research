from unittest.mock import patch, MagicMock
import pytest
from pydantic import ValidationError
from deepresearch import main


@pytest.fixture
def mock_agent():
    with patch("deepresearch.DeepResearchAgent") as mock:
        yield mock


@pytest.fixture
def mock_session_manager():
    with patch("deepresearch.SessionManager") as mock:
        yield mock


@patch(
    "sys.argv", ["deepresearch", "start", "My Prompt", "--depth", "2", "--breadth", "3"]
)
@patch("deepresearch.detach_process", return_value=1234)
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
    }
    mgr_instance.get_children.return_value = []

    with patch("rich.console.Console.save_html") as mock_save:
        main()
        mock_save.assert_called_once_with(
            "out.html", theme=mock_save.call_args[1].get("theme")
        )


@patch("sys.argv", ["deepresearch", "delete", "1"])
def test_main_delete(mock_session_manager):
    mgr_instance = mock_session_manager.return_value
    mgr_instance.delete_session.return_value = True
    main()
    mgr_instance.delete_session.assert_called_with("1")


@patch("sys.argv", ["deepresearch", "cleanup", "--force"])
@patch("deepresearch.genai.Client")
def test_main_cleanup(mock_client, mock_session_manager):
    client_instance = mock_client.return_value
    store_mock = MagicMock()
    store_mock.name = "stores/123"
    client_instance.file_search_stores.list.return_value = [store_mock]

    main()
    client_instance.file_search_stores.delete.assert_called_with(name="stores/123")


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
def test_main_auth_logout():
    with patch("os.path.exists", return_value=True), patch("os.remove") as mock_remove:
        main()
        mock_remove.assert_called_once()


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
        "deepresearch.ResearchRequest",
        side_effect=ValidationError.from_exception_data("error", []),
    ):
        main()  # Should catch ValidationError and print it
