import pytest
from unittest.mock import MagicMock, patch
from rich.console import Console
from deep_research import DeepResearchAgent, DeepResearchConfig, ResearchRequest

@pytest.fixture
def mock_console():
    """Fixture to create a mock Console object."""
    return MagicMock(spec=Console)

def test_deep_research_agent_initialization(mock_console):
    """Test that the DeepResearchAgent initializes correctly."""
    with patch.dict('os.environ', {'GEMINI_API_KEY': 'test_key'}):
        agent = DeepResearchAgent(console=mock_console)
        assert agent.console == mock_console
        assert agent.file_manager.console == mock_console

def test_start_research_poll_returns_report(mock_console):
    """Test that start_research_poll returns the final report."""
    with patch.dict('os.environ', {'GEMINI_API_KEY': 'test_key'}):
        with patch('deep_research.DeepResearchAgent.start_research_poll', return_value="Final Report") as mock_poll:
            agent = DeepResearchAgent(console=mock_console)
            request = ResearchRequest(prompt="test")
            result = agent.start_research_poll(request)

            mock_poll.assert_called_once_with(request)
            assert result == "Final Report"

def test_start_research_stream_returns_report(mock_console):
    """Test that start_research_stream returns the final report."""
    with patch.dict('os.environ', {'GEMINI_API_KEY': 'test_key'}):
        with patch('deep_research.DeepResearchAgent.start_research_stream', return_value="Final Streamed Report") as mock_stream:
            agent = DeepResearchAgent(console=mock_console)
            request = ResearchRequest(prompt="test", verbose=True)
            result = agent.start_research_stream(request)

            mock_stream.assert_called_once_with(request)
            assert result == "Final Streamed Report"
