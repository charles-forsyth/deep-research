import os
from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv

xdg_config_home = os.getenv(
    "XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config")
)
user_config_path = os.path.join(xdg_config_home, "deepresearch", ".env")
user_db_path = os.path.join(xdg_config_home, "deepresearch", "history.db")

# Variables set in the real environment before any .env file was read.
SHELL_ENV = frozenset(os.environ)


def load_env_files() -> None:
    """Shell exports win, then the user settings file, then a ./.env in this folder.

    The user file (written by `auth login`) comes before ./.env because many project
    folders carry their own .env with an old GEMINI_API_KEY; letting it win made the
    CLI fail with API_KEY_INVALID depending on where it was run. A ./.env still
    supplies anything the user file does not set. load_dotenv never overrides a
    variable that is already set, so the first source wins.
    """
    load_dotenv(user_config_path)
    if os.path.exists(".env"):
        load_dotenv(".env")


load_env_files()


AGENT_STANDARD = "deep-research-preview-04-2026"
AGENT_MAX = "deep-research-max-preview-04-2026"


def agent_id(choice: str | None, default: str) -> str:
    """The Interactions agent for a run: "max" -> Deep Research Max, else the default."""
    return AGENT_MAX if (choice or "").lower() == "max" else default


class DeepResearchConfig(BaseModel):
    api_key: str = Field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY"), validate_default=True
    )
    agent_name: str = Field(
        default_factory=lambda: os.getenv(
            "GEMINI_AGENT_NAME", "deep-research-preview-04-2026"
        )
    )
    followup_model: str = Field(
        default_factory=lambda: os.getenv("GEMINI_FOLLOWUP_MODEL", "gemini-3.8-flash")
    )
    # Safety limit for one research task (root or child), in minutes. Runs are
    # not cut off early: a task only stops if it is still going after this long,
    # and then its Google interaction is cancelled so it stops billing.
    # 0 disables the limit.
    task_timeout_min: int = Field(
        default_factory=lambda: int(os.getenv("DR_TASK_TIMEOUT_MIN", "180")), ge=0
    )
    debug: bool = False

    @field_validator("api_key", mode="before")
    @classmethod
    def check_api_key(cls, v: str) -> str:
        if not v:
            raise ValueError(
                "GEMINI_API_KEY not found. Please set it in .env or ~/.config/deepresearch/.env"
            )
        return v


def service_env() -> dict[str, str]:
    """Environment for long-lived background processes (the dashboard).

    The CLI now loads the user settings first too (load_env_files), but a daemon
    must not depend on where it was started even for variables the user file lacks
    at import time, so here the user settings file is re-applied over anything a
    folder .env set; variables exported in the real shell still win over both.
    """
    from dotenv import dotenv_values

    env = dict(os.environ)
    if os.path.exists(user_config_path):
        for k, v in dotenv_values(user_config_path).items():
            if v is not None and k not in SHELL_ENV:
                env[k] = v
    return env
