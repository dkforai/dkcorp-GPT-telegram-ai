from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    ai_provider: str
    ai_api_key: str
    ai_model: str
    ai_base_url: str | None
    database_path: Path
    users_file: Path
    companies_file: Path
    role_profiles_file: Path
    project_root: Path
    history_limit: int
    knowledge_max_chars: int
    max_response_chars: int
    log_level: str


def load_settings() -> Settings:
    load_dotenv()

    provider = os.getenv("AI_PROVIDER", "openai").strip().lower()
    if provider not in {"openai", "deepseek"}:
        raise ValueError("AI_PROVIDER harus 'openai' atau 'deepseek'")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    api_key = os.getenv("AI_API_KEY", "").strip()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN belum diisi")
    if not api_key:
        raise ValueError("AI_API_KEY belum diisi")

    default_model = "gpt-5.4-mini" if provider == "openai" else "deepseek-chat"
    configured_url = os.getenv("AI_BASE_URL", "").strip()
    default_url = None if provider == "openai" else "https://api.deepseek.com"

    return Settings(
        telegram_bot_token=token,
        ai_provider=provider,
        ai_api_key=api_key,
        ai_model=os.getenv("AI_MODEL", default_model).strip() or default_model,
        ai_base_url=configured_url or default_url,
        database_path=Path(os.getenv("DATABASE_PATH", "data/bot.db")),
        users_file=Path(os.getenv("USERS_FILE", "config/users.json")),
        companies_file=Path(os.getenv("COMPANIES_FILE", "config/companies.json")),
        role_profiles_file=Path(
            os.getenv("ROLE_PROFILES_FILE", "config/role_profiles.json")
        ),
        project_root=Path(os.getenv("PROJECT_ROOT", ".")),
        history_limit=max(0, int(os.getenv("HISTORY_LIMIT", "12"))),
        knowledge_max_chars=max(0, int(os.getenv("KNOWLEDGE_MAX_CHARS", "50000"))),
        max_response_chars=max(1, int(os.getenv("MAX_RESPONSE_CHARS", "12000"))),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
