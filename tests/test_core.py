import asyncio
import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.admin import create_admin_app
from app.bot import InternalBot, _split_message
from app.company_context import CompanyContent, load_company_content
from app.config import Settings
from app.database import Company, Database, Membership, User
from app.knowledge import load_knowledge
from app.prompts import build_system_prompt
from app.role_profiles import load_role_profiles, resolve_communication_profile
from app.telegram_renderer import markdown_to_telegram_html


def _test_settings(
    tmp_path: Path,
    users_file: Path,
    companies_file: Path,
    **overrides,
) -> Settings:
    values = {
        "telegram_bot_token": "test-token",
        "ai_provider": "openai",
        "ai_api_key": "test-key",
        "ai_model": "test-model",
        "ai_base_url": None,
        "database_path": tmp_path / "test.db",
        "users_file": users_file,
        "companies_file": companies_file,
        "role_profiles_file": Path("config/role_profiles.json"),
        "project_root": Path("."),
        "history_limit": 12,
        "max_concurrent_updates": 4,
        "knowledge_max_chars": 50000,
        "max_response_chars": 12000,
        "log_level": "INFO",
        "admin_username": "admin",
        "admin_password": "strong-password",
        "admin_session_secret": "x" * 32,
        "admin_host": "127.0.0.1",
        "admin_port": 8080,
        "admin_cookie_secure": False,
    }
    values.update(overrides)
    return Settings(**values)


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


def test_admin_requires_login_and_renders_database(tmp_path):
    companies_file = tmp_path / "companies.json"
    companies_file.write_text(
        json.dumps([{"id": "company-a", "name": "Company A"}]),
        encoding="utf-8",
    )
    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            [
                {
                    "telegram_id": 42,
                    "name": "DK",
                    "memberships": [
                        {
                            "company_id": "company-a",
                            "job_title": "Owner",
                            "default": True,
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    database = Database(tmp_path / "admin.db")
    database.initialize()
    database.sync_companies(companies_file)
    database.sync_users(users_file)
    settings = _test_settings(
        tmp_path,
        users_file,
        companies_file,
        database_path=tmp_path / "admin.db",
    )
    app = create_admin_app(settings, database)
    with TestClient(app) as client:
        response = client.get("/admin", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"

        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "wrong-password"},
        )
        assert response.status_code == 401

        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "strong-password"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        response = client.get("/admin")
        assert response.status_code == 200
        assert "Company A" in response.text
        assert "Database transition mode" in response.text
        assert 'href="/admin/static/admin.css"' in response.text

        response = client.get("/admin/static/admin.css")
        assert response.status_code == 200
        assert "--sidebar" in response.text

        response = client.get("/admin/users")
        assert response.status_code == 200
        assert "DK" in response.text
        assert "Owner" in response.text

        response = client.get("/admin/companies")
        csrf = re.search(r'name="csrf_token" value="([a-f0-9]+)"', response.text)
        assert csrf is not None
        response = client.post(
            "/admin/companies/company-a/status",
            data={"csrf_token": csrf.group(1), "active": "0"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "membership+aktif" in response.headers["location"]
        assert database.get_company_admin("company-a").active is True


def test_admin_company_management_and_csrf(tmp_path):
    companies_file = tmp_path / "companies.json"
    companies_file.write_text(
        json.dumps([{"id": "company-a", "name": "Company A"}]),
        encoding="utf-8",
    )
    users_file = tmp_path / "users.json"
    users_file.write_text("[]", encoding="utf-8")
    database = Database(tmp_path / "company-admin.db")
    database.initialize()
    assert database.bootstrap_companies(companies_file) == 1
    assert database.bootstrap_companies(companies_file) == 0

    settings = _test_settings(
        tmp_path,
        users_file,
        companies_file,
        database_path=tmp_path / "company-admin.db",
    )
    app = create_admin_app(settings, database)
    with TestClient(app) as client:
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "strong-password"},
            follow_redirects=False,
        )
        assert response.status_code == 303

        response = client.get("/admin/companies/new")
        assert response.status_code == 200
        csrf = re.search(r'name="csrf_token" value="([a-f0-9]+)"', response.text)
        assert csrf is not None
        csrf_token = csrf.group(1)

        response = client.post(
            "/admin/companies",
            data={"company_id": "company-b", "name": "Company B", "active": "1"},
        )
        assert response.status_code == 403

        response = client.post(
            "/admin/companies",
            data={
                "csrf_token": csrf_token,
                "company_id": "company-b",
                "name": "Company B",
                "active": "1",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        company = database.get_company_admin("company-b")
        assert company is not None
        assert company.profile_file == "companies/company-b/profile.md"
        assert company.instruction_file == "companies/company-b/instruction.md"
        assert company.knowledge_dir == "companies/company-b/knowledge"

        response = client.post(
            "/admin/companies/company-b",
            data={"csrf_token": csrf_token, "name": "Company B Updated"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert database.get_company_admin("company-b").name == "Company B Updated"

        response = client.post(
            "/admin/companies/company-b/status",
            data={"csrf_token": csrf_token, "active": "0"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert database.get_company_admin("company-b").active is False

    actions = [row["action"] for row in database.list_admin_audit_events()]
    assert actions == [
        "company.deactivated",
        "company.updated",
        "company.created",
    ]

    companies_file.write_text(
        json.dumps([{"id": "company-a", "name": "Config Name Changed"}]),
        encoding="utf-8",
    )
    assert database.bootstrap_companies(companies_file) == 0
    assert database.get_company_admin("company-a").name == "Company A"


def test_admin_user_and_membership_management(tmp_path):
    companies_file = tmp_path / "companies.json"
    companies_file.write_text(
        json.dumps(
            [
                {"id": "company-a", "name": "Company A"},
                {"id": "company-b", "name": "Company B"},
            ]
        ),
        encoding="utf-8",
    )
    users_file = tmp_path / "users.json"
    users_file.write_text("[]", encoding="utf-8")
    database = Database(tmp_path / "user-admin.db")
    database.initialize()
    database.bootstrap_companies(companies_file)
    settings = _test_settings(
        tmp_path,
        users_file,
        companies_file,
        database_path=tmp_path / "user-admin.db",
    )
    app = create_admin_app(settings, database)
    with TestClient(app) as client:
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "strong-password"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        response = client.get("/admin/users/new")
        assert response.status_code == 200
        csrf = re.search(r'name="csrf_token" value="([a-f0-9]+)"', response.text)
        assert csrf is not None
        csrf_token = csrf.group(1)

        user_payload = {
            "telegram_id": "123456789",
            "name": "Dyna",
            "active": "1",
            "company_id": "company-a",
            "job_title": "Content Creator",
            "division": "Marketing",
            "role_level": "staff",
            "communication_profile": "staff",
            "custom_instruction": "Jawab praktis.",
        }
        response = client.post("/admin/users", data=user_payload)
        assert response.status_code == 403
        response = client.post(
            "/admin/users",
            data={"csrf_token": csrf_token, **user_payload},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert database.get_user(123456789).name == "Dyna"
        membership_a = database.get_membership_admin(123456789, "company-a")
        assert membership_a is not None
        assert membership_a.is_default is True
        assert membership_a.communication_profile == "staff"

        response = client.get("/admin/users/123456789/edit")
        assert response.status_code == 200
        assert "Company A" in response.text
        response = client.get("/admin/users/123456789/memberships/new")
        assert response.status_code == 200
        assert "Company B" in response.text

        membership_b_payload = {
            "csrf_token": csrf_token,
            "company_id": "company-b",
            "job_title": "Project Lead",
            "division": "Growth",
            "role_level": "manager",
            "communication_profile": "manager",
            "custom_instruction": "Fokus KPI.",
            "is_default": "1",
        }
        response = client.post(
            "/admin/users/123456789/memberships",
            data=membership_b_payload,
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert database.get_membership_admin(
            123456789, "company-b"
        ).is_default is True
        assert database.get_membership_admin(
            123456789, "company-a"
        ).is_default is False
        response = client.get(
            "/admin/users/123456789/memberships/company-b/edit"
        )
        assert response.status_code == 200
        assert "Project Lead" in response.text

        response = client.post(
            "/admin/users/123456789/memberships/company-b/status",
            data={"csrf_token": csrf_token, "active": "0"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "membership+lain" in response.headers["location"]
        assert database.get_membership_admin(123456789, "company-b").active is True

        response = client.post(
            "/admin/users/123456789/memberships/company-a",
            data={
                "csrf_token": csrf_token,
                "job_title": "Content Creator",
                "division": "Marketing",
                "role_level": "staff",
                "communication_profile": "staff",
                "custom_instruction": "Jawab praktis.",
                "is_default": "1",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert database.get_membership_admin(
            123456789, "company-a"
        ).is_default is True

        response = client.post(
            "/admin/users/123456789/memberships/company-b/status",
            data={"csrf_token": csrf_token, "active": "0"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert database.get_membership_admin(123456789, "company-b").active is False

        response = client.post(
            "/admin/users/123456789",
            data={"csrf_token": csrf_token, "name": "Dyna Updated"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert database.get_user_admin(123456789).name == "Dyna Updated"

        response = client.post(
            "/admin/users/123456789/status",
            data={"csrf_token": csrf_token, "active": "0"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert database.get_user(123456789) is None
        assert database.get_user_admin(123456789).active is False

    actions = [row["action"] for row in database.list_admin_audit_events()]
    assert actions[:7] == [
        "user.deactivated",
        "user.updated",
        "membership.deactivated",
        "membership.updated",
        "membership.created",
        "membership.created",
        "user.created",
    ]


def test_bot_serializes_same_user_and_allows_different_users(tmp_path):
    companies_file = tmp_path / "companies.json"
    companies_file.write_text(
        json.dumps([{"id": "company-a", "name": "Company A"}]),
        encoding="utf-8",
    )
    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            [
                {
                    "telegram_id": 42,
                    "name": "User A",
                    "memberships": [{"company_id": "company-a", "default": True}],
                },
                {
                    "telegram_id": 43,
                    "name": "User B",
                    "memberships": [{"company_id": "company-a", "default": True}],
                },
            ]
        ),
        encoding="utf-8",
    )
    database = Database(tmp_path / "concurrency.db")
    database.initialize()
    database.sync_companies(companies_file)
    database.sync_users(users_file)
    settings = _test_settings(tmp_path, users_file, companies_file)
    bot = InternalBot(settings, database, SimpleNamespace())

    async def measure(user_ids: list[int]) -> int:
        active = 0
        max_active = 0

        async def worker(user_id: int) -> None:
            nonlocal active, max_active
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=user_id),
                effective_message=None,
            )
            async with bot._serialized_user(update) as user:
                assert user is not None
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0.02)
                active -= 1

        await asyncio.gather(*(worker(user_id) for user_id in user_ids))
        return max_active

    async def scenario() -> tuple[int, int]:
        return await measure([42, 42]), await measure([42, 43])

    same_user_max, different_user_max = asyncio.run(scenario())
    assert same_user_max == 1
    assert different_user_max == 2

    application = bot.build_application()
    assert application.update_processor.max_concurrent_updates == 4


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
