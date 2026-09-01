import asyncio
import json
import re
import sqlite3
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.admin import create_admin_app
from app.bot import InternalBot, _split_message
from app.company_context import CompanyContent, load_company_content
from app.config import Settings
from app.database import AIModule, Company, Database, Membership, User
from app.document_ingestion import extract_uploaded_document
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

    module = AIModule(
        company_id="amazing-malang",
        company_name="Amazing Malang",
        module_id="marketing",
        name="Marketing",
        description="Perencanaan kampanye",
        active=True,
    )
    module_prompt = build_system_prompt(
        user,
        membership,
        company,
        content,
        profile,
        module,
        "Gunakan funnel awareness sampai conversion.",
    )
    assert 'module_id="marketing"' in module_prompt
    assert "Gunakan funnel awareness sampai conversion." in module_prompt


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
        connection.execute(
            """
            CREATE TABLE user_sessions (
                telegram_id INTEGER PRIMARY KEY,
                active_company_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
    database = Database(database_path)
    database.initialize()
    with sqlite3.connect(database_path) as connection:
        message_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        session_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(user_sessions)"
            ).fetchall()
        }
    assert "company_id" in message_columns
    assert "module_id" in message_columns
    assert "active_module_id" in session_columns


def test_module_access_publish_and_history_are_scoped(tmp_path):
    database = Database(tmp_path / "modules.db")
    database.initialize()
    database.create_company("company-a", "Company A", actor="admin")
    database.create_user_with_membership(
        42,
        "DK",
        "company-a",
        "Owner",
        "Management",
        "gm",
        "executive",
        "",
        actor="admin",
    )
    first = database.create_module(
        "company-a", "", "Marketing", "Kelola campaign", actor="admin"
    )
    second = database.create_module(
        "company-a", "", "Marketing", "Module kedua", actor="admin"
    )
    assert first.module_id == "marketing"
    assert second.module_id == "marketing-2"

    database.set_membership_module_access(
        42, "company-a", [first.module_id], actor="admin"
    )
    assert database.list_accessible_modules(42, "company-a") == []
    database.save_module_playbook_draft(
        "company-a", first.module_id, "", actor="admin"
    )
    try:
        database.publish_module_playbook(
            "company-a", first.module_id, actor="admin"
        )
        assert False, "Playbook kosong seharusnya tidak dapat dipublikasikan"
    except ValueError as exc:
        assert "tidak boleh kosong" in str(exc)
    database.save_module_playbook_draft(
        "company-a", first.module_id, "Playbook marketing v1", actor="admin"
    )
    assert database.list_accessible_modules(42, "company-a") == []
    assert database.publish_module_playbook(
        "company-a", first.module_id, actor="admin"
    ) == 1
    accessible = database.list_accessible_modules(42, "company-a")
    assert [module.module_id for module in accessible] == ["marketing"]
    assert database.set_active_module(42, "marketing") == first

    replies: list[str] = []

    async def reply_text(value: str, **_kwargs) -> None:
        replies.append(value)

    settings = _test_settings(
        tmp_path,
        tmp_path / "users.json",
        tmp_path / "companies.json",
        database_path=tmp_path / "modules.db",
    )
    bot = InternalBot(settings, database, SimpleNamespace())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    asyncio.run(bot.module(update, SimpleNamespace(args=["general"])))
    assert database.get_active_module(42, "company-a") is None
    assert "General" in replies[-1]
    asyncio.run(bot.module(update, SimpleNamespace(args=["marketing"])))
    assert database.get_active_module(42, "company-a") == first
    assert "Marketing" in replies[-1]

    database.add_message(42, "user", "General", "company-a", "")
    database.add_message(42, "user", "Marketing", "company-a", "marketing")
    assert database.get_history(42, 10, "company-a", "") == [
        {"role": "user", "content": "General"}
    ]
    assert database.get_history(42, 10, "company-a", "marketing") == [
        {"role": "user", "content": "Marketing"}
    ]

    database.save_module_playbook_draft(
        "company-a", "marketing", "Playbook marketing v2", actor="admin"
    )
    assert database.get_published_module_playbook(
        "company-a", "marketing"
    ) == "Playbook marketing v1"
    assert database.publish_module_playbook(
        "company-a", "marketing", actor="admin"
    ) == 2
    versions = database.list_module_playbook_versions("company-a", "marketing")
    database.restore_module_playbook_version_to_draft(
        "company-a", "marketing", int(versions[1]["id"]), actor="admin"
    )
    state = database.get_module_playbook_admin("company-a", "marketing")
    assert state is not None
    assert state["draft_content"] == "Playbook marketing v1"
    assert state["published_content"] == "Playbook marketing v2"

    database.set_membership_module_access(42, "company-a", [], actor="admin")
    assert database.get_active_module(42, "company-a") is None
    assert database.set_active_module(42, "marketing") is None


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
        assert 'name="company_id"' not in response.text
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
                "company_id": "id-yang-dipalsukan",
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


def test_server_generated_identifiers_and_collision_suffixes(tmp_path):
    database = Database(tmp_path / "generated-identifiers.db")
    database.initialize()

    first_company = database.create_company(
        "", "Amazing Malang", actor="admin"
    )
    second_company = database.create_company(
        "", "Amazing Malang", actor="admin"
    )
    assert first_company.company_id == "amazing-malang"
    assert second_company.company_id == "amazing-malang-2"

    first_document = database.create_knowledge_document(
        first_company.company_id,
        "",
        "Target Omzet 2026",
        "Target pertama.",
        actor="admin",
    )
    second_document = database.create_knowledge_document(
        first_company.company_id,
        "",
        "Target Omzet 2026",
        "Target kedua.",
        actor="admin",
    )
    assert first_document["document_key"] == "target-omzet-2026"
    assert second_document["document_key"] == "target-omzet-2026-2"


def test_admin_activity_is_read_only_filterable_and_content_safe(tmp_path):
    companies_file = tmp_path / "companies.json"
    companies_file.write_text("[]", encoding="utf-8")
    users_file = tmp_path / "users.json"
    users_file.write_text("[]", encoding="utf-8")
    database = Database(tmp_path / "activity.db")
    database.initialize()
    database.create_company("company-a", "Company A", actor="admin")
    database.create_knowledge_document(
        "company-a",
        "private-plan",
        "Private Plan",
        "RAHASIA-ISI-DOKUMEN",
        actor="admin",
    )
    database.publish_knowledge_document(
        "company-a", "private-plan", actor="admin"
    )
    settings = _test_settings(
        tmp_path,
        users_file,
        companies_file,
        database_path=tmp_path / "activity.db",
    )
    app = create_admin_app(settings, database)

    with TestClient(app) as client:
        assert client.get("/admin/activity", follow_redirects=False).status_code == 303
        assert client.post(
            "/admin/login",
            data={"username": "admin", "password": "strong-password"},
            follow_redirects=False,
        ).status_code == 303
        response = client.get("/admin/activity")
        assert response.status_code == 200
        assert "Audit aktivitas" in response.text
        assert "Knowledge dipublikasikan" in response.text
        assert "company-a:private-plan" in response.text
        assert "RAHASIA-ISI-DOKUMEN" not in response.text
        assert "WIB" in response.text

        company_only = client.get("/admin/activity?category=company")
        assert company_only.status_code == 200
        assert "Company dibuat" in company_only.text
        assert "Knowledge dipublikasikan" not in company_only.text


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


def test_company_instruction_draft_publish_and_runtime_override(tmp_path):
    companies_file = tmp_path / "companies.json"
    instruction_file = tmp_path / "companies" / "company-a" / "instruction.md"
    instruction_file.parent.mkdir(parents=True)
    instruction_file.write_text("Instruksi file lama", encoding="utf-8")
    companies_file.write_text(
        json.dumps(
            [
                {
                    "id": "company-a",
                    "name": "Company A",
                    "instruction_file": "companies/company-a/instruction.md",
                }
            ]
        ),
        encoding="utf-8",
    )
    database = Database(tmp_path / "instruction.db")
    database.initialize()
    database.bootstrap_companies(companies_file)
    company = database.get_company_admin("company-a")
    assert company is not None
    assert database.get_published_company_instruction("company-a") is None
    content = load_company_content(company, tmp_path, 1000)
    assert content.instruction == "Instruksi file lama"

    database.save_company_instruction_draft(
        "company-a", "Instruksi database v1", actor="admin"
    )
    assert database.get_published_company_instruction("company-a") is None
    assert database.publish_company_instruction("company-a", actor="admin") == 1
    published = database.get_published_company_instruction("company-a")
    assert published == "Instruksi database v1"
    content = load_company_content(company, tmp_path, 1000, published)
    assert content.instruction == "Instruksi database v1"

    database.save_company_instruction_draft(
        "company-a", "Instruksi database v2", actor="admin"
    )
    assert database.get_published_company_instruction("company-a") == (
        "Instruksi database v1"
    )
    assert database.publish_company_instruction("company-a", actor="admin") == 2
    versions = database.list_company_instruction_versions("company-a")
    assert [row["version_number"] for row in versions] == [2, 1]
    database.restore_company_instruction_version_to_draft(
        "company-a", versions[1]["id"], actor="admin"
    )
    state = database.get_company_instruction_admin("company-a")
    assert state is not None
    assert state["draft_content"] == "Instruksi database v1"
    assert state["published_content"] == "Instruksi database v2"


def test_admin_company_instruction_workflow(tmp_path):
    companies_file = tmp_path / "companies.json"
    companies_file.write_text(
        json.dumps([{"id": "company-a", "name": "Company A"}]),
        encoding="utf-8",
    )
    users_file = tmp_path / "users.json"
    users_file.write_text("[]", encoding="utf-8")
    database = Database(tmp_path / "instruction-admin.db")
    database.initialize()
    database.bootstrap_companies(companies_file)
    settings = _test_settings(
        tmp_path,
        users_file,
        companies_file,
        database_path=tmp_path / "instruction-admin.db",
        project_root=Path("."),
    )
    app = create_admin_app(settings, database)
    with TestClient(app) as client:
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "strong-password"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        response = client.get("/admin/instructions")
        assert response.status_code == 200
        assert "Company A" in response.text

        response = client.get("/admin/instructions/company-a")
        assert response.status_code == 200
        csrf = re.search(r'name="csrf_token" value="([a-f0-9]+)"', response.text)
        assert csrf is not None
        csrf_token = csrf.group(1)

        response = client.post(
            "/admin/instructions/company-a/draft",
            data={"content": "Jawab sesuai kebijakan Company A."},
        )
        assert response.status_code == 403
        response = client.post(
            "/admin/instructions/company-a/draft",
            data={
                "csrf_token": csrf_token,
                "content": "Jawab sesuai kebijakan Company A.",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert database.get_published_company_instruction("company-a") is None

        response = client.get("/admin/instructions/company-a/preview")
        assert response.status_code == 200
        assert "Jawab sesuai kebijakan Company A." in response.text
        response = client.post(
            "/admin/instructions/company-a/publish",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert database.get_published_company_instruction("company-a") == (
            "Jawab sesuai kebijakan Company A."
        )
        response = client.get("/admin/instructions/company-a")
        assert "Published v1" in response.text
        assert "Live" in response.text

    actions = [row["action"] for row in database.list_admin_audit_events()]
    assert actions[:2] == [
        "company_instruction.published",
        "company_instruction.draft_saved",
    ]


def test_admin_module_playbook_and_membership_access_workflow(tmp_path):
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
                            "division": "Management",
                            "role_level": "gm",
                            "communication_profile": "executive",
                            "default": True,
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    database = Database(tmp_path / "module-admin.db")
    database.initialize()
    database.bootstrap_companies(companies_file)
    database.bootstrap_users(users_file)
    settings = _test_settings(
        tmp_path,
        users_file,
        companies_file,
        database_path=tmp_path / "module-admin.db",
    )
    app = create_admin_app(settings, database)

    with TestClient(app) as client:
        assert client.post(
            "/admin/login",
            data={"username": "admin", "password": "strong-password"},
            follow_redirects=False,
        ).status_code == 303
        form_page = client.get("/admin/modules/new")
        assert form_page.status_code == 200
        assert 'name="module_id"' not in form_page.text
        csrf = re.search(
            r'name="csrf_token" value="([a-f0-9]+)"', form_page.text
        )
        assert csrf is not None
        csrf_token = csrf.group(1)

        assert client.post(
            "/admin/modules",
            data={
                "company_id": "company-a",
                "name": "Marketing",
                "description": "Campaign planning",
            },
        ).status_code == 403
        response = client.post(
            "/admin/modules",
            data={
                "csrf_token": csrf_token,
                "company_id": "company-a",
                "module_id": "forged-id",
                "name": "Marketing",
                "description": "Campaign planning",
                "active": "1",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith(
            "/admin/modules/company-a/marketing"
        )
        assert database.get_module_admin("company-a", "forged-id") is None

        assert client.post(
            "/admin/modules/company-a/marketing/draft",
            data={
                "csrf_token": csrf_token,
                "content": "Playbook marketing yang hanya boleh muncul setelah publish.",
            },
            follow_redirects=False,
        ).status_code == 303
        assert database.get_published_module_playbook(
            "company-a", "marketing"
        ) is None
        preview = client.get("/admin/modules/company-a/marketing/preview")
        assert preview.status_code == 200
        assert "hanya boleh muncul setelah publish" in preview.text
        assert client.post(
            "/admin/modules/company-a/marketing/publish",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        ).status_code == 303

        membership_page = client.get(
            "/admin/users/42/memberships/company-a/edit"
        )
        assert membership_page.status_code == 200
        assert "Akses Modules" in membership_page.text
        assert 'value="marketing"' in membership_page.text
        assert client.post(
            "/admin/users/42/memberships/company-a/modules",
            data={"csrf_token": csrf_token, "module_ids": "marketing"},
            follow_redirects=False,
        ).status_code == 303
        assert [
            item.module_id
            for item in database.list_accessible_modules(42, "company-a")
        ] == ["marketing"]

        activity = client.get("/admin/activity?category=module")
        assert activity.status_code == 200
        assert "Playbook dipublikasikan" in activity.text
        assert "Akses module diperbarui" in activity.text
        assert "hanya boleh muncul setelah publish" not in activity.text


def test_knowledge_publish_runtime_fallback_and_tenant_isolation(tmp_path):
    companies_file = tmp_path / "companies.json"
    for company_id, file_content in (
        ("company-a", "Knowledge file A"),
        ("company-b", "Knowledge file B"),
    ):
        knowledge_dir = tmp_path / "companies" / company_id / "knowledge"
        knowledge_dir.mkdir(parents=True)
        (knowledge_dir / "legacy.md").write_text(file_content, encoding="utf-8")
    companies_file.write_text(
        json.dumps(
            [
                {
                    "id": "company-a",
                    "name": "Company A",
                    "knowledge_dir": "companies/company-a/knowledge",
                },
                {
                    "id": "company-b",
                    "name": "Company B",
                    "knowledge_dir": "companies/company-b/knowledge",
                },
            ]
        ),
        encoding="utf-8",
    )
    database = Database(tmp_path / "knowledge.db")
    database.initialize()
    database.bootstrap_companies(companies_file)
    company_a = database.get_company_admin("company-a")
    company_b = database.get_company_admin("company-b")
    assert company_a is not None and company_b is not None

    assert database.get_published_company_knowledge("company-a", 10_000) is None
    fallback_a = load_company_content(company_a, tmp_path, 10_000).knowledge
    assert "Knowledge file A" in fallback_a
    database.create_knowledge_document(
        "company-a", "sales-target", "Sales Target", "Target A v1", "admin"
    )
    assert database.get_published_company_knowledge("company-a", 10_000) is None
    assert database.publish_knowledge_document(
        "company-a", "sales-target", "admin"
    ) == 1
    published_a = database.get_published_company_knowledge("company-a", 10_000)
    assert published_a is not None
    assert "Target A v1" in published_a
    assert "Knowledge file A" not in published_a
    assert load_company_content(
        company_a, tmp_path, 10_000, None, published_a
    ).knowledge == published_a

    assert database.get_published_company_knowledge("company-b", 10_000) is None
    fallback_b = load_company_content(company_b, tmp_path, 10_000).knowledge
    assert "Knowledge file B" in fallback_b
    database.create_knowledge_document(
        "company-b", "sales-target", "Sales Target", "Target B", "admin"
    )
    database.publish_knowledge_document("company-b", "sales-target", "admin")
    assert "Target B" not in database.get_published_company_knowledge(
        "company-a", 10_000
    )

    database.save_knowledge_document_draft(
        "company-a", "sales-target", "Sales Target", "Target A v2", "admin"
    )
    assert "Target A v1" in database.get_published_company_knowledge(
        "company-a", 10_000
    )
    assert database.publish_knowledge_document(
        "company-a", "sales-target", "admin"
    ) == 2
    versions = database.list_knowledge_document_versions(
        "company-a", "sales-target"
    )
    assert [row["version_number"] for row in versions] == [2, 1]
    assert all(len(str(row["content_sha256"])) == 64 for row in versions)
    database.restore_knowledge_document_version_to_draft(
        "company-a", "sales-target", versions[1]["id"], "admin"
    )
    document = database.get_knowledge_document_admin(
        "company-a", "sales-target"
    )
    assert document is not None
    assert document["draft_content"] == "Target A v1"
    assert document["published_content"] == "Target A v2"

    try:
        database.restore_knowledge_document_version_to_draft(
            "company-b", "sales-target", versions[1]["id"], "admin"
        )
        assert False, "Cross-tenant restore seharusnya ditolak"
    except ValueError as exc:
        assert "tidak ditemukan" in str(exc)

    database.set_knowledge_document_active(
        "company-a", "sales-target", False, "admin"
    )
    assert database.get_published_company_knowledge("company-a", 10_000) == ""
    assert load_company_content(company_a, tmp_path, 10_000, None, "").knowledge == ""


def test_admin_knowledge_document_workflow(tmp_path):
    companies_file = tmp_path / "companies.json"
    companies_file.write_text(
        json.dumps([{"id": "company-a", "name": "Company A"}]),
        encoding="utf-8",
    )
    users_file = tmp_path / "users.json"
    users_file.write_text("[]", encoding="utf-8")
    database = Database(tmp_path / "knowledge-admin.db")
    database.initialize()
    database.bootstrap_companies(companies_file)
    settings = _test_settings(
        tmp_path,
        users_file,
        companies_file,
        database_path=tmp_path / "knowledge-admin.db",
        project_root=Path("."),
    )
    app = create_admin_app(settings, database)
    with TestClient(app) as client:
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "strong-password"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert client.get("/admin/knowledge").status_code == 200
        response = client.get("/admin/knowledge/company-a/new")
        assert response.status_code == 200
        assert 'name="document_key"' not in response.text
        csrf = re.search(r'name="csrf_token" value="([a-f0-9]+)"', response.text)
        assert csrf is not None
        csrf_token = csrf.group(1)

        response = client.post(
            "/admin/knowledge/company-a",
            data={
                "document_key": "target-2026",
                "title": "Target 2026",
                "content": "Target omzet bulanan Rp500 juta.",
            },
        )
        assert response.status_code == 403
        response = client.post(
            "/admin/knowledge/company-a",
            data={
                "csrf_token": csrf_token,
                "document_key": "key-yang-dipalsukan",
                "title": "Target 2026",
                "content": "Target omzet bulanan Rp500 juta.",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith(
            "/admin/knowledge/company-a/target-2026"
        )
        assert database.get_published_company_knowledge("company-a", 10_000) is None

        response = client.get(
            "/admin/knowledge/company-a/target-2026/preview"
        )
        assert response.status_code == 200
        assert "Rp500 juta" in response.text
        response = client.post(
            "/admin/knowledge/company-a/target-2026/publish",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "Rp500 juta" in database.get_published_company_knowledge(
            "company-a", 10_000
        )
        response = client.get("/admin/knowledge/company-a/target-2026")
        assert response.status_code == 200
        assert "Published v1" in response.text
        assert "Live" in response.text

        response = client.post(
            "/admin/knowledge/company-a/target-2026/status",
            data={"csrf_token": csrf_token, "active": "0"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert database.get_published_company_knowledge("company-a", 10_000) == ""

    actions = [row["action"] for row in database.list_admin_audit_events()]
    assert actions[:3] == [
        "knowledge_document.deactivated",
        "knowledge_document.published",
        "knowledge_document.created",
    ]
    audit_payload = " ".join(
        str(row["details_json"]) for row in database.list_admin_audit_events()
    )
    assert "Target omzet bulanan Rp500 juta." not in audit_payload


def test_document_ingestion_pdf_docx_and_invalid_formats():
    pdf_buffer = BytesIO()
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): font}
            )
        }
    )
    content = DecodedStreamObject()
    content.set_data(
        b"BT /F1 12 Tf 72 720 Td (Target PDF Rp700 juta) Tj ET"
    )
    page[NameObject("/Contents")] = content
    writer.write(pdf_buffer)
    pdf = extract_uploaded_document(
        "target.pdf", "application/pdf", pdf_buffer.getvalue()
    )
    assert "Target PDF Rp700 juta" in pdf.text
    assert len(pdf.sha256) == 64

    docx_buffer = BytesIO()
    document = Document()
    document.add_heading("Brand Identity", level=1)
    document.add_paragraph("Amazing Malang memakai tone hangat dan informatif.")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Warna"
    table.cell(0, 1).text = "Ungu"
    document.save(docx_buffer)
    docx = extract_uploaded_document(
        "brand.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        docx_buffer.getvalue(),
    )
    assert "Brand Identity" in docx.text
    assert "Warna | Ungu" in docx.text

    for filename, data, expected in (
        ("legacy.doc", b"legacy", "Simpan ulang sebagai .docx"),
        ("fake.pdf", b"not-a-pdf", "tidak cocok dengan format PDF"),
        ("scan.pdf", _blank_pdf_bytes(), "tidak mengandung teks"),
    ):
        try:
            extract_uploaded_document(filename, "application/octet-stream", data)
            assert False, f"{filename} seharusnya ditolak"
        except ValueError as exc:
            assert expected in str(exc)


def test_admin_knowledge_docx_upload_creates_reviewable_draft(tmp_path):
    companies_file = tmp_path / "companies.json"
    companies_file.write_text(
        json.dumps([{"id": "company-a", "name": "Company A"}]),
        encoding="utf-8",
    )
    users_file = tmp_path / "users.json"
    users_file.write_text("[]", encoding="utf-8")
    database = Database(tmp_path / "upload-admin.db")
    database.initialize()
    database.bootstrap_companies(companies_file)
    settings = _test_settings(
        tmp_path,
        users_file,
        companies_file,
        database_path=tmp_path / "upload-admin.db",
        project_root=Path("."),
    )
    app = create_admin_app(settings, database)
    docx_buffer = BytesIO()
    source_document = Document()
    source_document.add_paragraph("Brand voice Amazing Malang bersifat hangat.")
    source_document.save(docx_buffer)
    payload = docx_buffer.getvalue()

    with TestClient(app) as client:
        assert client.post(
            "/admin/login",
            data={"username": "admin", "password": "strong-password"},
            follow_redirects=False,
        ).status_code == 303
        form_page = client.get("/admin/knowledge/company-a/new")
        csrf = re.search(
            r'name="csrf_token" value="([a-f0-9]+)"', form_page.text
        )
        assert csrf is not None
        response = client.post(
            "/admin/knowledge/company-a",
            data={
                "csrf_token": csrf.group(1),
                "document_key": "key-yang-dipalsukan",
                "title": "Brand Identity",
                "content": "",
            },
            files={
                "source_file": (
                    "brand.docx",
                    payload,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        stored = database.get_knowledge_document_admin(
            "company-a", "brand-identity"
        )
        assert stored is not None
        assert "Brand voice Amazing Malang" in stored["draft_content"]
        assert stored["source_filename"] == "brand.docx"
        assert stored["source_size_bytes"] == len(payload)
        assert len(str(stored["source_sha256"])) == 64
        assert database.get_published_company_knowledge("company-a", 10_000) is None
        editor = client.get("/admin/knowledge/company-a/brand-identity")
        assert "Source upload" in editor.text
        assert "brand.docx" in editor.text

    audit = database.list_admin_audit_events()[0]
    assert "brand.docx" in str(audit["details_json"])
    assert "Brand voice Amazing Malang" not in str(audit["details_json"])


def _blank_pdf_bytes() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


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
