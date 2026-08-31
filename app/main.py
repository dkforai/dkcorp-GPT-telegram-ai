from __future__ import annotations

import logging

from app.bot import InternalBot
from app.config import load_settings
from app.database import Database
from app.providers import create_provider
from app.role_profiles import load_role_profiles


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Telegram Bot API embeds the bot token in the request URL. Keep HTTP client
    # logs above INFO so credentials are never printed during normal operation.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    database = Database(settings.database_path)
    database.initialize()
    synced = database.sync_users(settings.users_file)
    logging.getLogger(__name__).info("Sinkronisasi %d user dari konfigurasi", synced)
    profiles = load_role_profiles(settings.role_profiles_file)
    logging.getLogger(__name__).info(
        "Memuat %d communication profile", len(profiles.by_id)
    )

    provider = create_provider(
        settings.ai_provider,
        settings.ai_api_key,
        settings.ai_model,
        settings.ai_base_url,
    )
    application = InternalBot(settings, database, provider).build_application()
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
