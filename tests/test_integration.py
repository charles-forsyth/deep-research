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
