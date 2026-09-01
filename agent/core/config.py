"""Configuration loaded from environment variables (12-factor)."""
from __future__ import annotations
import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    openrouter_api_key: str = ""
    agent_db_path: str = "input_data/access_snapshot.sqlite"
    agent_model_planner: str = "openai/gpt-4o-mini"
    agent_model_executor: str = "openai/gpt-4o-mini"
    agent_model_verifier: str = "openai/gpt-4.1-mini"
    agent_max_tool_calls: int = 15
    agent_max_wall_seconds: int = 90
    agent_run_store_dir: str = "runs"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    @property
    def db_path(self) -> Path:
        return Path(self.agent_db_path).resolve()

    @property
    def run_store_dir(self) -> Path:
        p = Path(self.agent_run_store_dir).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p


def get_settings() -> Settings:
    # allow token.txt fallback for local convenience only (never committed)
    s = Settings()
    if not s.openrouter_api_key:
        token_file = Path("token.txt")
        if token_file.exists():
            s.openrouter_api_key = token_file.read_text().strip()
    return s
