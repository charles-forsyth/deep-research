from unittest.mock import MagicMock, patch
import pytest
import sys
from deep_research import main, DeepResearchAgent

@pytest.fixture
def mock_agent_class():
    with patch("deep_research.DeepResearchAgent") as mock_agent_cls:
        instance = mock_agent_cls.return_value
        instance.start_research_stream.return_value = "id_123"
        instance.start_research_poll.return_value = "id_123"
        yield mock_agent_cls

def test_main_research_stream(mock_agent_class):
    """Test 'research --stream' command invocation."""
    test_args = ["deep_research.py", "research", "Topic", "--stream"]
    with patch.object(sys, 'argv', test_args):
        main()
        
    mock_agent_class.assert_called_once()
    mock_agent_class.return_value.start_research_stream.assert_called_once()
    args = mock_agent_class.return_value.start_research_stream.call_args[0][0]
    assert args.prompt == "Topic"
    assert args.stream is True

def test_main_research_poll(mock_agent_class):
    """Test 'research' (polling) command invocation."""
    test_args = ["deep_research.py", "research", "Topic"]
    with patch.object(sys, 'argv', test_args):
        main()
        
    mock_agent_class.return_value.start_research_poll.assert_called_once()
    args = mock_agent_class.return_value.start_research_poll.call_args[0][0]
    assert args.stream is False

def test_main_followup(mock_agent_class):
    """Test 'followup' command invocation."""
    test_args = ["deep_research.py", "followup", "id_123", "Question"]
    with patch.object(sys, 'argv', test_args):
        main()
        
    mock_agent_class.return_value.follow_up.assert_called_once()
    args = mock_agent_class.return_value.follow_up.call_args[0][0]
    assert args.interaction_id == "id_123"
    assert args.prompt == "Question"

def test_main_upload_arg(mock_agent_class):
    """Test --upload argument parsing."""
    test_args = ["deep_research.py", "research", "Topic", "--upload", "file1.pdf", "file2.txt"]
    with patch.object(sys, 'argv', test_args):
        main()
        
    args = mock_agent_class.return_value.start_research_poll.call_args[0][0]
    assert args.upload_paths == ["file1.pdf", "file2.txt"]

def test_process_stream_output(capsys):
    """Test _process_stream prints correctly."""
    # We can test this by instantiating the real class with a mock client
    # but strictly testing the helper method
    agent = DeepResearchAgent(MagicMock())
    
    # Mock events
    event1 = MagicMock(event_type="content.delta")
    event1.delta.type = "text"
    event1.delta.text = "Hello "
    
    event2 = MagicMock(event_type="content.delta")
    event2.delta.type = "thought_summary"
    event2.delta.content.text = "Thinking..."
    
    stream = [event1, event2]
    
    agent._process_stream(stream, [None], [None], [False])
    
    captured = capsys.readouterr()
    assert "Hello " in captured.out
    assert "[THOUGHT] Thinking..." in captured.out

@patch("deep_research.SessionManager")
def test_main_smart_followup(mock_mgr_cls, mock_agent_class):
    """Test followup with numeric ID lookup."""
    # Setup mock manager
    mock_mgr = mock_mgr_cls.return_value
    # Mock return value to simulate DB lookup (sqlite3.Row behaves like dict)
    mock_mgr.get_session.return_value = {'interaction_id': 'v1_real_id'}
    
    test_args = ["deep_research.py", "followup", "5", "Question"]
    with patch.object(sys, 'argv', test_args):
        main()
    
    mock_mgr.get_session.assert_called_with("5")
    
    # Check that agent was called with the RESOLVED ID
    mock_agent_class.return_value.follow_up.assert_called_once()
    args = mock_agent_class.return_value.follow_up.call_args[0][0]
    assert args.interaction_id == "v1_real_id"

@patch("deep_research.detach_process")
@patch("deep_research.SessionManager")
def test_main_start_command(mock_mgr_cls, mock_detach):
    """Test 'start' (headless) command."""
    mock_mgr = mock_mgr_cls.return_value
    mock_mgr.create_session.return_value = 99
    
    test_args = ["deep_research.py", "start", "Topic", "--upload", "doc.pdf"]
    with patch.object(sys, 'argv', test_args):
        main()
        
    # Verify session created
    mock_mgr.create_session.assert_called_with("pending_start", "Topic", ["doc.pdf"])
    
    # Verify detach called with correct args
    mock_detach.assert_called_once()
    child_args, log_path = mock_detach.call_args[0]
    
    assert "research" in child_args
    assert "Topic" in child_args
    assert "--adopt-session" in child_args
    assert "99" in child_args
    assert "--upload" in child_args
    assert "doc.pdf" in child_args
    assert "session_99.log" in log_path
