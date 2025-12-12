from unittest.mock import patch
import pytest
from deep_research import DeepResearchAgent, DeepResearchConfig, ResearchRequest, FollowUpRequest
from pydantic import ValidationError

@pytest.fixture
def mock_env_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake_key")

@pytest.fixture
def mock_genai_client():
    with patch("deep_research.genai.Client") as mock_client:
        yield mock_client

def test_config_initialization(mock_env_api_key):
    """Test that the config initializes correctly with an API key."""
    config = DeepResearchConfig()
    assert config.api_key == "fake_key"
    assert config.agent_name == "deep-research-pro-preview-12-2025"

def test_config_missing_key(monkeypatch):
    """Test that the config raises an error if the API key is missing."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY not found"):
        DeepResearchConfig()

def test_research_request_validation():
    """Test valid research request creation."""
    req = ResearchRequest(prompt="Test Prompt", stream=True)
    assert req.prompt == "Test Prompt"
    assert req.stream is True
    assert req.final_prompt == "Test Prompt"
    assert req.tools_config is None

def test_research_request_format_and_stores():
    """Test research request with format and stores."""
    req = ResearchRequest(
        prompt="Test Prompt", 
        stores=["store1"], 
        output_format="Technical"
    )
    assert "Format the output as follows: Technical" in req.final_prompt
    assert req.tools_config[0]["file_search_store_names"] == ["store1"]

def test_followup_request_validation():
    """Test valid follow-up request."""
    req = FollowUpRequest(interaction_id="123", prompt="More info")
    assert req.interaction_id == "123"
    assert req.prompt == "More info"

def test_followup_request_missing_field():
    """Test follow-up request validation failure."""
    with pytest.raises(ValidationError):
        FollowUpRequest(prompt="Just prompt")

def test_agent_initialization(mock_env_api_key, mock_genai_client):
    """Test that the agent initializes correctly with a config."""
    agent = DeepResearchAgent()
    assert agent.config.api_key == "fake_key"
    mock_genai_client.assert_called_once_with(api_key="fake_key")