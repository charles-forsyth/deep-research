import pytest
from pydantic import ValidationError
from deepresearch.core.config import DeepResearchConfig
from deepresearch.cli.base import ResearchRequest, FollowUpRequest


@pytest.fixture
def mock_env_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake_key")
    monkeypatch.delenv("GEMINI_AGENT_NAME", raising=False)
    monkeypatch.delenv("GEMINI_FOLLOWUP_MODEL", raising=False)


def test_config_initialization(mock_env_api_key):
    config = DeepResearchConfig()
    assert config.api_key == "fake_key"
    assert config.agent_name == "deep-research-preview-04-2026"


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


def test_service_env_prefers_user_file_over_folder_env(tmp_path, monkeypatch):
    """A ./.env loaded by the CLI must not replace the saved key for the dashboard."""
    from deepresearch.core import config

    user = tmp_path / "user.env"
    user.write_text("GEMINI_API_KEY=user-key\nDR_ONLY_IN_FILE=x\n")
    monkeypatch.setattr(config, "user_config_path", str(user))
    monkeypatch.setattr(config, "SHELL_ENV", frozenset({"PATH", "SHELL_SET"}))
    monkeypatch.setenv("GEMINI_API_KEY", "stale-folder-key")  # came from ./.env
    monkeypatch.setenv("SHELL_SET", "from-shell")
    env = config.service_env()
    assert env["GEMINI_API_KEY"] == "user-key"
    assert env["DR_ONLY_IN_FILE"] == "x"
    assert env["SHELL_SET"] == "from-shell"


def test_service_env_keeps_real_shell_export(tmp_path, monkeypatch):
    from deepresearch.core import config

    user = tmp_path / "user.env"
    user.write_text("GEMINI_API_KEY=user-key\n")
    monkeypatch.setattr(config, "user_config_path", str(user))
    monkeypatch.setattr(config, "SHELL_ENV", frozenset({"GEMINI_API_KEY"}))
    monkeypatch.setenv("GEMINI_API_KEY", "exported-in-shell")
    assert config.service_env()["GEMINI_API_KEY"] == "exported-in-shell"
