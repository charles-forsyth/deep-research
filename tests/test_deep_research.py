from unittest.mock import MagicMock, patch
import pytest
import os
from deep_research import DeepResearchAgent

@pytest.fixture
def mock_env_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake_key")

@pytest.fixture
def mock_genai_client():
    with patch("deep_research.genai.Client") as mock_client:
        yield mock_client

def test_agent_initialization(mock_env_api_key, mock_genai_client):
    """Test that the agent initializes correctly with an API key."""
    agent = DeepResearchAgent()
    assert agent.api_key == "fake_key"
    assert agent.agent_name == "deep-research-pro-preview-12-2025"
    mock_genai_client.assert_called_once_with(api_key="fake_key")

def test_agent_initialization_missing_key(monkeypatch):
    """Test that the agent raises an error if the API key is missing."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY not found"):
        DeepResearchAgent()
