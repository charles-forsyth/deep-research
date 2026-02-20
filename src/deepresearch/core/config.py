import os
from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv

xdg_config_home = os.getenv(
    "XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config")
)
user_config_path = os.path.join(xdg_config_home, "deepresearch", ".env")
user_db_path = os.path.join(xdg_config_home, "deepresearch", "history.db")

# Load local .env if it exists, then fallback to user config
if os.path.exists(".env"):
    load_dotenv(".env")
load_dotenv(user_config_path)


class DeepResearchConfig(BaseModel):
    api_key: str = Field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY"), validate_default=True
    )
    agent_name: str = Field(
        default_factory=lambda: os.getenv(
            "GEMINI_AGENT_NAME", "deep-research-pro-preview-12-2025"
        )
    )
    followup_model: str = Field(
        default_factory=lambda: os.getenv(
            "GEMINI_FOLLOWUP_MODEL", "gemini-3-pro-preview"
        )
    )
    recursion_timeout: int = 600  # 10 minutes per child task

    @field_validator("api_key", mode="before")
    @classmethod
    def check_api_key(cls, v: str) -> str:
        if not v:
            raise ValueError(
                "GEMINI_API_KEY not found. Please set it in .env or ~/.config/deepresearch/.env"
            )
        return v
