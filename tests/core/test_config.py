import pytest
from pydantic import ValidationError
from deepresearch.core.config import DeepResearchConfig
from deepresearch.cli.base import ResearchRequest, FollowUpRequest


@pytest.fixture
def mock_env_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake_key")


def test_config_initialization(mock_env_api_key):
    config = DeepResearchConfig()
    assert config.api_key == "fake_key"
    assert config.agent_name == "deep-research-pro-preview-12-2025"


def test_config_missing_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValidationError, match="GEMINI_API_KEY not found"):
        DeepResearchConfig()


def test_research_request_validation():
    req = ResearchRequest(prompt="Test Prompt", stream=True)
    assert req.prompt == "Test Prompt"
    assert req.stream is True
    assert req.final_prompt == "Test Prompt"
    assert req.tools_config is None


def test_research_request_format_and_stores():
    req = ResearchRequest(
        prompt="Test Prompt", stores=["store1"], output_format="Technical"
    )
    assert "Format the output as follows: Technical" in req.final_prompt
    assert req.tools_config[0]["file_search_store_names"] == ["store1"]


def test_request_auto_format_json():
    req = ResearchRequest(prompt="test", output_file="data.json")
    assert "Output the final report as valid JSON" in req.final_prompt


def test_request_auto_format_csv():
    req = ResearchRequest(prompt="test", output_file="data.csv")
    assert "Output the final report as valid CSV" in req.final_prompt


def test_followup_request_validation():
    req = FollowUpRequest(interaction_id="123", prompt="More info")
    assert req.interaction_id == "123"
    assert req.prompt == "More info"


def test_followup_request_missing_field():
    with pytest.raises(ValidationError):
        FollowUpRequest(prompt="Just prompt")
