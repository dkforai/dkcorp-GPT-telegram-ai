import json
import sqlite3
from pathlib import Path

from app.bot import _split_message
from app.company_context import CompanyContent, load_company_content
from app.database import Company, Database, Membership, User
from app.knowledge import load_knowledge
from app.prompts import build_system_prompt
from app.role_profiles import load_role_profiles, resolve_communication_profile
from app.telegram_renderer import markdown_to_telegram_html


def test_user_sync_history_and_clear(tmp_path):
    companies_file = tmp_path / "companies.json"
    companies_file.write_text(
        json.dumps(
            [
                {
                    "id": "company-a",
                    "name": "Company A",
                    "profile_file": "companies/company-a/profile.md",
                    "instruction_file": "companies/company-a/instruction.md",
                    "knowledge_dir": "companies/company-a/knowledge",
                    "active": True,
                },
                {
                    "id": "company-b",
                    "name": "Company B",
                    "active": True,
                },
            ]
        ),
        encoding="utf-8",
    )
    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            [
                {
                    "telegram_id": 42,
                    "name": "DK",
                    "active": True,
                    "memberships": [
                        {
                            "company_id": "company-a",
                            "job_title": "Owner",
                            "division": "Management",
                            "role_level": "gm",
                            "communication_profile": "executive",
                            "custom_instruction": "Jawab strategis.",
                            "default": True,
                            "active": True,
                        },
                        {
                            "company_id": "company-b",
                            "job_title": "Advisor",
                            "communication_profile": "manager",
                            "active": True,
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    database = Database(tmp_path / "bot.db")
    database.initialize()
    assert database.sync_companies(companies_file) == 2
    assert database.sync_users(users_file) == 1
    assert database.get_user(42).name == "DK"
    membership = database.get_active_membership(42)
    assert membership.company_id == "company-a"
    assert membership.communication_profile == "executive"
    assert database.set_active_company(42, "company-b").company_name == "Company B"
    assert database.get_active_membership(42).company_id == "company-b"

    database.add_message(42, "user", "Legacy tanpa company")
    database.sync_users(users_file)
    assert database.get_history(42, 10, "company-a") == [
        {"role": "user", "content": "Legacy tanpa company"},
    ]

    database.add_message(42, "user", "Halo A", "company-a")
    database.add_message(42, "assistant", "Hai A", "company-a")
    database.add_message(42, "user", "Halo B", "company-b")
    assert database.get_history(42, 10, "company-a") == [
        {"role": "user", "content": "Legacy tanpa company"},
        {"role": "user", "content": "Halo A"},
        {"role": "assistant", "content": "Hai A"},
    ]
    assert database.get_history(42, 10, "company-b") == [
        {"role": "user", "content": "Halo B"},
    ]

    raw_users = json.loads(users_file.read_text(encoding="utf-8"))
    raw_users[0]["memberships"] = [raw_users[0]["memberships"][0]]
    users_file.write_text(json.dumps(raw_users), encoding="utf-8")
    database.sync_users(users_file)
    assert database.set_active_company(42, "company-b") is None
    database.clear_history(42, "company-a")
    assert database.get_history(42, 10, "company-a") == []
    assert database.get_history(42, 10, "company-b") == [
        {"role": "user", "content": "Halo B"},
    ]


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
        role="",
        division="",
        communication_profile="",
        custom_instruction="",
        active=True,
    )
    company = Company(
        company_id="amazing-malang",
        name="Amazing Malang",
        profile_file="",
        instruction_file="",
        knowledge_dir="",
        active=True,
    )
    membership = Membership(
        telegram_id=1,
        company_id="amazing-malang",
        company_name="Amazing Malang",
        job_title="Creator",
        division="Marketing",
        role_level="staff",
        communication_profile="",
        custom_instruction="Gunakan ide konkret.",
        is_default=True,
        active=True,
    )
    profile = resolve_communication_profile(
        membership.communication_profile, membership.job_title, profiles
    )
    content = CompanyContent(
        profile="Media lifestyle Malang",
        instruction="Utamakan akurasi lokasi.",
        knowledge=knowledge,
    )
    prompt = build_system_prompt(user, membership, company, content, profile)
    assert "Dyna" in prompt
    assert "Gunakan ide konkret" in prompt
    assert "Amazing Malang" in prompt
    assert "Utamakan akurasi lokasi" in prompt
    assert "Communication profile: Staff" in prompt
    assert "tujuan → checklist" in prompt
    assert '<knowledge company_id="amazing-malang">' in prompt


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


def test_existing_messages_get_company_scope_migration(tmp_path):
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE users (
                telegram_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                division TEXT NOT NULL DEFAULT '',
                communication_profile TEXT NOT NULL DEFAULT '',
                custom_instruction TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
    database = Database(database_path)
    database.initialize()
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
    assert "company_id" in columns


def test_company_content_is_scoped_to_configured_paths(tmp_path):
    content_dir = tmp_path / "companies" / "company-a"
    knowledge_dir = content_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    (content_dir / "profile.md").write_text("Profil A", encoding="utf-8")
    (content_dir / "instruction.md").write_text("Instruksi A", encoding="utf-8")
    (knowledge_dir / "facts.md").write_text("Fakta A", encoding="utf-8")
    company = Company(
        company_id="company-a",
        name="Company A",
        profile_file="companies/company-a/profile.md",
        instruction_file="companies/company-a/instruction.md",
        knowledge_dir="companies/company-a/knowledge",
        active=True,
    )
    content = load_company_content(company, tmp_path, 1000)
    assert content.profile == "Profil A"
    assert content.instruction == "Instruksi A"
    assert "Fakta A" in content.knowledge


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
