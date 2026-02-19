from unittest.mock import MagicMock, patch
import pytest
import os
from deepresearch.core.agent import DeepResearchAgent
from deepresearch.core.config import DeepResearchConfig
from deepresearch.cli.base import ResearchRequest, FollowUpRequest


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.file_search_stores.create.return_value.name = "stores/test-store"
    client.files.upload.return_value.name = "files/test-file"
    client.files.upload.return_value.state.name = "ACTIVE"
    return client


def test_agent_initialization(mock_client):
    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "fake_key"}),
        patch("deepresearch.core.agent.genai.Client") as mock_genai_client,
    ):
        agent = DeepResearchAgent()
        assert agent.config.api_key == "fake_key"
        mock_genai_client.assert_called_once_with(api_key="fake_key")


def test_agent_auto_upload_and_cleanup(mock_client):
    config = DeepResearchConfig(api_key="test")
    agent = DeepResearchAgent(config)
    agent.client = mock_client
    agent.file_manager = MagicMock()
    agent.file_manager.create_store_from_paths.return_value = "stores/temp-store"

    req = ResearchRequest(prompt="Base prompt", upload_paths=["doc.pdf"])

    mock_client.interactions = MagicMock()
    mock_client.interactions.create.return_value = []

    agent.start_research_stream(req)

    agent.file_manager.create_store_from_paths.assert_called_with(["doc.pdf"])
    call_args = mock_client.interactions.create.call_args
    assert (
        "IMPORTANT: You have access to a File Search Store" in call_args.kwargs["input"]
    )

    tools = call_args.kwargs["tools"]
    assert tools[0]["file_search_store_names"] == ["stores/temp-store"]
    agent.file_manager.cleanup.assert_called_once()


def test_recursive_research():
    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "fake_key"}),
        patch(
            "deepresearch.core.agent.DeepResearchAgent.start_research_poll"
        ) as mock_poll,
        patch(
            "deepresearch.core.agent.DeepResearchAgent.start_research_stream"
        ) as mock_stream,
        patch("deepresearch.core.agent.DeepResearchAgent.analyze_gaps") as mock_gaps,
        patch(
            "deepresearch.core.agent.DeepResearchAgent.synthesize_findings"
        ) as mock_synth,
        patch(
            "deepresearch.core.agent.SessionManager.create_session"
        ) as mock_create_session,
        patch("deepresearch.core.agent.SessionManager.get_session") as mock_get_session,
        patch("deepresearch.core.agent.SessionManager.update_session"),
    ):
        mock_poll.return_value = "interaction_child"
        mock_stream.return_value = "interaction_root"
        mock_gaps.return_value = ["Q1", "Q2"]
        mock_synth.return_value = "Final Report"
        mock_create_session.return_value = 100
        mock_get_session.return_value = {
            "status": "completed",
            "result": "Initial Report",
            "id": 1,
        }

        agent = DeepResearchAgent()
        req = ResearchRequest(prompt="Topic", depth=2)
        agent.start_recursive_research(req)

        assert mock_stream.call_count == 1
        assert mock_poll.call_count == 2
        mock_gaps.assert_called_once()
        mock_synth.assert_called_once()

        args = mock_synth.call_args
        assert args[0][0] == "Topic"
        assert args[0][1] == "Initial Report"
        assert len(args[0][2]) == 2


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
    assert interaction_id is None

    # KeyboardInterrupt in stream
    agent.client.interactions.create.side_effect = KeyboardInterrupt()
    agent.start_research_stream(req)

    # KeyboardInterrupt in poll
    agent.client.interactions.create.side_effect = KeyboardInterrupt()
    agent.start_research_poll(req)

    # follow_up exception
    agent.client.interactions.create.side_effect = Exception("API Down")
    agent.follow_up(FollowUpRequest(interaction_id="123", prompt="Test"))

    # analyze_gaps exception
    agent.client.models.generate_content.side_effect = Exception("API Down")
    assert agent.analyze_gaps("prompt", "report") == []

    # synthesize_findings exception
    assert "ERROR: Synthesis failed" in agent.synthesize_findings(
        "prompt", "main", ["sub"]
    )


def test_process_stream_output(capsys):
    agent = DeepResearchAgent(MagicMock())

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
