import json

from app.bot import _split_message
from app.database import Database, User
from app.knowledge import load_knowledge
from app.prompts import build_system_prompt


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

    user = User(1, "Dyna", "Creator", "Marketing", "Gunakan ide konkret.", True)
    prompt = build_system_prompt(user, knowledge)
    assert "Dyna" in prompt
    assert "Gunakan ide konkret" in prompt
    assert "<knowledge>" in prompt


def test_split_message():
    chunks = _split_message("a" * 9000, size=4000)
    assert [len(chunk) for chunk in chunks] == [4000, 4000, 1000]

