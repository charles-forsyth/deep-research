import pytest
from unittest.mock import patch, MagicMock
from deep_research import main

def test_main_handles_prompt():
    """Test that the main function correctly handles the prompt argument."""
    with patch('sys.argv', ['deep_research.py', 'test prompt']), \
         patch('deep_research.DeepResearchAgent') as mock_agent:
        main()
        # Check that the agent was initialized and called with the correct prompt
        mock_agent.assert_called_once()
        instance = mock_agent.return_value
        # Assuming the main logic calls a method like `start_research`
        # This part might need adjustment based on the actual implementation
        # For simplicity, we'll assume the request object is passed to a method
        # and we can inspect the call to that method.
        # Let's refine this to check the initiation of ResearchRequest inside the agent's methods

        # A better approach is to check the methods called on the instance
        # Let's assume a method `start_research_poll` is called for non-verbose execution
        instance.start_research_poll.assert_called_once()
        call_args = instance.start_research_poll.call_args
        request_arg = call_args[0][0]
        assert request_arg.prompt == 'test prompt'

def test_main_handles_verbose_flag():
    """Test that the --verbose flag is correctly handled."""
    with patch('sys.argv', ['deep_research.py', 'test prompt', '--verbose']), \
         patch('deep_research.DeepResearchAgent') as mock_agent:
        main()
        mock_agent.assert_called_once()
        instance = mock_agent.return_value
        instance.start_research_stream.assert_called_once()
        call_args = instance.start_research_stream.call_args
        request_arg = call_args[0][0]
        assert request_arg.verbose is True

def test_main_handles_output_flag():
    """Test that the --output flag is correctly handled."""
    with patch('sys.argv', ['deep_research.py', 'test prompt', '--output', 'report.md']), \
         patch('deep_research.DeepResearchAgent') as mock_agent:
        main()
        mock_agent.assert_called_once()
        instance = mock_agent.return_value
        instance.start_research_poll.assert_called_once()
        call_args = instance.start_research_poll.call_args
        request_arg = call_args[0][0]
        assert request_arg.output_file == 'report.md'
