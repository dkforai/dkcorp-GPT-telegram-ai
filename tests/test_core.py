import json
import sqlite3
from pathlib import Path

from app.bot import _split_message
from app.database import Database, User
from app.knowledge import load_knowledge
from app.prompts import build_system_prompt
from app.role_profiles import load_role_profiles, resolve_communication_profile
from app.telegram_renderer import markdown_to_telegram_html


def test_user_sync_history_and_clear(tmp_path):
    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            [
                {
                    "telegram_id": 42,
                    "name": "DK",
                    "role": "Owner",
                    "division": "Management",
                    "communication_profile": "executive",
                    "custom_instruction": "Jawab strategis.",
                    "active": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    database = Database(tmp_path / "bot.db")
    database.initialize()
    assert database.sync_users(users_file) == 1
    assert database.get_user(42).name == "DK"
    assert database.get_user(42).communication_profile == "executive"

    database.add_message(42, "user", "Halo")
    database.add_message(42, "assistant", "Hai")
    assert database.get_history(42, 10) == [
        {"role": "user", "content": "Halo"},
        {"role": "assistant", "content": "Hai"},
    ]
    database.clear_history(42)
    assert database.get_history(42, 10) == []


def test_knowledge_limit_and_prompt(tmp_path):
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "brand.md").write_text("Brand voice: ramah", encoding="utf-8")
    knowledge = load_knowledge(knowledge_dir, 1000)
    assert "brand.md" in knowledge
    assert "Brand voice: ramah" in knowledge

    profiles_file = tmp_path / "role_profiles.json"
    profiles_file.write_text(
        json.dumps(
            {
                "default_profile": "default",
                "profiles": [
                    {
                        "id": "staff",
                        "label": "Staff",
                        "response_level": "Operasional",
                        "role_aliases": ["Creator"],
                        "focus": ["langkah"],
                        "default_structure": ["tujuan", "checklist"],
                        "avoid": ["abstrak"],
                    },
                    {
                        "id": "default",
                        "label": "Default",
                        "response_level": "Seimbang",
                        "role_aliases": [],
                        "focus": [],
                        "default_structure": [],
                        "avoid": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    profiles = load_role_profiles(profiles_file)
    user = User(
        telegram_id=1,
        name="Dyna",
        role="Creator",
        division="Marketing",
        communication_profile="",
        custom_instruction="Gunakan ide konkret.",
        active=True,
    )
    profile = resolve_communication_profile(
        user.communication_profile, user.role, profiles
    )
    prompt = build_system_prompt(user, knowledge, profile)
    assert "Dyna" in prompt
    assert "Gunakan ide konkret" in prompt
    assert "Communication profile: Staff" in prompt
    assert "tujuan → checklist" in prompt
    assert "<knowledge>" in prompt


def test_explicit_profile_overrides_role_alias():
    profiles = load_role_profiles(Path("config/role_profiles.json"))
    profile = resolve_communication_profile("manager", "Owner", profiles)
    assert profile.profile_id == "manager"


def test_existing_database_gets_communication_profile_migration(tmp_path):
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE users (
                telegram_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                division TEXT NOT NULL DEFAULT '',
                custom_instruction TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """
        )
    database = Database(database_path)
    database.initialize()
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
    assert "communication_profile" in columns


def test_split_message():
    chunks = _split_message("a" * 9000, size=4000)
    assert [len(chunk) for chunk in chunks] == [4000, 4000, 1000]


def test_telegram_renderer_formats_supported_markup():
    source = (
        "# Diagnosis\n\n"
        "**Conversion** turun dan *perlu dicek*.\n"
        "- Buka `/whoami`\n"
        "- Baca [panduan](https://example.com/guide?a=1&b=2)\n"
        "> Data belum cukup\n"
        "||Jawaban quiz||"
    )
    rendered = markdown_to_telegram_html(source)
    assert "<b>Diagnosis</b>" in rendered
    assert "<b>Conversion</b>" in rendered
    assert "<i>perlu dicek</i>" in rendered
    assert "• Buka <code>/whoami</code>" in rendered
    assert '<a href="https://example.com/guide?a=1&amp;b=2">panduan</a>' in rendered
    assert "<blockquote>Data belum cukup</blockquote>" in rendered
    assert "<tg-spoiler>Jawaban quiz</tg-spoiler>" in rendered


def test_telegram_renderer_escapes_model_html_and_code():
    source = "<b>raw</b> & aman\n```python\nif a < b:\n    print('&')\n```"
    rendered = markdown_to_telegram_html(source)
    assert "&lt;b&gt;raw&lt;/b&gt; &amp; aman" in rendered
    assert "<pre>if a &lt; b:\n    print('&amp;')</pre>" in rendered
    assert rendered.count("<b>") == 0
