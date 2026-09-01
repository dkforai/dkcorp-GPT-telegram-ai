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
    max_concurrent_updates: int
    knowledge_max_chars: int
    max_response_chars: int
    log_level: str
    admin_username: str
    admin_password: str
    admin_session_secret: str
    admin_host: str
    admin_port: int
    admin_cookie_secure: bool

    @property
    def admin_enabled(self) -> bool:
        return bool(
            self.admin_username and self.admin_password and self.admin_session_secret
        )


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
    admin_username = os.getenv("ADMIN_USERNAME", "").strip()
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    admin_session_secret = os.getenv("ADMIN_SESSION_SECRET", "").strip()
    admin_values = (admin_username, admin_password, admin_session_secret)
    if any(admin_values) and not all(admin_values):
        raise ValueError(
            "ADMIN_USERNAME, ADMIN_PASSWORD, dan ADMIN_SESSION_SECRET harus diisi bersama"
        )
    if all(admin_values) and len(admin_password) < 12:
        raise ValueError("ADMIN_PASSWORD minimal 12 karakter")
    if all(admin_values) and len(admin_session_secret) < 32:
        raise ValueError("ADMIN_SESSION_SECRET minimal 32 karakter")

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
        max_concurrent_updates=max(
            1, min(16, int(os.getenv("MAX_CONCURRENT_UPDATES", "4")))
        ),
        knowledge_max_chars=max(0, int(os.getenv("KNOWLEDGE_MAX_CHARS", "50000"))),
        max_response_chars=max(1, int(os.getenv("MAX_RESPONSE_CHARS", "12000"))),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        admin_username=admin_username,
        admin_password=admin_password,
        admin_session_secret=admin_session_secret,
        admin_host=os.getenv("ADMIN_HOST", "0.0.0.0").strip() or "0.0.0.0",
        admin_port=max(1, int(os.getenv("PORT", os.getenv("ADMIN_PORT", "8080")))),
        admin_cookie_secure=_env_bool("ADMIN_COOKIE_SECURE", True),
    )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}
