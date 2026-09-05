from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import tempfile
import unicodedata
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from app.credentials import PROVIDERS, encrypt_api_key, validate_encrypted_endpoint
from app.model_catalog import CatalogResult, CATALOG_MESSAGES
from app.role_profiles import profile_id_for_role

from app.user_import import (
    MAX_USER_IMPORT_ROWS, UserImportReport, UserImportRow, UserImportValidationError,
)


COMPANY_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
SUPPORTED_AI_PROVIDERS = set(PROVIDERS)
ROLE_LEVELS = {"owner", "gm", "manager", "supervisor", "staff"}
COMMUNICATION_PROFILES = {
    "owner", "executive", "manager", "supervisor", "staff", "default"
}
MAX_TELEGRAM_ID = 9_007_199_254_740_991
MAX_COMPANY_INSTRUCTION_CHARS = 50_000
MAX_KNOWLEDGE_DOCUMENT_CHARS = 100_000
MAX_MODULE_PLAYBOOK_CHARS = 50_000


@dataclass(frozen=True)
class User:
    telegram_id: int
    name: str
    role: str
    division: str
    communication_profile: str
    custom_instruction: str
    active: bool


@dataclass(frozen=True)
class Company:
    company_id: str
    name: str
    profile_file: str
    instruction_file: str
    knowledge_dir: str
    active: bool


@dataclass(frozen=True)
class Membership:
    telegram_id: int
    company_id: str
    company_name: str
    job_title: str
    division: str
    role_level: str
    communication_profile: str
    custom_instruction: str
    is_default: bool
    active: bool


@dataclass(frozen=True)
class AIRuntimeProfile:
    profile_id: str
    label: str
    provider: str
    api_key_env: str
    model: str
    base_url: str
    active: bool
    api_key_ciphertext: str = field(default="", repr=False)
    updated_at: str = ""
    last_test_status: str = ""
    last_test_at: str = ""
    catalog_updated_at: str = ""


@dataclass(frozen=True)
class AIModule:
    company_id: str
    company_name: str
    module_id: str
    name: str
    description: str
    ai_runtime_profile_id: str
    active: bool
    backup_ai_runtime_profile_id: str = ""
    ai_model: str = ""
    backup_ai_model: str = ""
    short_code: str = ""


@dataclass(frozen=True)
class AdminUser:
    username: str
    display_name: str
    role: str
    active: bool
    created_at: str
    updated_at: str


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT '',
                    division TEXT NOT NULL DEFAULT '',
                    communication_profile TEXT NOT NULL DEFAULT '',
                    custom_instruction TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS companies (
                    company_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    profile_file TEXT NOT NULL DEFAULT '',
                    instruction_file TEXT NOT NULL DEFAULT '',
                    knowledge_dir TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_company_memberships (
                    telegram_id INTEGER NOT NULL,
                    company_id TEXT NOT NULL,
                    job_title TEXT NOT NULL DEFAULT '',
                    division TEXT NOT NULL DEFAULT '',
                    role_level TEXT NOT NULL DEFAULT '',
                    communication_profile TEXT NOT NULL DEFAULT '',
                    custom_instruction TEXT NOT NULL DEFAULT '',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (telegram_id, company_id),
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id),
                    FOREIGN KEY (company_id) REFERENCES companies(company_id)
                );

                CREATE TABLE IF NOT EXISTS user_sessions (
                    telegram_id INTEGER PRIMARY KEY,
                    active_company_id TEXT NOT NULL,
                    active_module_id TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id),
                    FOREIGN KEY (active_company_id) REFERENCES companies(company_id)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    company_id TEXT NOT NULL DEFAULT '',
                    module_id TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
                );

                CREATE TABLE IF NOT EXISTS admin_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS admin_users (
                    username TEXT PRIMARY KEY COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('super_admin', 'admin_operator')),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS communication_styles (
                    profile_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    response_level TEXT NOT NULL,
                    focus_json TEXT NOT NULL,
                    structure_json TEXT NOT NULL,
                    avoid_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS module_pending_requests (
                    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    company_id TEXT NOT NULL REFERENCES companies(company_id),
                    module_id TEXT NOT NULL, content TEXT NOT NULL,
                    context_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(telegram_id, company_id, module_id)
                );

                CREATE TABLE IF NOT EXISTS ai_runtime_profiles (
                    profile_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    api_key_env TEXT NOT NULL,
                    model TEXT NOT NULL,
                    base_url TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS modules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    ai_runtime_profile_id TEXT,
                    backup_ai_runtime_profile_id TEXT REFERENCES ai_runtime_profiles(profile_id),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(company_id, module_id),
                    FOREIGN KEY (company_id) REFERENCES companies(company_id),
                    FOREIGN KEY (ai_runtime_profile_id)
                        REFERENCES ai_runtime_profiles(profile_id)
                );

                CREATE TABLE IF NOT EXISTS ai_model_catalog (
                    profile_id TEXT NOT NULL REFERENCES ai_runtime_profiles(profile_id),
                    model_id TEXT NOT NULL,
                    selectable INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (profile_id, model_id)
                );

                CREATE TABLE IF NOT EXISTS module_playbook_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    module_pk INTEGER NOT NULL,
                    version_number INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    published_by TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    UNIQUE(module_pk, version_number),
                    FOREIGN KEY (module_pk) REFERENCES modules(id)
                );

                CREATE TABLE IF NOT EXISTS module_playbook_state (
                    module_pk INTEGER PRIMARY KEY,
                    draft_content TEXT NOT NULL DEFAULT '',
                    draft_updated_by TEXT NOT NULL DEFAULT '',
                    draft_updated_at TEXT NOT NULL,
                    published_version_id INTEGER,
                    FOREIGN KEY (module_pk) REFERENCES modules(id),
                    FOREIGN KEY (published_version_id)
                        REFERENCES module_playbook_versions(id)
                );

                CREATE TABLE IF NOT EXISTS module_access (
                    telegram_id INTEGER NOT NULL,
                    company_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (telegram_id, company_id, module_id),
                    FOREIGN KEY (telegram_id, company_id)
                        REFERENCES user_company_memberships(telegram_id, company_id),
                    FOREIGN KEY (company_id, module_id)
                        REFERENCES modules(company_id, module_id)
                );

                CREATE TABLE IF NOT EXISTS company_instruction_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id TEXT NOT NULL,
                    version_number INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    published_by TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    UNIQUE(company_id, version_number),
                    FOREIGN KEY (company_id) REFERENCES companies(company_id)
                );

                CREATE TABLE IF NOT EXISTS company_instruction_state (
                    company_id TEXT PRIMARY KEY,
                    draft_content TEXT NOT NULL DEFAULT '',
                    draft_updated_by TEXT NOT NULL DEFAULT '',
                    draft_updated_at TEXT NOT NULL,
                    published_version_id INTEGER,
                    FOREIGN KEY (company_id) REFERENCES companies(company_id),
                    FOREIGN KEY (published_version_id)
                        REFERENCES company_instruction_versions(id)
                );

                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id TEXT NOT NULL,
                    document_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    draft_content TEXT NOT NULL DEFAULT '',
                    draft_updated_by TEXT NOT NULL,
                    draft_updated_at TEXT NOT NULL,
                    source_filename TEXT NOT NULL DEFAULT '',
                    source_media_type TEXT NOT NULL DEFAULT '',
                    source_size_bytes INTEGER NOT NULL DEFAULT 0,
                    source_sha256 TEXT NOT NULL DEFAULT '',
                    published_version_id INTEGER,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(company_id, document_key),
                    FOREIGN KEY (company_id) REFERENCES companies(company_id),
                    FOREIGN KEY (published_version_id)
                        REFERENCES knowledge_document_versions(id)
                );

                CREATE TABLE IF NOT EXISTS knowledge_document_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL,
                    version_number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    published_by TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    UNIQUE(document_id, version_number),
                    FOREIGN KEY (document_id) REFERENCES knowledge_documents(id)
                );
                """
            )
            credential_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(ai_runtime_profiles)")
            }
            for column in ("api_key_ciphertext", "last_test_status", "last_test_at", "catalog_updated_at"):
                if column not in credential_columns:
                    connection.execute(
                        f"ALTER TABLE ai_runtime_profiles ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            user_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "communication_profile" not in user_columns:
                connection.execute(
                    """
                    ALTER TABLE users
                    ADD COLUMN communication_profile TEXT NOT NULL DEFAULT ''
                    """
                )

            message_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "company_id" not in message_columns:
                connection.execute(
                    """
                    ALTER TABLE messages
                    ADD COLUMN company_id TEXT NOT NULL DEFAULT ''
                    """
                )
            if "module_id" not in message_columns:
                connection.execute(
                    """
                    ALTER TABLE messages
                    ADD COLUMN module_id TEXT NOT NULL DEFAULT ''
                    """
                )

            session_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(user_sessions)"
                ).fetchall()
            }
            if "active_module_id" not in session_columns:
                connection.execute(
                    """
                    ALTER TABLE user_sessions
                    ADD COLUMN active_module_id TEXT NOT NULL DEFAULT ''
                    """
                )

            module_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(modules)").fetchall()
            }
            if "ai_runtime_profile_id" not in module_columns:
                connection.execute(
                    "ALTER TABLE modules ADD COLUMN ai_runtime_profile_id TEXT"
                )
            if "backup_ai_runtime_profile_id" not in module_columns:
                connection.execute(
                    "ALTER TABLE modules ADD COLUMN backup_ai_runtime_profile_id "
                    "TEXT REFERENCES ai_runtime_profiles(profile_id)"
                )
            for column in ("ai_model", "backup_ai_model", "short_code"):
                if column not in module_columns:
                    connection.execute(f"ALTER TABLE modules ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS modules_short_code_unique "
                "ON modules(company_id, short_code COLLATE NOCASE) WHERE short_code != ''"
            )

            knowledge_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(knowledge_documents)"
                ).fetchall()
            }
            for column_name, definition in (
                ("source_filename", "TEXT NOT NULL DEFAULT ''"),
                ("source_media_type", "TEXT NOT NULL DEFAULT ''"),
                ("source_size_bytes", "INTEGER NOT NULL DEFAULT 0"),
                ("source_sha256", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column_name not in knowledge_columns:
                    connection.execute(
                        f"ALTER TABLE knowledge_documents "
                        f"ADD COLUMN {column_name} {definition}"
                    )

            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_user_company_id
                ON messages(telegram_id, company_id, module_id, id DESC);

                CREATE INDEX IF NOT EXISTS idx_messages_user_company_module_id
                ON messages(telegram_id, company_id, module_id, id DESC);

                CREATE INDEX IF NOT EXISTS idx_memberships_user
                ON user_company_memberships(telegram_id, active, company_id);

                CREATE INDEX IF NOT EXISTS idx_admin_audit_created
                ON admin_audit_events(created_at DESC, id DESC);

                CREATE INDEX IF NOT EXISTS idx_instruction_versions_company
                ON company_instruction_versions(company_id, version_number DESC);

                CREATE INDEX IF NOT EXISTS idx_knowledge_documents_company
                ON knowledge_documents(company_id, active, document_key);

                CREATE INDEX IF NOT EXISTS idx_knowledge_versions_document
                ON knowledge_document_versions(document_id, version_number DESC);

                CREATE INDEX IF NOT EXISTS idx_modules_company
                ON modules(company_id, active, module_id);

                CREATE INDEX IF NOT EXISTS idx_modules_runtime_profile
                ON modules(ai_runtime_profile_id, active, company_id, module_id);

                CREATE INDEX IF NOT EXISTS idx_module_access_membership
                ON module_access(telegram_id, company_id, active, module_id);

                CREATE INDEX IF NOT EXISTS idx_module_playbook_versions
                ON module_playbook_versions(module_pk, version_number DESC);
                """
            )
            from app.shared_modules import initialize_schema
            initialize_schema(connection)

    def ensure_primary_admin(self, username: str, password: str) -> None:
        """Bootstrap the environment admin and keep its env password authoritative."""
        normalized = _validate_admin_username(username)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT password_hash FROM admin_users WHERE username = ? COLLATE NOCASE",
                (normalized,),
            ).fetchone()
            if existing:
                if not _verify_admin_password(password, existing["password_hash"]):
                    connection.execute(
                        "UPDATE admin_users SET password_hash=?, updated_at=? WHERE username=? COLLATE NOCASE",
                        (_hash_admin_password(password), _now(), normalized),
                    )
                return
            timestamp = _now()
            connection.execute(
                """INSERT INTO admin_users(
                    username, display_name, password_hash, role, active,
                    created_at, updated_at
                ) VALUES(?,?,?,?,1,?,?)""",
                (
                    normalized,
                    normalized,
                    _hash_admin_password(password),
                    "super_admin",
                    timestamp,
                    timestamp,
                ),
            )

    def ensure_communication_styles(self, profiles) -> None:
        with self._connect() as connection:
            for profile in profiles.by_id.values():
                connection.execute(
                    """INSERT OR IGNORE INTO communication_styles(
                        profile_id, label, response_level, focus_json,
                        structure_json, avoid_json, updated_at
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        profile.profile_id, profile.label, profile.response_level,
                        json.dumps(profile.focus, ensure_ascii=False),
                        json.dumps(profile.default_structure, ensure_ascii=False),
                        json.dumps(profile.avoid, ensure_ascii=False), _now(),
                    ),
                )

    def list_communication_styles(self) -> list[dict[str, object]]:
        order = ("owner", "executive", "manager", "supervisor", "staff", "default")
        with self._connect() as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM communication_styles"
            ).fetchall()]
        rank = {profile_id: index for index, profile_id in enumerate(order)}
        return sorted(rows, key=lambda row: rank.get(str(row["profile_id"]), 99))

    def update_communication_style(
        self, profile_id: str, response_level: str, focus: str,
        structure: str, avoid: str, actor: str,
    ) -> None:
        profile_id = str(profile_id).strip().casefold()
        if profile_id not in {"owner", "executive", "manager", "supervisor", "staff", "default"}:
            raise ValueError("Communication style tidak valid")
        level = str(response_level).strip()
        if not level or len(level) > 500:
            raise ValueError("Kedalaman jawaban wajib diisi dan maksimal 500 karakter")

        def lines(value: str) -> list[str]:
            result = [item.strip() for item in str(value).splitlines() if item.strip()]
            if len(result) > 20 or any(len(item) > 300 for item in result):
                raise ValueError("Setiap daftar maksimal 20 baris dan 300 karakter per baris")
            return result

        with self._connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM communication_styles WHERE profile_id=?", (profile_id,)
            ).fetchone():
                raise ValueError("Communication style tidak ditemukan")
            connection.execute(
                """UPDATE communication_styles SET response_level=?, focus_json=?,
                    structure_json=?, avoid_json=?, updated_at=? WHERE profile_id=?""",
                (
                    level, json.dumps(lines(focus), ensure_ascii=False),
                    json.dumps(lines(structure), ensure_ascii=False),
                    json.dumps(lines(avoid), ensure_ascii=False), _now(), profile_id,
                ),
            )
            _write_audit(
                connection, actor, "communication_style.updated",
                "communication_style", profile_id, {},
            )

    def authenticate_admin(self, username: str, password: str) -> AdminUser | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM admin_users WHERE username = ? COLLATE NOCASE AND active = 1",
                (str(username).strip(),),
            ).fetchone()
        if row is None or not _verify_admin_password(password, row["password_hash"]):
            return None
        return _admin_user_from_row(row)

    def get_admin_user(self, username: str) -> AdminUser | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM admin_users WHERE username = ? COLLATE NOCASE",
                (str(username).strip(),),
            ).fetchone()
        return _admin_user_from_row(row) if row else None

    def list_admin_users(self) -> list[AdminUser]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM admin_users ORDER BY role DESC, display_name COLLATE NOCASE"
            ).fetchall()
        return [_admin_user_from_row(row) for row in rows]

    def create_admin_user(
        self, username: str, display_name: str, password: str, role: str, actor: str
    ) -> AdminUser:
        normalized = _validate_admin_username(username)
        name = str(display_name).strip()
        if not name or len(name) > 100:
            raise ValueError("Nama admin wajib diisi dan maksimal 100 karakter")
        if len(password) < 12:
            raise ValueError("Password minimal 12 karakter")
        if role not in {"super_admin", "admin_operator"}:
            raise ValueError("Role admin tidak valid")
        timestamp = _now()
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO admin_users(
                        username, display_name, password_hash, role, active,
                        created_at, updated_at
                    ) VALUES(?,?,?,?,1,?,?)""",
                    (
                        normalized, name, _hash_admin_password(password), role,
                        timestamp, timestamp,
                    ),
                )
                _write_audit(
                    connection, actor, "admin_user.created", "admin_user",
                    normalized, {"display_name": name, "role": role},
                )
        except sqlite3.IntegrityError:
            raise ValueError("Username admin sudah digunakan") from None
        return self.get_admin_user(normalized)

    def set_admin_user_active(
        self, username: str, active: bool, actor: str
    ) -> AdminUser:
        normalized = _validate_admin_username(username)
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM admin_users WHERE username = ? COLLATE NOCASE",
                (normalized,),
            ).fetchone()
            if current is None:
                raise ValueError("Akun admin tidak ditemukan")
            if current["role"] == "super_admin" and not active:
                count = connection.execute(
                    "SELECT COUNT(*) FROM admin_users WHERE role='super_admin' AND active=1"
                ).fetchone()[0]
                if count <= 1:
                    raise ValueError("Super Admin aktif terakhir tidak dapat dinonaktifkan")
            connection.execute(
                "UPDATE admin_users SET active=?, updated_at=? WHERE username=? COLLATE NOCASE",
                (int(active), _now(), normalized),
            )
            _write_audit(
                connection, actor,
                "admin_user.activated" if active else "admin_user.deactivated",
                "admin_user", normalized, {},
            )
        return self.get_admin_user(normalized)

    def bootstrap_companies(self, companies_file: Path) -> int:
        """Import JSON only when the company registry is still empty."""
        with self._connect() as connection:
            existing = int(
                connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
            )
        return 0 if existing else self.sync_companies(companies_file)

    def bootstrap_users(self, users_file: Path) -> int:
        """Import JSON only when the user directory is still empty."""
        with self._connect() as connection:
            existing = int(
                connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            )
        return 0 if existing else self.sync_users(users_file)

    def sync_companies(self, companies_file: Path) -> int:
        if not companies_file.exists():
            return 0
        raw_companies = json.loads(companies_file.read_text(encoding="utf-8"))
        if not isinstance(raw_companies, list):
            raise ValueError(f"{companies_file} harus berisi JSON array")

        now = _now()
        rows = []
        seen_ids: set[str] = set()
        for item in raw_companies:
            company_id = str(item["id"]).strip().casefold()
            if not COMPANY_ID_PATTERN.fullmatch(company_id):
                raise ValueError(
                    f"Company ID '{company_id}' tidak valid. Gunakan huruf kecil, "
                    "angka, dan tanda minus."
                )
            if company_id in seen_ids:
                raise ValueError(f"Company ID duplikat: {company_id}")
            seen_ids.add(company_id)
            rows.append(
                (
                    company_id,
                    str(item["name"]).strip(),
                    _safe_relative_path(item.get("profile_file", "")),
                    _safe_relative_path(item.get("instruction_file", "")),
                    _safe_relative_path(item.get("knowledge_dir", "")),
                    int(bool(item.get("active", True))),
                    now,
                )
            )

        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO companies (
                    company_id, name, profile_file, instruction_file,
                    knowledge_dir, active, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_id) DO UPDATE SET
                    name=excluded.name,
                    profile_file=excluded.profile_file,
                    instruction_file=excluded.instruction_file,
                    knowledge_dir=excluded.knowledge_dir,
                    active=excluded.active,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
            _deactivate_missing(
                connection, "companies", "company_id", sorted(seen_ids)
            )
        return len(rows)

    def sync_users(self, users_file: Path) -> int:
        if not users_file.exists():
            return 0
        raw_users = json.loads(users_file.read_text(encoding="utf-8"))
        if not isinstance(raw_users, list):
            raise ValueError(f"{users_file} harus berisi JSON array")

        now = _now()
        user_rows = []
        membership_rows = []
        membership_ids_by_user: dict[int, list[str]] = {}
        for item in raw_users:
            telegram_id = int(item["telegram_id"])
            user_rows.append(
                (
                    telegram_id,
                    str(item["name"]).strip(),
                    str(item.get("role", "")).strip(),
                    str(item.get("division", "")).strip(),
                    str(item.get("communication_profile", "")).strip(),
                    str(item.get("custom_instruction", "")).strip(),
                    int(bool(item.get("active", True))),
                    now,
                )
            )

            memberships = item.get("memberships", [])
            if not isinstance(memberships, list):
                raise ValueError(f"Membership user {telegram_id} harus berupa array")
            default_count = sum(bool(value.get("default", False)) for value in memberships)
            if default_count > 1:
                raise ValueError(f"User {telegram_id} hanya boleh memiliki satu company default")
            seen_companies: set[str] = set()
            for membership in memberships:
                company_id = str(membership["company_id"]).strip().casefold()
                if company_id in seen_companies:
                    raise ValueError(
                        f"Membership company '{company_id}' duplikat untuk user {telegram_id}"
                    )
                seen_companies.add(company_id)
                membership_rows.append(
                    (
                        telegram_id,
                        company_id,
                        str(membership.get("job_title", "")).strip(),
                        str(membership.get("division", "")).strip(),
                        str(membership.get("role_level", "")).strip().casefold(),
                        str(membership.get("communication_profile", "")).strip(),
                        str(membership.get("custom_instruction", "")).strip(),
                        int(bool(membership.get("default", False))),
                        int(bool(membership.get("active", True))),
                        now,
                    )
                )
            membership_ids_by_user[telegram_id] = sorted(seen_companies)

        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO users (
                    telegram_id, name, role, division,
                    communication_profile, custom_instruction, active, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    name=excluded.name,
                    role=excluded.role,
                    division=excluded.division,
                    communication_profile=excluded.communication_profile,
                    custom_instruction=excluded.custom_instruction,
                    active=excluded.active,
                    updated_at=excluded.updated_at
                """,
                user_rows,
            )
            try:
                connection.executemany(
                    """
                    INSERT INTO user_company_memberships (
                        telegram_id, company_id, job_title, division, role_level,
                        communication_profile, custom_instruction, is_default,
                        active, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(telegram_id, company_id) DO UPDATE SET
                        job_title=excluded.job_title,
                        division=excluded.division,
                        role_level=excluded.role_level,
                        communication_profile=excluded.communication_profile,
                        custom_instruction=excluded.custom_instruction,
                        is_default=excluded.is_default,
                        active=excluded.active,
                        updated_at=excluded.updated_at
                    """,
                    membership_rows,
                )
                for telegram_id, company_ids in membership_ids_by_user.items():
                    if company_ids:
                        placeholders = ",".join("?" for _ in company_ids)
                        connection.execute(
                            f"""
                            UPDATE user_company_memberships SET active = 0
                            WHERE telegram_id = ?
                            AND company_id NOT IN ({placeholders})
                            """,
                            (telegram_id, *company_ids),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE user_company_memberships SET active = 0
                            WHERE telegram_id = ?
                            """,
                            (telegram_id,),
                        )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    "Membership merujuk company yang belum terdaftar di config/companies.json"
                ) from exc
            _deactivate_missing(
                connection,
                "users",
                "telegram_id",
                [row[0] for row in user_rows],
            )
            _assign_legacy_messages(connection)
        return len(user_rows)

    def get_user(self, telegram_id: int) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_id = ? AND active = 1",
                (telegram_id,),
            ).fetchone()
        if row is None:
            return None
        return User(
            telegram_id=row["telegram_id"],
            name=row["name"],
            role=row["role"],
            division=row["division"],
            communication_profile=row["communication_profile"],
            custom_instruction=row["custom_instruction"],
            active=bool(row["active"]),
        )

    def get_user_admin(self, telegram_id: int) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return _user_from_row(row) if row else None

    def create_user_with_membership(
        self,
        telegram_id: object,
        name: object,
        company_id: object,
        job_title: object,
        division: object,
        role_level: object,
        communication_profile: object,
        custom_instruction: object,
        actor: str,
        active: bool = True,
    ) -> User:
        normalized_id = _validate_telegram_id(telegram_id)
        normalized_name = _validate_user_name(name)
        membership = _validate_membership_fields(
            company_id,
            job_title,
            division,
            role_level,
            communication_profile,
            custom_instruction,
        )
        now = _now()
        try:
            with self._connect() as connection:
                _require_active_company(connection, membership["company_id"])
                connection.execute(
                    """
                    INSERT INTO users (
                        telegram_id, name, role, division, communication_profile,
                        custom_instruction, active, updated_at
                    ) VALUES (?, ?, '', '', '', '', ?, ?)
                    """,
                    (normalized_id, normalized_name, int(active), now),
                )
                connection.execute(
                    """
                    INSERT INTO user_company_memberships (
                        telegram_id, company_id, job_title, division, role_level,
                        communication_profile, custom_instruction, is_default,
                        active, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?)
                    """,
                    (
                        normalized_id,
                        membership["company_id"],
                        membership["job_title"],
                        membership["division"],
                        membership["role_level"],
                        membership["communication_profile"],
                        membership["custom_instruction"],
                        now,
                    ),
                )
                _write_audit(
                    connection,
                    actor,
                    "user.created",
                    "user",
                    str(normalized_id),
                    {"name": normalized_name, "active": active},
                )
                _write_audit(
                    connection,
                    actor,
                    "membership.created",
                    "membership",
                    f"{normalized_id}:{membership['company_id']}",
                    _membership_audit_details(membership, is_default=True),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Telegram ID '{normalized_id}' sudah terdaftar") from exc
        user = self.get_user_admin(normalized_id)
        if user is None:
            raise RuntimeError("User gagal disimpan")
        return user

    def import_new_users(
        self,
        rows: list[UserImportRow],
        *,
        role_level: str,
        communication_profile: str,
        active: bool,
        actor: str,
        source_filename: str = "",
        source_sha256: str = "",
    ) -> UserImportReport:
        """Insert-only, all-or-nothing import. Existing identities are never mutated."""
        if not rows or len(rows) > MAX_USER_IMPORT_ROWS:
            raise ValueError("Import membutuhkan 1–500 baris data.")
        if role_level not in ROLE_LEVELS or communication_profile not in COMMUNICATION_PROFILES:
            raise ValueError("Role level atau communication profile tidak valid.")
        communication_profile = profile_id_for_role(role_level)
        if not isinstance(active, bool):
            raise ValueError("Status whitelist tidak valid.")
        skipped: list[int] = []
        duplicates: list[int] = []
        errors: list[str] = []
        new_users: dict[int, str] = {}
        memberships: dict[tuple[int, str], dict[str, str]] = {}
        with closing(self._connect()) as connection, connection:
            # Lock before checking IDs: concurrent imports/manual creation cannot
            # turn a checked-new identity into an update or a partially saved batch.
            connection.execute("BEGIN IMMEDIATE")
            companies: dict[str, set[str]] = {}
            for company in connection.execute("SELECT company_id, name FROM companies WHERE active = 1"):
                for value in (company["name"], company["company_id"]):
                    key = " ".join(value.split()).casefold()
                    companies.setdefault(key, set()).add(company["company_id"])
            for row in rows:
                try:
                    raw_id = row.telegram_id
                    if isinstance(raw_id, (int, float)) and not isinstance(raw_id, bool) and raw_id > 999_999_999_999_999:
                        raise ValueError("Telegram ID lebih dari 15 digit harus disimpan sebagai teks di Excel agar tidak dibulatkan.")
                    if isinstance(raw_id, float):
                        if not math.isfinite(raw_id) or not raw_id.is_integer():
                            raise ValueError("Telegram ID harus angka bulat, bukan pecahan.")
                        raw_id = int(raw_id)
                    if isinstance(raw_id, bool) or not re.fullmatch(r"[0-9]+", str(raw_id).strip()):
                        raise ValueError("Telegram ID harus ID angka, bukan nomor berformat atau @username.")
                    telegram_id = _validate_telegram_id(raw_id)
                    # Include inactive users. Ignore all remaining Excel values for
                    # existing IDs, including company, name, status, and membership.
                    if connection.execute(
                        "SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,)
                    ).fetchone():
                        skipped.append(row.row_number)
                        continue
                    name = _validate_user_name(row.name)
                    matches = companies.get(" ".join(str(row.company or "").split()).casefold(), set())
                    if not matches:
                        raise ValueError("Perusahaan tidak ditemukan atau nonaktif. Cocokkan dengan halaman Companies.")
                    if len(matches) != 1:
                        raise ValueError("Nama perusahaan ambigu. Isi Company ID yang tepat pada kolom Perusahaan.")
                    company_id = next(iter(matches))
                    membership = _validate_membership_fields(
                        company_id, row.job_title, row.division, role_level,
                        communication_profile, "",
                    )
                    if telegram_id in new_users and new_users[telegram_id] != name:
                        raise ValueError("Nama berbeda untuk Telegram ID yang sama dalam file.")
                    key = (telegram_id, company_id)
                    if key in memberships:
                        if memberships[key] != membership:
                            raise ValueError("Data membership berbeda untuk Telegram ID dan perusahaan yang sama.")
                        duplicates.append(row.row_number)
                        continue
                    new_users[telegram_id] = name
                    memberships[key] = membership
                except ValueError as exc:
                    errors.append(f"Baris {row.row_number}: {exc}")
            if errors:
                raise UserImportValidationError(errors)

            now = _now()
            for telegram_id, name in new_users.items():
                connection.execute(
                    "INSERT INTO users (telegram_id, name, active, updated_at) VALUES (?, ?, ?, ?)",
                    (telegram_id, name, int(active), now),
                )
                _write_audit(connection, actor, "user.created", "user", str(telegram_id),
                             {"name": name, "active": active})
            assigned_default: set[int] = set()
            for (telegram_id, company_id), membership in memberships.items():
                is_default = telegram_id not in assigned_default
                assigned_default.add(telegram_id)
                connection.execute(
                    """
                    INSERT INTO user_company_memberships (
                        telegram_id, company_id, job_title, division, role_level,
                        communication_profile, custom_instruction, is_default, active, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, '', ?, 1, ?)
                    """,
                    (telegram_id, company_id, membership["job_title"], membership["division"],
                     role_level, communication_profile, int(is_default), now),
                )
                _write_audit(connection, actor, "membership.created", "membership",
                             f"{telegram_id}:{company_id}",
                             _membership_audit_details(membership, is_default=is_default))
            report = UserImportReport(len(new_users), len(memberships), tuple(skipped), tuple(duplicates))
            _write_audit(
                connection, actor, "users.imported", "user_import", source_sha256[:64],
                {"source_filename": Path(source_filename.replace("\\", "/")).name[:255],
                 "source_sha256": source_sha256[:64], "created_users": report.created_users,
                 "created_memberships": report.created_memberships, "skipped_rows": len(skipped),
                 "duplicate_rows": len(duplicates), "active": active,
                 "role_level": role_level, "communication_profile": communication_profile},
            )
        return report

    def update_user(self, telegram_id: int, name: object, actor: str) -> User:
        normalized_id = _validate_telegram_id(telegram_id)
        normalized_name = _validate_user_name(name)
        with self._connect() as connection:
            current = connection.execute(
                "SELECT name FROM users WHERE telegram_id = ?", (normalized_id,)
            ).fetchone()
            if current is None:
                raise ValueError("User tidak ditemukan")
            connection.execute(
                "UPDATE users SET name = ?, updated_at = ? WHERE telegram_id = ?",
                (normalized_name, _now(), normalized_id),
            )
            _write_audit(
                connection,
                actor,
                "user.updated",
                "user",
                str(normalized_id),
                {"name_before": current["name"], "name_after": normalized_name},
            )
        user = self.get_user_admin(normalized_id)
        if user is None:
            raise RuntimeError("User gagal diperbarui")
        return user

    def set_user_active(self, telegram_id: int, active: bool, actor: str) -> User:
        normalized_id = _validate_telegram_id(telegram_id)
        with self._connect() as connection:
            current = connection.execute(
                "SELECT active FROM users WHERE telegram_id = ?", (normalized_id,)
            ).fetchone()
            if current is None:
                raise ValueError("User tidak ditemukan")
            if bool(current["active"]) != active:
                connection.execute(
                    "UPDATE users SET active = ?, updated_at = ? WHERE telegram_id = ?",
                    (int(active), _now(), normalized_id),
                )
                if not active:
                    connection.execute(
                        "DELETE FROM user_sessions WHERE telegram_id = ?",
                        (normalized_id,),
                    )
                _write_audit(
                    connection,
                    actor,
                    "user.activated" if active else "user.deactivated",
                    "user",
                    str(normalized_id),
                    {"active": active},
                )
        user = self.get_user_admin(normalized_id)
        if user is None:
            raise RuntimeError("User gagal diperbarui")
        return user

    def get_company(self, company_id: str) -> Company | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM companies WHERE company_id = ? AND active = 1",
                (company_id,),
            ).fetchone()
        return _company_from_row(row) if row else None

    def get_company_admin(self, company_id: str) -> Company | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM companies WHERE company_id = ?",
                (company_id.strip().casefold(),),
            ).fetchone()
        return _company_from_row(row) if row else None

    def create_company(
        self, company_id: str, name: str, actor: str, active: bool = True
    ) -> Company:
        normalized_name = _validate_company_name(name)
        requested_id = str(company_id or "").strip()
        normalized_id = _validate_company_id(requested_id) if requested_id else ""
        now = _now()
        try:
            with self._connect() as connection:
                if not normalized_id:
                    normalized_id = _next_unique_identifier(
                        connection,
                        "companies",
                        "company_id",
                        _identifier_from_label(normalized_name, "company"),
                    )
                base = f"companies/{normalized_id}"
                connection.execute(
                    """
                    INSERT INTO companies (
                        company_id, name, profile_file, instruction_file,
                        knowledge_dir, active, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_id,
                        normalized_name,
                        f"{base}/profile.md",
                        f"{base}/instruction.md",
                        f"{base}/knowledge",
                        int(active),
                        now,
                    ),
                )
                _write_audit(
                    connection,
                    actor,
                    "company.created",
                    "company",
                    normalized_id,
                    {"name": normalized_name, "active": active},
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Company ID '{normalized_id}' sudah digunakan") from exc
        company = self.get_company_admin(normalized_id)
        if company is None:
            raise RuntimeError("Company gagal disimpan")
        return company

    def update_company(self, company_id: str, name: str, actor: str) -> Company:
        normalized_id = _validate_company_id(company_id)
        normalized_name = _validate_company_name(name)
        with self._connect() as connection:
            current = connection.execute(
                "SELECT name FROM companies WHERE company_id = ?", (normalized_id,)
            ).fetchone()
            if current is None:
                raise ValueError("Company tidak ditemukan")
            connection.execute(
                "UPDATE companies SET name = ?, updated_at = ? WHERE company_id = ?",
                (normalized_name, _now(), normalized_id),
            )
            _write_audit(
                connection,
                actor,
                "company.updated",
                "company",
                normalized_id,
                {"name_before": current["name"], "name_after": normalized_name},
            )
        company = self.get_company_admin(normalized_id)
        if company is None:
            raise RuntimeError("Company gagal diperbarui")
        return company

    def migrate_company_ids(self, renames: dict[str, str], actor: str) -> str | None:
        """Operator-only migration BEFORE starting any bot/admin writers.

        Requires a stopped previous runtime (Railway's single mounted volume).
        Returns the verified backup path, or None for a previously audited run.
        This is deliberately not exposed through an admin HTTP endpoint.
        """
        if not isinstance(renames, dict) or not renames or len(renames) > 20:
            raise ValueError("Migrasi membutuhkan mapping Company ID yang tidak kosong")
        normalized = {}
        for old, new in renames.items():
            if not isinstance(old, str) or not isinstance(new, str):
                raise ValueError("Company ID migrasi harus berupa string")
            source, target = _validate_company_id(old), _validate_company_id(new)
            if source in normalized or source == target:
                raise ValueError("Company ID sumber duplikat atau tidak berubah")
            normalized[source] = target
        if len(set(normalized.values())) != len(normalized) or set(normalized) & set(normalized.values()):
            raise ValueError("Tujuan migrasi duplikat atau bertumpuk dengan sumber")
        if not actor.strip():
            raise ValueError("Actor migrasi wajib diisi")
        references = {
            "companies": "company_id",
            "user_company_memberships": "company_id",
            "user_sessions": "active_company_id",
            "messages": "company_id",
            "shared_messages": "company_id",
            "module_pending_requests": "company_id",
            "modules": "company_id",
            "module_access": "company_id",
            "company_instruction_versions": "company_id",
            "company_instruction_state": "company_id",
            "knowledge_documents": "company_id",
        }
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("PRAGMA defer_foreign_keys=ON")
            if connection.execute("PRAGMA quick_check").fetchall()[0][0] != "ok":
                raise ValueError("Integritas database belum valid; migrasi dibatalkan")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Database memiliki relasi rusak; migrasi dibatalkan")
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )]
            detected = {}
            for table in tables:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                    raise ValueError("Schema tidak dikenal; perlu review migrasi")
                columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
                scoped = [column for column in columns if column in {"company_id", "active_company_id"}]
                if scoped:
                    if len(scoped) != 1:
                        raise ValueError("Schema Company ID berubah; perlu review migrasi")
                    detected[table] = scoped[0]
                for fk in connection.execute(f'PRAGMA foreign_key_list("{table}")'):
                    if fk[2] == "companies" and references.get(table) != fk[3]:
                        raise ValueError("Referensi company baru; perlu review migrasi")
            if detected != references:
                raise ValueError("Schema Company ID berubah; perlu review migrasi")
            existing = {row[0] for row in connection.execute("SELECT company_id FROM companies")}
            previous = connection.execute(
                "SELECT details_json FROM admin_audit_events WHERE action='company.ids_migrated'"
            ).fetchall()
            if all(old not in existing and new in existing for old, new in normalized.items()):
                if any(json.loads(row[0]).get("renames") == normalized for row in previous):
                    for table, column in references.items():
                        for old in normalized:
                            if connection.execute(f'SELECT 1 FROM "{table}" WHERE "{column}"=? LIMIT 1', (old,)).fetchone():
                                raise ValueError("Referensi ID lama muncul kembali; perlu review")
                    return None
                raise ValueError("ID sumber tidak ada tanpa audit migrasi yang cocok")
            if any(old not in existing or new in existing for old, new in normalized.items()):
                raise ValueError("Company sumber tidak ditemukan atau ID tujuan sudah digunakan")
            for table, column in references.items():
                for new in normalized.values():
                    if connection.execute(f'SELECT 1 FROM "{table}" WHERE "{column}"=? LIMIT 1', (new,)).fetchone():
                        raise ValueError("ID tujuan memiliki referensi lama; tidak boleh menggabungkan data")

            def snapshot(mapping):
                result = {}
                for table in tables:
                    rows = []
                    for row in connection.execute(f'SELECT * FROM "{table}"'):
                        values = dict(row)
                        column = references.get(table)
                        if column:
                            values[column] = mapping.get(values[column], values[column])
                        rows.append(hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest())
                    result[table] = sorted(rows)
                return result

            expected = snapshot(normalized)
            backup_dir = self.path.resolve().parent / "backups"
            backup_dir.mkdir(mode=0o700, exist_ok=True)
            descriptor, backup_name = tempfile.mkstemp(
                prefix="before-company-id-", suffix=".db", dir=backup_dir
            )
            os.close(descriptor)
            # A separate read connection sees the pre-migration snapshot while
            # BEGIN IMMEDIATE prevents other writers. Backing up the writer
            # connection itself would wait on its own uncommitted transaction.
            with closing(sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(backup_name)) as backup:
                    source.backup(backup)
                    if backup.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                        raise ValueError("Cadangan gagal diverifikasi; migrasi dibatalkan")
                    if backup.execute("PRAGMA foreign_key_check").fetchall():
                        raise ValueError("Relasi cadangan tidak valid; migrasi dibatalkan")
            for table, column in references.items():
                for old, new in normalized.items():
                    connection.execute(f'UPDATE "{table}" SET "{column}"=? WHERE "{column}"=?', (new, old))
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Relasi hasil migrasi tidak valid; seluruh perubahan dibatalkan")
            if snapshot({}) != expected:
                raise ValueError("Data selain ID berubah; seluruh migrasi dibatalkan")
            _write_audit(connection, actor, "company.ids_migrated", "company", ",".join(sorted(normalized.values())), {
                "renames": normalized, "backup_path": backup_name,
                "verified": "all_rows_preserved_except_company_ids",
            })
        return backup_name

    def set_company_active(
        self, company_id: str, active: bool, actor: str
    ) -> Company:
        normalized_id = _validate_company_id(company_id)
        with self._connect() as connection:
            current = connection.execute(
                "SELECT active FROM companies WHERE company_id = ?", (normalized_id,)
            ).fetchone()
            if current is None:
                raise ValueError("Company tidak ditemukan")
            if bool(current["active"]) == active:
                company = self.get_company_admin(normalized_id)
                if company is None:
                    raise RuntimeError("Company tidak ditemukan")
                return company
            if not active:
                active_members = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM user_company_memberships
                        WHERE company_id = ? AND active = 1
                        """,
                        (normalized_id,),
                    ).fetchone()[0]
                )
                if active_members:
                    raise ValueError(
                        "Company masih memiliki membership aktif. Nonaktifkan "
                        "membership terlebih dahulu."
                    )
                other_active = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM companies
                        WHERE active = 1 AND company_id != ?
                        """,
                        (normalized_id,),
                    ).fetchone()[0]
                )
                if not other_active:
                    raise ValueError("Minimal satu company harus tetap aktif")
            connection.execute(
                "UPDATE companies SET active = ?, updated_at = ? WHERE company_id = ?",
                (int(active), _now(), normalized_id),
            )
            _write_audit(
                connection,
                actor,
                "company.activated" if active else "company.deactivated",
                "company",
                normalized_id,
                {"active": active},
            )
        company = self.get_company_admin(normalized_id)
        if company is None:
            raise RuntimeError("Company gagal diperbarui")
        return company

    def list_ai_runtime_profiles(self) -> list[AIRuntimeProfile]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT profile_id, label, provider, api_key_env, model,
                       base_url, active, api_key_ciphertext, updated_at, last_test_status, last_test_at, catalog_updated_at
                FROM ai_runtime_profiles
                ORDER BY label COLLATE NOCASE
                """
            ).fetchall()
        return [_ai_runtime_profile_from_row(row) for row in rows]

    def get_ai_runtime_profile(
        self, profile_id: object
    ) -> AIRuntimeProfile | None:
        normalized_profile = _validate_runtime_profile_id(profile_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT profile_id, label, provider, api_key_env, model,
                       base_url, active, api_key_ciphertext, updated_at, last_test_status, last_test_at, catalog_updated_at
                FROM ai_runtime_profiles WHERE profile_id = ?
                """,
                (normalized_profile,),
            ).fetchone()
        return _ai_runtime_profile_from_row(row) if row else None

    def create_ai_runtime_profile(
        self,
        profile_id: object,
        label: object,
        provider: object,
        api_key_env: object,
        model: object,
        base_url: object,
        actor: str,
        *,
        active: bool = True,
        api_key: str | None = None,
        discoverable: bool = False,
    ) -> AIRuntimeProfile:
        normalized_label = _validate_runtime_profile_label(label)
        requested_id = str(profile_id or "").strip()
        normalized_profile = (
            _validate_runtime_profile_id(requested_id) if requested_id else ""
        )
        runtime = _validate_runtime_profile_fields(
            provider, api_key_env, model, base_url, encrypted=api_key is not None,
            allow_empty_model=discoverable,
        )
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if not normalized_profile:
                    normalized_profile = _next_unique_identifier(
                        connection,
                        "ai_runtime_profiles",
                        "profile_id",
                        _identifier_from_label(normalized_label, "credential"),
                    )
                ciphertext = (
                    encrypt_api_key(normalized_profile, runtime["provider"], api_key)
                    if api_key is not None else ""
                )
                connection.execute(
                    """
                    INSERT INTO ai_runtime_profiles (
                        profile_id, label, provider, api_key_env, model,
                        base_url, active, created_at, updated_at, api_key_ciphertext
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_profile,
                        normalized_label,
                        runtime["provider"],
                        runtime["api_key_env"],
                        runtime["model"],
                        runtime["base_url"],
                        int(active),
                        now,
                        now,
                        ciphertext,
                    ),
                )
                _write_audit(
                    connection,
                    actor,
                    "ai_runtime_profile.created",
                    "ai_runtime_profile",
                    normalized_profile,
                    {
                        "label": normalized_label,
                        "provider": runtime["provider"],
                        "api_key_env": runtime["api_key_env"],
                        "model": runtime["model"],
                        "active": active,
                        "secret_source": "encrypted" if ciphertext else "environment",
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Credential profile dengan ID tersebut sudah ada") from exc
        profile = self.get_ai_runtime_profile(normalized_profile)
        if profile is None:
            raise RuntimeError("Credential profile gagal disimpan")
        return profile

    def update_ai_runtime_profile(
        self,
        profile_id: object,
        label: object,
        provider: object,
        api_key_env: object,
        model: object,
        base_url: object,
        actor: str,
        *,
        api_key: str | None = None,
    ) -> AIRuntimeProfile:
        normalized_profile = _validate_runtime_profile_id(profile_id)
        normalized_label = _validate_runtime_profile_label(label)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT label, provider, api_key_env, model, base_url, api_key_ciphertext
                FROM ai_runtime_profiles WHERE profile_id = ?
                """,
                (normalized_profile,),
            ).fetchone()
            if current is None:
                raise ValueError("Credential profile tidak ditemukan")
            replacing = bool(api_key and api_key.strip())
            encrypted = bool(current["api_key_ciphertext"]) or replacing
            runtime = _validate_runtime_profile_fields(
                provider, api_key_env or current["api_key_env"], model, base_url, encrypted=encrypted,
                allow_empty_model=not current["model"],
            )
            ciphertext = current["api_key_ciphertext"]
            if encrypted and runtime["provider"] != current["provider"] and not replacing:
                raise ValueError("Isi API key baru saat mengganti provider.")
            if replacing:
                ciphertext = encrypt_api_key(normalized_profile, runtime["provider"], api_key)
            connection.execute("DELETE FROM ai_model_catalog WHERE profile_id = ?", (normalized_profile,))
            connection.execute(
                """
                UPDATE ai_runtime_profiles
                SET label = ?, provider = ?, api_key_env = ?, model = ?,
                    base_url = ?, updated_at = ?, api_key_ciphertext = ?,
                    last_test_status = '', last_test_at = '', catalog_updated_at = ''
                WHERE profile_id = ?
                """,
                (
                    normalized_label,
                    runtime["provider"],
                    runtime["api_key_env"],
                    runtime["model"],
                    runtime["base_url"],
                    _now(),
                    ciphertext,
                    normalized_profile,
                ),
            )
            _write_audit(
                connection,
                actor,
                "ai_runtime_profile.updated",
                "ai_runtime_profile",
                normalized_profile,
                {
                    "label_before": current["label"],
                    "label_after": normalized_label,
                    "provider_before": current["provider"],
                    "provider_after": runtime["provider"],
                    "api_key_env_before": current["api_key_env"],
                    "api_key_env_after": runtime["api_key_env"],
                    "model_before": current["model"],
                    "model_after": runtime["model"],
                    "key_replaced": replacing,
                },
            )
        profile = self.get_ai_runtime_profile(normalized_profile)
        if profile is None:
            raise RuntimeError("Credential profile gagal diperbarui")
        return profile

    def create_ai_connection(self, provider: str, api_key: str, actor: str) -> AIRuntimeProfile:
        if provider not in PROVIDERS:
            raise ValueError("Pilih provider yang didukung")
        return self.create_ai_runtime_profile(
            "", PROVIDERS[provider][0], provider, "", "", "", actor,
            api_key=api_key, discoverable=True,
        )

    def list_ai_models(self, profile_id: str) -> list[dict[str, object]]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT model_id, selectable, reason, fetched_at FROM ai_model_catalog "
                "WHERE profile_id = ? ORDER BY model_id", (profile_id,),
            ).fetchall()]

    def record_model_catalog(self, profile: AIRuntimeProfile, result: CatalogResult, actor: str) -> bool:
        if result.status not in CATALOG_MESSAGES:
            raise ValueError("Status katalog tidak valid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE ai_runtime_profiles SET last_test_status = ?, last_test_at = ? "
                "WHERE profile_id = ? AND updated_at = ? AND active = 1",
                (result.status, _now(), profile.profile_id, profile.updated_at),
            )
            if not changed.rowcount:
                return False
            if result.status == "success":
                connection.execute("DELETE FROM ai_model_catalog WHERE profile_id = ?", (profile.profile_id,))
                now = _now()
                connection.execute("UPDATE ai_runtime_profiles SET catalog_updated_at = ? WHERE profile_id = ?",
                                   (now, profile.profile_id))
                connection.executemany(
                    "INSERT INTO ai_model_catalog (profile_id, model_id, selectable, reason, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(profile.profile_id, m.model_id, int(m.selectable), m.reason, now) for m in result.models],
                )
            _write_audit(connection, actor, "ai_models.refreshed", "ai_runtime_profile", profile.profile_id,
                         {"status": result.status, "model_count": len(result.models)})
            return True

    def get_module_ai_profile(self, module: AIModule, *, backup: bool = False) -> AIRuntimeProfile | None:
        profile_id = module.backup_ai_runtime_profile_id if backup else module.ai_runtime_profile_id
        model = module.backup_ai_model if backup else module.ai_model
        if not profile_id:
            return None
        profile = self.get_ai_runtime_profile(profile_id)
        if profile is None or not profile.active:
            return None
        if model:
            if not any(m["model_id"] == model and m["selectable"] for m in self.list_ai_models(profile_id)):
                return None
            return replace(profile, model=model)
        return profile if profile.model else None  # Preserve pre-catalog Module selections.

    def record_ai_connection_test(
        self, profile: AIRuntimeProfile, status: str, actor: str
    ) -> bool:
        if status not in {"success", "authentication", "rate_limit", "unavailable", "configuration", "request", "response"}:
            raise ValueError("Status tes tidak valid")
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE ai_runtime_profiles SET last_test_status = ?, last_test_at = ? "
                "WHERE profile_id = ? AND updated_at = ?",
                (status, _now(), profile.profile_id, profile.updated_at),
            )
            if result.rowcount:
                _write_audit(
                    connection, actor, "ai_runtime_profile.tested", "ai_runtime_profile",
                    profile.profile_id, {"test_status": status},
                )
            return bool(result.rowcount)

    def set_ai_runtime_profile_active(
        self, profile_id: object, active: bool, actor: str
    ) -> AIRuntimeProfile:
        normalized_profile = _validate_runtime_profile_id(profile_id)
        with self._connect() as connection:
            current = connection.execute(
                "SELECT active FROM ai_runtime_profiles WHERE profile_id = ?",
                (normalized_profile,),
            ).fetchone()
            if current is None:
                raise ValueError("Credential profile tidak ditemukan")
            if bool(current["active"]) != active:
                connection.execute(
                    """
                    UPDATE ai_runtime_profiles SET active = ?, updated_at = ?
                    WHERE profile_id = ?
                    """,
                    (int(active), _now(), normalized_profile),
                )
                if not active:
                    connection.execute(
                        """
                        UPDATE user_sessions SET active_module_id = '', updated_at = ?
                        WHERE active_module_id IN (
                            SELECT module_id FROM modules
                            WHERE ai_runtime_profile_id = ?
                              AND company_id = user_sessions.active_company_id
                        )
                        """,
                        (_now(), normalized_profile),
                    )
                _write_audit(
                    connection,
                    actor,
                    "ai_runtime_profile.activated"
                    if active
                    else "ai_runtime_profile.deactivated",
                    "ai_runtime_profile",
                    normalized_profile,
                    {"active": active},
                )
        profile = self.get_ai_runtime_profile(normalized_profile)
        if profile is None:
            raise RuntimeError("Credential profile gagal diperbarui")
        return profile

    def list_modules_admin(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    m.company_id,
                    c.name AS company_name,
                    m.module_id,
                    m.short_code,
                    m.name,
                    m.description,
                    m.ai_runtime_profile_id,
                    m.backup_ai_runtime_profile_id,
                    backup.label AS backup_ai_label,
                    backup.active AS backup_ai_active,
                    p.label AS ai_runtime_profile_label,
                    p.provider AS ai_provider,
                    p.api_key_env,
                    COALESCE(NULLIF(m.ai_model, ''), p.model) AS ai_model,
                    m.ai_model AS selected_ai_model,
                    m.backup_ai_model AS selected_backup_ai_model,
                    COALESCE(NULLIF(m.backup_ai_model, ''), backup.model) AS backup_ai_model,
                    p.active AS ai_runtime_profile_active,
                    m.active,
                    COUNT(DISTINCT CASE WHEN a.active = 1 THEN a.telegram_id END)
                        AS access_count,
                    v.version_number AS published_version_number,
                    s.draft_updated_at,
                    CASE
                        WHEN s.module_pk IS NULL THEN 0
                        WHEN v.id IS NULL THEN 1
                        WHEN s.draft_content != v.content THEN 1
                        ELSE 0
                    END AS has_unpublished_draft
                FROM modules m
                JOIN companies c ON c.company_id = m.company_id
                LEFT JOIN ai_runtime_profiles p
                    ON p.profile_id = m.ai_runtime_profile_id
                LEFT JOIN ai_runtime_profiles backup
                    ON backup.profile_id = m.backup_ai_runtime_profile_id
                LEFT JOIN module_access a
                    ON a.company_id = m.company_id
                   AND a.module_id = m.module_id
                LEFT JOIN module_playbook_state s ON s.module_pk = m.id
                LEFT JOIN module_playbook_versions v
                    ON v.id = s.published_version_id
                GROUP BY m.id
                ORDER BY c.name COLLATE NOCASE, m.name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_module_admin(
        self, company_id: str, module_id: str
    ) -> AIModule | None:
        normalized_company = _validate_company_id(company_id)
        normalized_module = _validate_module_id(module_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT m.company_id, c.name AS company_name, m.module_id,
                       m.name, m.description, m.ai_runtime_profile_id, m.active,
                       m.backup_ai_runtime_profile_id, m.ai_model, m.backup_ai_model, m.short_code
                FROM modules m
                JOIN companies c ON c.company_id = m.company_id
                WHERE m.company_id = ? AND m.module_id = ?
                """,
                (normalized_company, normalized_module),
            ).fetchone()
        return _module_from_row(row) if row else None

    def create_module(
        self,
        company_id: str,
        module_id: object,
        name: object,
        description: object,
        actor: str,
        ai_runtime_profile_id: object = "",
        active: bool = True,
        backup_ai_runtime_profile_id: object = "",
        ai_model: object = "",
        backup_ai_model: object = "",
        short_code: object = "",
    ) -> AIModule:
        normalized_company = _validate_company_id(company_id)
        normalized_name = _validate_module_name(name)
        normalized_description = _validate_module_description(description)
        normalized_code = _validate_module_short_code(short_code)
        normalized_profile = _validate_runtime_profile_id(ai_runtime_profile_id)
        normalized_backup = _validate_backup_profile_id(
            backup_ai_runtime_profile_id, normalized_profile, ai_model, backup_ai_model
        )
        requested_id = str(module_id or "").strip()
        normalized_module = (
            _validate_module_id(requested_id) if requested_id else ""
        )
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                _require_active_company(connection, normalized_company)
                selected_model = _validate_model_selection(connection, normalized_profile, ai_model)
                selected_backup_model = ""
                if normalized_backup:
                    selected_backup_model = _validate_model_selection(connection, normalized_backup, backup_ai_model)
                _validate_distinct_model_choices(connection, normalized_profile, selected_model,
                                                 normalized_backup, selected_backup_model)
                if not normalized_module:
                    normalized_module = _next_unique_identifier(
                        connection,
                        "modules",
                        "module_id",
                        _identifier_from_label(normalized_name, "module"),
                        where_column="company_id",
                        where_value=normalized_company,
                    )
                _require_unique_module_command(connection, normalized_company, normalized_module, normalized_code)
                connection.execute(
                    """
                    INSERT INTO modules (
                        company_id, module_id, name, description,
                        ai_runtime_profile_id, backup_ai_runtime_profile_id, active,
                        created_at, updated_at, ai_model, backup_ai_model, short_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_company,
                        normalized_module,
                        normalized_name,
                        normalized_description,
                        normalized_profile,
                        normalized_backup or None,
                        int(active),
                        now,
                        now,
                        selected_model,
                        selected_backup_model,
                        normalized_code,
                    ),
                )
                _write_audit(
                    connection,
                    actor,
                    "module.created",
                    "module",
                    f"{normalized_company}:{normalized_module}",
                    {
                        "company_id": normalized_company,
                        "name": normalized_name,
                        "ai_runtime_profile_id": normalized_profile,
                        "backup_ai_runtime_profile_id": normalized_backup,
                        "ai_model": selected_model,
                        "backup_ai_model": selected_backup_model,
                        "short_code": normalized_code,
                        "active": active,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Module dengan ID tersebut sudah ada pada company") from exc
        module = self.get_module_admin(normalized_company, normalized_module)
        if module is None:
            raise RuntimeError("Module gagal disimpan")
        return module

    def update_module(
        self,
        company_id: str,
        module_id: str,
        name: object,
        description: object,
        actor: str,
        ai_runtime_profile_id: object,
        backup_ai_runtime_profile_id: object = None,
        ai_model: object = None,
        backup_ai_model: object = None,
        short_code: object = None,
    ) -> AIModule:
        normalized_company = _validate_company_id(company_id)
        normalized_module = _validate_module_id(module_id)
        normalized_name = _validate_module_name(name)
        normalized_description = _validate_module_description(description)
        normalized_profile = _validate_runtime_profile_id(ai_runtime_profile_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _require_active_runtime_profile(connection, normalized_profile)
            current = connection.execute(
                """
                SELECT name, description, ai_runtime_profile_id,
                       backup_ai_runtime_profile_id, ai_model, backup_ai_model, short_code FROM modules
                WHERE company_id = ? AND module_id = ?
                """,
                (normalized_company, normalized_module),
            ).fetchone()
            if current is None:
                raise ValueError("Module tidak ditemukan")
            normalized_code = _validate_module_short_code(
                current["short_code"] if short_code is None else short_code
            )
            _require_unique_module_command(connection, normalized_company, normalized_module, normalized_code)
            selected_model = _validate_model_selection(
                connection, normalized_profile,
                current["ai_model"] if ai_model is None and current["ai_runtime_profile_id"] == normalized_profile else ai_model,
            )
            backup_id = (current["backup_ai_runtime_profile_id"]
                         if backup_ai_runtime_profile_id is None else backup_ai_runtime_profile_id)
            backup_model = (current["backup_ai_model"]
                            if backup_ai_model is None and backup_id == current["backup_ai_runtime_profile_id"]
                            else backup_ai_model)
            normalized_backup = _validate_backup_profile_id(
                backup_id, normalized_profile, selected_model, backup_model,
            )
            selected_backup_model = ""
            if normalized_backup:
                selected_backup_model = _validate_model_selection(connection, normalized_backup, backup_model)
            _validate_distinct_model_choices(connection, normalized_profile, selected_model,
                                             normalized_backup, selected_backup_model)
            connection.execute(
                """
                UPDATE modules
                SET name = ?, description = ?, ai_runtime_profile_id = ?,
                    backup_ai_runtime_profile_id = ?, updated_at = ?, ai_model = ?, backup_ai_model = ?, short_code = ?
                WHERE company_id = ? AND module_id = ?
                """,
                (
                    normalized_name,
                    normalized_description,
                    normalized_profile,
                    normalized_backup or None,
                    _now(),
                    selected_model,
                    selected_backup_model,
                    normalized_code,
                    normalized_company,
                    normalized_module,
                ),
            )
            _write_audit(
                connection,
                actor,
                "module.updated",
                "module",
                f"{normalized_company}:{normalized_module}",
                {
                    "name_before": current["name"],
                    "name_after": normalized_name,
                    "short_code_before": current["short_code"],
                    "short_code_after": normalized_code,
                    "ai_runtime_profile_before": current["ai_runtime_profile_id"],
                    "ai_runtime_profile_after": normalized_profile,
                    "backup_ai_runtime_profile_before": current["backup_ai_runtime_profile_id"],
                    "backup_ai_runtime_profile_after": normalized_backup,
                    "ai_model_before": current["ai_model"],
                    "ai_model_after": selected_model,
                    "backup_ai_model_before": current["backup_ai_model"],
                    "backup_ai_model_after": selected_backup_model,
                },
            )
        module = self.get_module_admin(normalized_company, normalized_module)
        if module is None:
            raise RuntimeError("Module gagal diperbarui")
        return module

    def set_module_active(
        self, company_id: str, module_id: str, active: bool, actor: str
    ) -> AIModule:
        normalized_company = _validate_company_id(company_id)
        normalized_module = _validate_module_id(module_id)
        with self._connect() as connection:
            current = connection.execute(
                """
                SELECT active FROM modules
                WHERE company_id = ? AND module_id = ?
                """,
                (normalized_company, normalized_module),
            ).fetchone()
            if current is None:
                raise ValueError("Module tidak ditemukan")
            if bool(current["active"]) != active:
                if active:
                    _require_active_company(connection, normalized_company)
                    runtime_row = connection.execute(
                        """
                        SELECT ai_runtime_profile_id, ai_model FROM modules
                        WHERE company_id = ? AND module_id = ?
                        """,
                        (normalized_company, normalized_module),
                    ).fetchone()
                    _validate_model_selection(
                        connection, runtime_row["ai_runtime_profile_id"], runtime_row["ai_model"]
                    )
                connection.execute(
                    """
                    UPDATE modules SET active = ?, updated_at = ?
                    WHERE company_id = ? AND module_id = ?
                    """,
                    (int(active), _now(), normalized_company, normalized_module),
                )
                if not active:
                    connection.execute(
                        """
                        UPDATE user_sessions SET active_module_id = '', updated_at = ?
                        WHERE active_company_id = ? AND active_module_id = ?
                        """,
                        (_now(), normalized_company, normalized_module),
                    )
                _write_audit(
                    connection,
                    actor,
                    "module.activated" if active else "module.deactivated",
                    "module",
                    f"{normalized_company}:{normalized_module}",
                    {"active": active},
                )
        module = self.get_module_admin(normalized_company, normalized_module)
        if module is None:
            raise RuntimeError("Module gagal diperbarui")
        return module

    def get_module_playbook_admin(
        self, company_id: str, module_id: str
    ) -> dict[str, object] | None:
        normalized_company = _validate_company_id(company_id)
        normalized_module = _validate_module_id(module_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    m.id AS module_pk,
                    m.company_id,
                    c.name AS company_name,
                    m.module_id,
                    m.short_code,
                    m.name,
                    m.description,
                    m.ai_runtime_profile_id,
                    m.backup_ai_runtime_profile_id,
                    backup.label AS backup_ai_label,
                    backup.active AS backup_ai_active,
                    p.label AS ai_runtime_profile_label,
                    p.provider AS ai_provider,
                    p.api_key_env,
                    COALESCE(NULLIF(m.ai_model, ''), p.model) AS ai_model,
                    m.ai_model AS selected_ai_model,
                    m.backup_ai_model AS selected_backup_ai_model,
                    COALESCE(NULLIF(m.backup_ai_model, ''), backup.model) AS backup_ai_model,
                    p.base_url AS ai_base_url,
                    p.active AS ai_runtime_profile_active,
                    m.active,
                    s.draft_content,
                    s.draft_updated_by,
                    s.draft_updated_at,
                    v.id AS published_version_id,
                    v.version_number AS published_version_number,
                    v.content AS published_content,
                    v.content_sha256,
                    v.published_by,
                    v.published_at
                FROM modules m
                JOIN companies c ON c.company_id = m.company_id
                LEFT JOIN ai_runtime_profiles p
                    ON p.profile_id = m.ai_runtime_profile_id
                LEFT JOIN ai_runtime_profiles backup
                    ON backup.profile_id = m.backup_ai_runtime_profile_id
                LEFT JOIN module_playbook_state s ON s.module_pk = m.id
                LEFT JOIN module_playbook_versions v
                    ON v.id = s.published_version_id
                WHERE m.company_id = ? AND m.module_id = ?
                """,
                (normalized_company, normalized_module),
            ).fetchone()
        return dict(row) if row else None

    def save_module_playbook_draft(
        self, company_id: str, module_id: str, content: object, actor: str
    ) -> None:
        normalized_company = _validate_company_id(company_id)
        normalized_module = _validate_module_id(module_id)
        playbook = _validate_module_playbook(content)
        now = _now()
        with self._connect() as connection:
            module = connection.execute(
                """
                SELECT id FROM modules WHERE company_id = ? AND module_id = ?
                """,
                (normalized_company, normalized_module),
            ).fetchone()
            if module is None:
                raise ValueError("Module tidak ditemukan")
            connection.execute(
                """
                INSERT INTO module_playbook_state (
                    module_pk, draft_content, draft_updated_by, draft_updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(module_pk) DO UPDATE SET
                    draft_content=excluded.draft_content,
                    draft_updated_by=excluded.draft_updated_by,
                    draft_updated_at=excluded.draft_updated_at
                """,
                (module["id"], playbook, actor, now),
            )
            _write_audit(
                connection,
                actor,
                "module_playbook.draft_saved",
                "module_playbook",
                f"{normalized_company}:{normalized_module}",
                {"character_count": len(playbook)},
            )

    def publish_module_playbook(
        self, company_id: str, module_id: str, actor: str
    ) -> int:
        normalized_company = _validate_company_id(company_id)
        normalized_module = _validate_module_id(module_id)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            module_runtime = connection.execute(
                """
                SELECT ai_runtime_profile_id, ai_model FROM modules
                WHERE company_id = ? AND module_id = ?
                """,
                (normalized_company, normalized_module),
            ).fetchone()
            if module_runtime is None:
                raise ValueError("Module tidak ditemukan")
            _validate_model_selection(
                connection, module_runtime["ai_runtime_profile_id"], module_runtime["ai_model"]
            )
            state = connection.execute(
                """
                SELECT m.id AS module_pk, s.draft_content
                FROM modules m
                JOIN module_playbook_state s ON s.module_pk = m.id
                WHERE m.company_id = ? AND m.module_id = ?
                """,
                (normalized_company, normalized_module),
            ).fetchone()
            if state is None:
                raise ValueError("Simpan draft playbook sebelum publish")
            content = str(state["draft_content"]).strip()
            if not content:
                raise ValueError("Playbook module tidak boleh kosong saat publish")
            next_version = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(version_number), 0) + 1
                    FROM module_playbook_versions WHERE module_pk = ?
                    """,
                    (state["module_pk"],),
                ).fetchone()[0]
            )
            checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
            cursor = connection.execute(
                """
                INSERT INTO module_playbook_versions (
                    module_pk, version_number, content, content_sha256,
                    published_by, published_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (state["module_pk"], next_version, content, checksum, actor, now),
            )
            connection.execute(
                """
                UPDATE module_playbook_state
                SET published_version_id = ?, draft_updated_by = ?,
                    draft_updated_at = ? WHERE module_pk = ?
                """,
                (cursor.lastrowid, actor, now, state["module_pk"]),
            )
            _write_audit(
                connection,
                actor,
                "module_playbook.published",
                "module_playbook",
                f"{normalized_company}:{normalized_module}",
                {
                    "version_number": next_version,
                    "character_count": len(content),
                    "content_sha256": checksum,
                },
            )
        return next_version

    def list_module_playbook_versions(
        self, company_id: str, module_id: str, limit: int = 50
    ) -> list[dict[str, object]]:
        normalized_company = _validate_company_id(company_id)
        normalized_module = _validate_module_id(module_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT v.id, v.version_number, v.content, v.content_sha256,
                       v.published_by, v.published_at
                FROM module_playbook_versions v
                JOIN modules m ON m.id = v.module_pk
                WHERE m.company_id = ? AND m.module_id = ?
                ORDER BY v.version_number DESC LIMIT ?
                """,
                (
                    normalized_company,
                    normalized_module,
                    max(1, min(limit, 200)),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def restore_module_playbook_version_to_draft(
        self,
        company_id: str,
        module_id: str,
        version_id: int,
        actor: str,
    ) -> None:
        normalized_company = _validate_company_id(company_id)
        normalized_module = _validate_module_id(module_id)
        try:
            normalized_version = int(version_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Versi playbook tidak valid") from exc
        with self._connect() as connection:
            version = connection.execute(
                """
                SELECT m.id AS module_pk, v.version_number, v.content
                FROM module_playbook_versions v
                JOIN modules m ON m.id = v.module_pk
                WHERE v.id = ? AND m.company_id = ? AND m.module_id = ?
                """,
                (normalized_version, normalized_company, normalized_module),
            ).fetchone()
            if version is None:
                raise ValueError("Versi playbook tidak ditemukan")
            now = _now()
            connection.execute(
                """
                INSERT INTO module_playbook_state (
                    module_pk, draft_content, draft_updated_by, draft_updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(module_pk) DO UPDATE SET
                    draft_content=excluded.draft_content,
                    draft_updated_by=excluded.draft_updated_by,
                    draft_updated_at=excluded.draft_updated_at
                """,
                (version["module_pk"], version["content"], actor, now),
            )
            _write_audit(
                connection,
                actor,
                "module_playbook.version_restored_to_draft",
                "module_playbook",
                f"{normalized_company}:{normalized_module}",
                {"source_version_number": int(version["version_number"])},
            )

    def get_published_module_playbook(
        self, company_id: str, module_id: str
    ) -> str | None:
        normalized_company = _validate_company_id(company_id)
        normalized_module = _validate_module_id(module_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT v.content
                FROM modules m
                JOIN module_playbook_state s ON s.module_pk = m.id
                JOIN module_playbook_versions v ON v.id = s.published_version_id
                WHERE m.company_id = ? AND m.module_id = ? AND m.active = 1
                """,
                (normalized_company, normalized_module),
            ).fetchone()
        return str(row["content"]) if row else None

    def list_membership_module_access_admin(
        self, telegram_id: int, company_id: str
    ) -> list[dict[str, object]]:
        normalized_user = _validate_telegram_id(telegram_id)
        normalized_company = _validate_company_id(company_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.module_id, m.name, m.description, m.active,
                       CASE WHEN a.active = 1 THEN 1 ELSE 0 END AS allowed
                FROM modules m
                LEFT JOIN module_access a
                    ON a.telegram_id = ?
                   AND a.company_id = m.company_id
                   AND a.module_id = m.module_id
                WHERE m.company_id = ?
                ORDER BY m.name COLLATE NOCASE
                """,
                (normalized_user, normalized_company),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_membership_module_access(
        self,
        telegram_id: int,
        company_id: str,
        module_ids: list[object],
        actor: str,
    ) -> None:
        normalized_user = _validate_telegram_id(telegram_id)
        normalized_company = _validate_company_id(company_id)
        normalized_modules = sorted({_validate_module_id(item) for item in module_ids})
        now = _now()
        with self._connect() as connection:
            membership = connection.execute(
                """
                SELECT active FROM user_company_memberships
                WHERE telegram_id = ? AND company_id = ?
                """,
                (normalized_user, normalized_company),
            ).fetchone()
            if membership is None:
                raise ValueError("Membership tidak ditemukan")
            if normalized_modules:
                placeholders = ",".join("?" for _ in normalized_modules)
                existing = {
                    str(row["module_id"])
                    for row in connection.execute(
                        f"""
                        SELECT module_id FROM modules
                        WHERE company_id = ? AND module_id IN ({placeholders})
                        """,
                        (normalized_company, *normalized_modules),
                    ).fetchall()
                }
                missing = set(normalized_modules) - existing
                if missing:
                    raise ValueError("Module tidak ditemukan pada company membership")
            connection.execute(
                """
                DELETE FROM module_access
                WHERE telegram_id = ? AND company_id = ?
                """,
                (normalized_user, normalized_company),
            )
            for normalized_module in normalized_modules:
                connection.execute(
                    """
                    INSERT INTO module_access (
                        telegram_id, company_id, module_id, active, updated_at
                    ) VALUES (?, ?, ?, 1, ?)
                    """,
                    (normalized_user, normalized_company, normalized_module, now),
                )
            session = connection.execute(
                """
                SELECT active_module_id FROM user_sessions
                WHERE telegram_id = ? AND active_company_id = ?
                """,
                (normalized_user, normalized_company),
            ).fetchone()
            if session and str(session["active_module_id"]) not in normalized_modules:
                connection.execute(
                    """
                    UPDATE user_sessions SET active_module_id = '', updated_at = ?
                    WHERE telegram_id = ?
                    """,
                    (now, normalized_user),
                )
            _write_audit(
                connection,
                actor,
                "module_access.updated",
                "module_access",
                f"{normalized_user}:{normalized_company}",
                {
                    "company_id": normalized_company,
                    "module_count": len(normalized_modules),
                    "module_ids": normalized_modules,
                },
            )

    def list_accessible_modules(
        self, telegram_id: int, company_id: str
    ) -> list[AIModule]:
        normalized_user = _validate_telegram_id(telegram_id)
        normalized_company = _validate_company_id(company_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.company_id, c.name AS company_name, m.module_id,
                       m.name, m.description, m.ai_runtime_profile_id, m.active,
                       m.backup_ai_runtime_profile_id, m.ai_model, m.backup_ai_model, m.short_code
                FROM module_access a
                JOIN modules m
                    ON m.company_id = a.company_id
                   AND m.module_id = a.module_id
                JOIN companies c ON c.company_id = m.company_id
                JOIN ai_runtime_profiles profile
                    ON profile.profile_id = m.ai_runtime_profile_id
                   AND profile.active = 1
                JOIN module_playbook_state state
                    ON state.module_pk = m.id
                   AND state.published_version_id IS NOT NULL
                JOIN user_company_memberships membership
                    ON membership.telegram_id = a.telegram_id
                   AND membership.company_id = a.company_id
                WHERE a.telegram_id = ? AND a.company_id = ?
                  AND a.active = 1 AND m.active = 1
                  AND c.active = 1 AND membership.active = 1
                ORDER BY m.name COLLATE NOCASE
                """,
                (normalized_user, normalized_company),
            ).fetchall()
        return [_module_from_row(row) for row in rows]

    def get_active_module(
        self, telegram_id: int, company_id: str
    ) -> AIModule | None:
        modules = self.list_accessible_modules(telegram_id, company_id)
        with self._connect() as connection:
            session = connection.execute(
                """
                SELECT active_module_id FROM user_sessions
                WHERE telegram_id = ? AND active_company_id = ?
                """,
                (_validate_telegram_id(telegram_id), _validate_company_id(company_id)),
            ).fetchone()
        if not session or not str(session["active_module_id"]):
            return None
        return next(
            (
                module
                for module in modules
                if module.module_id == str(session["active_module_id"])
            ),
            None,
        )

    def set_active_module(
        self, telegram_id: int, module_id: str, *, short_code_only: bool = False
    ) -> AIModule | None:
        membership = self.get_active_membership(telegram_id)
        if membership is None:
            return None
        requested_module = _validate_module_id(module_id)
        module = next(
            (
                item
                for item in self.list_accessible_modules(
                    telegram_id, membership.company_id
                )
                if item.short_code == requested_module
                or (not short_code_only and item.module_id == requested_module)
            ),
            None,
        )
        if module is None:
            return None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_sessions (
                    telegram_id, active_company_id, active_module_id, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    active_company_id=excluded.active_company_id,
                    active_module_id=excluded.active_module_id,
                    updated_at=excluded.updated_at
                """,
                (telegram_id, membership.company_id, module.module_id, _now()),
            )
        return module

    def clear_active_module(self, telegram_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE user_sessions SET active_module_id = '', updated_at = ?
                WHERE telegram_id = ?
                """,
                (_now(), _validate_telegram_id(telegram_id)),
            )

    def get_company_instruction_admin(
        self, company_id: str
    ) -> dict[str, object] | None:
        normalized_id = _validate_company_id(company_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    c.company_id,
                    c.name AS company_name,
                    c.active AS company_active,
                    s.draft_content,
                    s.draft_updated_by,
                    s.draft_updated_at,
                    v.id AS published_version_id,
                    v.version_number AS published_version_number,
                    v.content AS published_content,
                    v.published_by,
                    v.published_at
                FROM companies c
                LEFT JOIN company_instruction_state s
                    ON s.company_id = c.company_id
                LEFT JOIN company_instruction_versions v
                    ON v.id = s.published_version_id
                WHERE c.company_id = ?
                """,
                (normalized_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_company_instructions_admin(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    c.company_id,
                    c.name AS company_name,
                    c.active AS company_active,
                    c.instruction_file,
                    s.draft_updated_at,
                    s.draft_updated_by,
                    v.version_number AS published_version_number,
                    v.published_at,
                    CASE
                        WHEN s.company_id IS NULL THEN 0
                        WHEN v.id IS NULL THEN 1
                        WHEN s.draft_content != v.content THEN 1
                        ELSE 0
                    END AS has_unpublished_draft
                FROM companies c
                LEFT JOIN company_instruction_state s
                    ON s.company_id = c.company_id
                LEFT JOIN company_instruction_versions v
                    ON v.id = s.published_version_id
                ORDER BY c.name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_company_instruction_versions(
        self, company_id: str, limit: int = 50
    ) -> list[dict[str, object]]:
        normalized_id = _validate_company_id(company_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, company_id, version_number, content,
                       published_by, published_at
                FROM company_instruction_versions
                WHERE company_id = ?
                ORDER BY version_number DESC
                LIMIT ?
                """,
                (normalized_id, max(1, min(limit, 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_published_company_instruction(self, company_id: str) -> str | None:
        normalized_id = _validate_company_id(company_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT v.content
                FROM company_instruction_state s
                JOIN company_instruction_versions v
                    ON v.id = s.published_version_id
                WHERE s.company_id = ?
                """,
                (normalized_id,),
            ).fetchone()
        return str(row["content"]) if row else None

    def save_company_instruction_draft(
        self, company_id: str, content: object, actor: str
    ) -> None:
        normalized_id = _validate_company_id(company_id)
        instruction = _validate_company_instruction(content)
        now = _now()
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM companies WHERE company_id = ?", (normalized_id,)
            ).fetchone() is None:
                raise ValueError("Company tidak ditemukan")
            connection.execute(
                """
                INSERT INTO company_instruction_state (
                    company_id, draft_content, draft_updated_by, draft_updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(company_id) DO UPDATE SET
                    draft_content=excluded.draft_content,
                    draft_updated_by=excluded.draft_updated_by,
                    draft_updated_at=excluded.draft_updated_at
                """,
                (normalized_id, instruction, actor, now),
            )
            _write_audit(
                connection,
                actor,
                "company_instruction.draft_saved",
                "company_instruction",
                normalized_id,
                {"character_count": len(instruction)},
            )

    def publish_company_instruction(self, company_id: str, actor: str) -> int:
        normalized_id = _validate_company_id(company_id)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                """
                SELECT draft_content FROM company_instruction_state
                WHERE company_id = ?
                """,
                (normalized_id,),
            ).fetchone()
            if state is None:
                raise ValueError("Simpan draft sebelum publish")
            next_version = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(version_number), 0) + 1
                    FROM company_instruction_versions
                    WHERE company_id = ?
                    """,
                    (normalized_id,),
                ).fetchone()[0]
            )
            cursor = connection.execute(
                """
                INSERT INTO company_instruction_versions (
                    company_id, version_number, content, published_by, published_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    normalized_id,
                    next_version,
                    state["draft_content"],
                    actor,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE company_instruction_state
                SET published_version_id = ?, draft_updated_by = ?,
                    draft_updated_at = ?
                WHERE company_id = ?
                """,
                (cursor.lastrowid, actor, now, normalized_id),
            )
            _write_audit(
                connection,
                actor,
                "company_instruction.published",
                "company_instruction",
                normalized_id,
                {
                    "version_number": next_version,
                    "character_count": len(str(state["draft_content"])),
                },
            )
        return next_version

    def restore_company_instruction_version_to_draft(
        self, company_id: str, version_id: int, actor: str
    ) -> None:
        normalized_id = _validate_company_id(company_id)
        try:
            normalized_version_id = int(version_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Versi instruction tidak valid") from exc
        with self._connect() as connection:
            version = connection.execute(
                """
                SELECT version_number, content
                FROM company_instruction_versions
                WHERE id = ? AND company_id = ?
                """,
                (normalized_version_id, normalized_id),
            ).fetchone()
            if version is None:
                raise ValueError("Versi instruction tidak ditemukan")
            now = _now()
            connection.execute(
                """
                INSERT INTO company_instruction_state (
                    company_id, draft_content, draft_updated_by, draft_updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(company_id) DO UPDATE SET
                    draft_content=excluded.draft_content,
                    draft_updated_by=excluded.draft_updated_by,
                    draft_updated_at=excluded.draft_updated_at
                """,
                (normalized_id, version["content"], actor, now),
            )
            _write_audit(
                connection,
                actor,
                "company_instruction.version_restored_to_draft",
                "company_instruction",
                normalized_id,
                {"source_version_number": int(version["version_number"])},
            )

    def list_company_knowledge_summary_admin(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    c.company_id,
                    c.name AS company_name,
                    c.active AS company_active,
                    c.knowledge_dir,
                    COUNT(d.id) AS document_count,
                    COUNT(CASE WHEN d.active = 1 THEN 1 END)
                        AS active_document_count,
                    COUNT(CASE WHEN d.published_version_id IS NOT NULL THEN 1 END)
                        AS published_document_count,
                    COUNT(CASE
                        WHEN d.active = 1 AND d.published_version_id IS NOT NULL
                        THEN 1 END) AS live_document_count,
                    COUNT(CASE
                        WHEN d.id IS NOT NULL AND (
                            v.id IS NULL OR d.title != v.title
                            OR d.draft_content != v.content
                        ) THEN 1 END) AS pending_draft_count
                FROM companies c
                LEFT JOIN knowledge_documents d ON d.company_id = c.company_id
                LEFT JOIN knowledge_document_versions v
                    ON v.id = d.published_version_id
                GROUP BY c.company_id
                ORDER BY c.name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_knowledge_documents_admin(
        self, company_id: str
    ) -> list[dict[str, object]]:
        normalized_company = _validate_company_id(company_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    d.id,
                    d.company_id,
                    d.document_key,
                    d.title,
                    d.active,
                    d.draft_updated_by,
                    d.draft_updated_at,
                    d.published_version_id,
                    v.version_number AS published_version_number,
                    v.title AS published_title,
                    v.content AS published_content,
                    v.published_at,
                    CASE
                        WHEN v.id IS NULL OR d.title != v.title
                             OR d.draft_content != v.content
                        THEN 1 ELSE 0
                    END AS has_unpublished_draft
                FROM knowledge_documents d
                LEFT JOIN knowledge_document_versions v
                    ON v.id = d.published_version_id
                WHERE d.company_id = ?
                ORDER BY d.title COLLATE NOCASE
                """,
                (normalized_company,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_knowledge_document_admin(
        self, company_id: str, document_key: str
    ) -> dict[str, object] | None:
        normalized_company = _validate_company_id(company_id)
        normalized_key = _validate_knowledge_document_key(document_key)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    d.*,
                    c.name AS company_name,
                    c.active AS company_active,
                    v.version_number AS published_version_number,
                    v.title AS published_title,
                    v.content AS published_content,
                    v.published_by,
                    v.published_at
                FROM knowledge_documents d
                JOIN companies c ON c.company_id = d.company_id
                LEFT JOIN knowledge_document_versions v
                    ON v.id = d.published_version_id
                WHERE d.company_id = ? AND d.document_key = ?
                """,
                (normalized_company, normalized_key),
            ).fetchone()
        return dict(row) if row else None

    def create_knowledge_document(
        self,
        company_id: str,
        document_key: object,
        title: object,
        content: object,
        actor: str,
        *,
        source_filename: object = "",
        source_media_type: object = "",
        source_size_bytes: object = 0,
        source_sha256: object = "",
    ) -> dict[str, object]:
        normalized_company = _validate_company_id(company_id)
        normalized_title = _validate_knowledge_title(title)
        requested_key = str(document_key or "").strip()
        normalized_key = (
            _validate_knowledge_document_key(requested_key) if requested_key else ""
        )
        normalized_content = _validate_knowledge_content(content)
        source = _validate_knowledge_source(
            source_filename,
            source_media_type,
            source_size_bytes,
            source_sha256,
        )
        now = _now()
        try:
            with self._connect() as connection:
                if connection.execute(
                    "SELECT 1 FROM companies WHERE company_id = ?",
                    (normalized_company,),
                ).fetchone() is None:
                    raise ValueError("Company tidak ditemukan")
                if not normalized_key:
                    normalized_key = _next_unique_identifier(
                        connection,
                        "knowledge_documents",
                        "document_key",
                        _identifier_from_label(normalized_title, "document"),
                        where_column="company_id",
                        where_value=normalized_company,
                    )
                connection.execute(
                    """
                    INSERT INTO knowledge_documents (
                        company_id, document_key, title, draft_content,
                        draft_updated_by, draft_updated_at, active,
                        source_filename, source_media_type, source_size_bytes,
                        source_sha256, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_company,
                        normalized_key,
                        normalized_title,
                        normalized_content,
                        actor,
                        now,
                        source["filename"],
                        source["media_type"],
                        source["size_bytes"],
                        source["sha256"],
                        now,
                        now,
                    ),
                )
                _write_audit(
                    connection,
                    actor,
                    "knowledge_document.created",
                    "knowledge_document",
                    f"{normalized_company}:{normalized_key}",
                    {
                        "title": normalized_title,
                        "character_count": len(normalized_content),
                        "source_filename": source["filename"],
                        "source_media_type": source["media_type"],
                        "source_size_bytes": source["size_bytes"],
                        "source_sha256": source["sha256"],
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"Document key '{normalized_key}' sudah digunakan pada company ini"
            ) from exc
        document = self.get_knowledge_document_admin(
            normalized_company, normalized_key
        )
        if document is None:
            raise RuntimeError("Knowledge document gagal disimpan")
        return document

    def save_knowledge_document_draft(
        self,
        company_id: str,
        document_key: str,
        title: object,
        content: object,
        actor: str,
    ) -> None:
        normalized_company = _validate_company_id(company_id)
        normalized_key = _validate_knowledge_document_key(document_key)
        normalized_title = _validate_knowledge_title(title)
        normalized_content = _validate_knowledge_content(content)
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_documents
                SET title = ?, draft_content = ?, draft_updated_by = ?,
                    draft_updated_at = ?, updated_at = ?
                WHERE company_id = ? AND document_key = ?
                """,
                (
                    normalized_title,
                    normalized_content,
                    actor,
                    now,
                    now,
                    normalized_company,
                    normalized_key,
                ),
            )
            if not cursor.rowcount:
                raise ValueError("Knowledge document tidak ditemukan")
            _write_audit(
                connection,
                actor,
                "knowledge_document.draft_saved",
                "knowledge_document",
                f"{normalized_company}:{normalized_key}",
                {
                    "title": normalized_title,
                    "character_count": len(normalized_content),
                },
            )

    def publish_knowledge_document(
        self, company_id: str, document_key: str, actor: str
    ) -> int:
        normalized_company = _validate_company_id(company_id)
        normalized_key = _validate_knowledge_document_key(document_key)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            document = connection.execute(
                """
                SELECT id, title, draft_content
                FROM knowledge_documents
                WHERE company_id = ? AND document_key = ?
                """,
                (normalized_company, normalized_key),
            ).fetchone()
            if document is None:
                raise ValueError("Knowledge document tidak ditemukan")
            next_version = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(version_number), 0) + 1
                    FROM knowledge_document_versions
                    WHERE document_id = ?
                    """,
                    (document["id"],),
                ).fetchone()[0]
            )
            content = str(document["draft_content"])
            checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
            cursor = connection.execute(
                """
                INSERT INTO knowledge_document_versions (
                    document_id, version_number, title, content,
                    content_sha256, published_by, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document["id"],
                    next_version,
                    document["title"],
                    content,
                    checksum,
                    actor,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE knowledge_documents
                SET published_version_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (cursor.lastrowid, now, document["id"]),
            )
            _write_audit(
                connection,
                actor,
                "knowledge_document.published",
                "knowledge_document",
                f"{normalized_company}:{normalized_key}",
                {
                    "version_number": next_version,
                    "title": str(document["title"]),
                    "character_count": len(content),
                    "content_sha256": checksum,
                },
            )
        return next_version

    def list_knowledge_document_versions(
        self, company_id: str, document_key: str, limit: int = 50
    ) -> list[dict[str, object]]:
        document = self.get_knowledge_document_admin(company_id, document_key)
        if document is None:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, version_number, title, content, content_sha256,
                       published_by, published_at
                FROM knowledge_document_versions
                WHERE document_id = ?
                ORDER BY version_number DESC
                LIMIT ?
                """,
                (document["id"], max(1, min(limit, 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def restore_knowledge_document_version_to_draft(
        self,
        company_id: str,
        document_key: str,
        version_id: int,
        actor: str,
    ) -> None:
        normalized_company = _validate_company_id(company_id)
        normalized_key = _validate_knowledge_document_key(document_key)
        try:
            normalized_version_id = int(version_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Versi knowledge tidak valid") from exc
        with self._connect() as connection:
            version = connection.execute(
                """
                SELECT v.version_number, v.title, v.content, d.id AS document_id
                FROM knowledge_document_versions v
                JOIN knowledge_documents d ON d.id = v.document_id
                WHERE v.id = ? AND d.company_id = ? AND d.document_key = ?
                """,
                (normalized_version_id, normalized_company, normalized_key),
            ).fetchone()
            if version is None:
                raise ValueError("Versi knowledge tidak ditemukan")
            now = _now()
            connection.execute(
                """
                UPDATE knowledge_documents
                SET title = ?, draft_content = ?, draft_updated_by = ?,
                    draft_updated_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    version["title"],
                    version["content"],
                    actor,
                    now,
                    now,
                    version["document_id"],
                ),
            )
            _write_audit(
                connection,
                actor,
                "knowledge_document.version_restored_to_draft",
                "knowledge_document",
                f"{normalized_company}:{normalized_key}",
                {"source_version_number": int(version["version_number"])},
            )

    def set_knowledge_document_active(
        self, company_id: str, document_key: str, active: bool, actor: str
    ) -> None:
        normalized_company = _validate_company_id(company_id)
        normalized_key = _validate_knowledge_document_key(document_key)
        with self._connect() as connection:
            current = connection.execute(
                """
                SELECT active FROM knowledge_documents
                WHERE company_id = ? AND document_key = ?
                """,
                (normalized_company, normalized_key),
            ).fetchone()
            if current is None:
                raise ValueError("Knowledge document tidak ditemukan")
            if bool(current["active"]) != active:
                connection.execute(
                    """
                    UPDATE knowledge_documents SET active = ?, updated_at = ?
                    WHERE company_id = ? AND document_key = ?
                    """,
                    (int(active), _now(), normalized_company, normalized_key),
                )
                _write_audit(
                    connection,
                    actor,
                    (
                        "knowledge_document.activated"
                        if active
                        else "knowledge_document.deactivated"
                    ),
                    "knowledge_document",
                    f"{normalized_company}:{normalized_key}",
                    {"active": active},
                )

    def get_published_company_knowledge(
        self, company_id: str, max_chars: int
    ) -> str | None:
        normalized_company = _validate_company_id(company_id)
        with self._connect() as connection:
            published_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM knowledge_documents
                    WHERE company_id = ? AND published_version_id IS NOT NULL
                    """,
                    (normalized_company,),
                ).fetchone()[0]
            )
            if not published_count:
                return None
            rows = connection.execute(
                """
                SELECT d.document_key, v.title, v.content
                FROM knowledge_documents d
                JOIN knowledge_document_versions v
                    ON v.id = d.published_version_id
                WHERE d.company_id = ? AND d.active = 1
                ORDER BY v.title COLLATE NOCASE, d.document_key
                """,
                (normalized_company,),
            ).fetchall()
        if max_chars <= 0:
            return ""
        sections: list[str] = []
        used = 0
        for row in rows:
            section = (
                f"## Sumber: {row['document_key']} — {row['title']}\n\n"
                f"{row['content']}"
            )
            separator = "\n\n---\n\n" if sections else ""
            remaining = max_chars - used - len(separator)
            if remaining <= 0:
                break
            sections.append(section[:remaining])
            used += len(separator) + len(sections[-1])
        return "\n\n---\n\n".join(sections)

    def list_admin_audit_events(self, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT actor, action, entity_type, entity_id, details_json, created_at
                FROM admin_audit_events
                ORDER BY id DESC LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def write_admin_event(
        self, actor: str, action: str, entity_type: str = "admin", entity_id: str = ""
    ) -> None:
        with self._connect() as connection:
            _write_audit(connection, actor, action, entity_type, entity_id, {})

    def list_memberships(self, telegram_id: int) -> list[Membership]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*, c.name AS company_name
                FROM user_company_memberships m
                JOIN companies c ON c.company_id = m.company_id
                WHERE m.telegram_id = ? AND m.active = 1 AND c.active = 1
                ORDER BY m.is_default DESC, c.name COLLATE NOCASE
                """,
                (telegram_id,),
            ).fetchall()
        return [_membership_from_row(row) for row in rows]

    def list_memberships_admin(self, telegram_id: int) -> list[Membership]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*, c.name AS company_name
                FROM user_company_memberships m
                JOIN companies c ON c.company_id = m.company_id
                WHERE m.telegram_id = ?
                ORDER BY m.is_default DESC, c.name COLLATE NOCASE
                """,
                (_validate_telegram_id(telegram_id),),
            ).fetchall()
        return [_membership_from_row(row) for row in rows]

    def get_membership_admin(
        self, telegram_id: int, company_id: str
    ) -> Membership | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT m.*, c.name AS company_name
                FROM user_company_memberships m
                JOIN companies c ON c.company_id = m.company_id
                WHERE m.telegram_id = ? AND m.company_id = ?
                """,
                (
                    _validate_telegram_id(telegram_id),
                    _validate_company_id(company_id),
                ),
            ).fetchone()
        return _membership_from_row(row) if row else None

    def create_membership(
        self,
        telegram_id: int,
        company_id: object,
        job_title: object,
        division: object,
        role_level: object,
        communication_profile: object,
        custom_instruction: object,
        is_default: bool,
        actor: str,
    ) -> Membership:
        normalized_user = _validate_telegram_id(telegram_id)
        values = _validate_membership_fields(
            company_id,
            job_title,
            division,
            role_level,
            communication_profile,
            custom_instruction,
        )
        now = _now()
        try:
            with self._connect() as connection:
                _require_user(connection, normalized_user)
                _require_active_company(connection, values["company_id"])
                has_default = bool(
                    connection.execute(
                        """
                        SELECT 1 FROM user_company_memberships
                        WHERE telegram_id = ? AND active = 1 AND is_default = 1
                        LIMIT 1
                        """,
                        (normalized_user,),
                    ).fetchone()
                )
                make_default = is_default or not has_default
                if make_default:
                    connection.execute(
                        """
                        UPDATE user_company_memberships
                        SET is_default = 0, updated_at = ?
                        WHERE telegram_id = ?
                        """,
                        (now, normalized_user),
                    )
                connection.execute(
                    """
                    INSERT INTO user_company_memberships (
                        telegram_id, company_id, job_title, division, role_level,
                        communication_profile, custom_instruction, is_default,
                        active, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        normalized_user,
                        values["company_id"],
                        values["job_title"],
                        values["division"],
                        values["role_level"],
                        values["communication_profile"],
                        values["custom_instruction"],
                        int(make_default),
                        now,
                    ),
                )
                _write_audit(
                    connection,
                    actor,
                    "membership.created",
                    "membership",
                    f"{normalized_user}:{values['company_id']}",
                    _membership_audit_details(values, is_default=make_default),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("User sudah memiliki membership pada company tersebut") from exc
        membership = self.get_membership_admin(
            normalized_user, values["company_id"]
        )
        if membership is None:
            raise RuntimeError("Membership gagal disimpan")
        return membership

    def update_membership(
        self,
        telegram_id: int,
        company_id: str,
        job_title: object,
        division: object,
        role_level: object,
        communication_profile: object,
        custom_instruction: object,
        is_default: bool,
        actor: str,
        *,
        preserve_legacy_context: bool = False,
    ) -> Membership:
        normalized_user = _validate_telegram_id(telegram_id)
        values = _validate_membership_fields(
            company_id,
            job_title,
            division,
            role_level,
            communication_profile,
            custom_instruction,
        )
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT * FROM user_company_memberships
                WHERE telegram_id = ? AND company_id = ?
                """,
                (normalized_user, values["company_id"]),
            ).fetchone()
            if current is None:
                raise ValueError("Membership tidak ditemukan")
            if preserve_legacy_context:
                # Keep legacy values exactly, within the same write transaction.
                # Runtime derives its effective profile from role_level instead.
                values["division"] = current["division"]
                values["communication_profile"] = current["communication_profile"]
            if bool(current["active"]):
                _require_active_company(connection, values["company_id"])
            if bool(current["is_default"]) and not is_default:
                other_default = connection.execute(
                    """
                    SELECT 1 FROM user_company_memberships
                    WHERE telegram_id = ? AND company_id != ?
                      AND active = 1 AND is_default = 1
                    LIMIT 1
                    """,
                    (normalized_user, values["company_id"]),
                ).fetchone()
                if not other_default:
                    raise ValueError(
                        "Pilih membership lain sebagai default sebelum melepas default ini"
                    )
            if is_default:
                connection.execute(
                    """
                    UPDATE user_company_memberships
                    SET is_default = 0, updated_at = ?
                    WHERE telegram_id = ?
                    """,
                    (now, normalized_user),
                )
            connection.execute(
                """
                UPDATE user_company_memberships SET
                    job_title = ?, division = ?, role_level = ?,
                    communication_profile = ?, custom_instruction = ?,
                    is_default = ?, updated_at = ?
                WHERE telegram_id = ? AND company_id = ?
                """,
                (
                    values["job_title"],
                    values["division"],
                    values["role_level"],
                    values["communication_profile"],
                    values["custom_instruction"],
                    int(is_default),
                    now,
                    normalized_user,
                    values["company_id"],
                ),
            )
            _write_audit(
                connection,
                actor,
                "membership.updated",
                "membership",
                f"{normalized_user}:{values['company_id']}",
                _membership_audit_details(values, is_default=is_default),
            )
        membership = self.get_membership_admin(
            normalized_user, values["company_id"]
        )
        if membership is None:
            raise RuntimeError("Membership gagal diperbarui")
        return membership

    def set_membership_active(
        self, telegram_id: int, company_id: str, active: bool, actor: str
    ) -> Membership:
        normalized_user = _validate_telegram_id(telegram_id)
        normalized_company = _validate_company_id(company_id)
        now = _now()
        with self._connect() as connection:
            current = connection.execute(
                """
                SELECT active, is_default FROM user_company_memberships
                WHERE telegram_id = ? AND company_id = ?
                """,
                (normalized_user, normalized_company),
            ).fetchone()
            if current is None:
                raise ValueError("Membership tidak ditemukan")
            if bool(current["active"]) != active:
                if active:
                    _require_active_company(connection, normalized_company)
                    has_default = bool(
                        connection.execute(
                            """
                            SELECT 1 FROM user_company_memberships
                            WHERE telegram_id = ? AND active = 1 AND is_default = 1
                            LIMIT 1
                            """,
                            (normalized_user,),
                        ).fetchone()
                    )
                    is_default = not has_default
                else:
                    if bool(current["is_default"]):
                        other_active = connection.execute(
                            """
                            SELECT 1 FROM user_company_memberships
                            WHERE telegram_id = ? AND company_id != ? AND active = 1
                            LIMIT 1
                            """,
                            (normalized_user, normalized_company),
                        ).fetchone()
                        if other_active:
                            raise ValueError(
                                "Jadikan membership lain sebagai default sebelum menonaktifkan ini"
                            )
                    is_default = False
                connection.execute(
                    """
                    UPDATE user_company_memberships
                    SET active = ?, is_default = ?, updated_at = ?
                    WHERE telegram_id = ? AND company_id = ?
                    """,
                    (
                        int(active),
                        int(is_default),
                        now,
                        normalized_user,
                        normalized_company,
                    ),
                )
                if not active:
                    connection.execute(
                        """
                        DELETE FROM user_sessions
                        WHERE telegram_id = ? AND active_company_id = ?
                        """,
                        (normalized_user, normalized_company),
                    )
                _write_audit(
                    connection,
                    actor,
                    "membership.activated" if active else "membership.deactivated",
                    "membership",
                    f"{normalized_user}:{normalized_company}",
                    {"active": active, "is_default": is_default},
                )
        membership = self.get_membership_admin(normalized_user, normalized_company)
        if membership is None:
            raise RuntimeError("Membership gagal diperbarui")
        return membership

    def get_active_membership(self, telegram_id: int) -> Membership | None:
        memberships = self.list_memberships(telegram_id)
        if not memberships:
            return None

        with self._connect() as connection:
            session = connection.execute(
                "SELECT active_company_id FROM user_sessions WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
        if session:
            for membership in memberships:
                if membership.company_id == session["active_company_id"]:
                    return membership

        defaults = [membership for membership in memberships if membership.is_default]
        if len(defaults) == 1:
            return defaults[0]
        if len(memberships) == 1:
            return memberships[0]
        return None

    def set_active_company(self, telegram_id: int, company_id: str) -> Membership | None:
        requested_id = company_id.strip().casefold()
        membership = next(
            (
                item
                for item in self.list_memberships(telegram_id)
                if item.company_id == requested_id
            ),
            None,
        )
        if membership is None:
            return None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_sessions (telegram_id, active_company_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    active_company_id=excluded.active_company_id,
                    active_module_id='',
                    updated_at=excluded.updated_at
                """,
                (telegram_id, membership.company_id, _now()),
            )
        return membership

    def get_admin_dashboard_stats(self) -> dict[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM companies WHERE active = 1) AS companies,
                    (SELECT COUNT(*) FROM users WHERE active = 1) AS users,
                    (
                        SELECT COUNT(*) FROM user_company_memberships
                        WHERE active = 1
                    ) AS memberships,
                    (SELECT COUNT(*) FROM messages) AS messages
                """
            ).fetchone()
        return {key: int(row[key]) for key in row.keys()}

    def list_companies_admin(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    c.company_id,
                    c.name,
                    c.profile_file,
                    c.instruction_file,
                    c.knowledge_dir,
                    c.active,
                    c.updated_at,
                    COUNT(DISTINCT CASE WHEN m.active = 1 THEN m.telegram_id END)
                        AS member_count,
                    COUNT(DISTINCT msg.id) AS message_count
                FROM companies c
                LEFT JOIN user_company_memberships m
                    ON m.company_id = c.company_id
                LEFT JOIN messages msg
                    ON msg.company_id = c.company_id
                GROUP BY c.company_id
                ORDER BY c.name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_users_admin(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    u.telegram_id,
                    u.name,
                    u.active,
                    u.updated_at,
                    COUNT(CASE WHEN m.active = 1 THEN 1 END) AS membership_count,
                    GROUP_CONCAT(
                        CASE WHEN m.active = 1
                        THEN c.name || ' · ' || COALESCE(NULLIF(m.job_title, ''), '-')
                        END,
                        ' | '
                    ) AS membership_summary
                FROM users u
                LEFT JOIN user_company_memberships m
                    ON m.telegram_id = u.telegram_id
                LEFT JOIN companies c
                    ON c.company_id = m.company_id
                GROUP BY u.telegram_id
                ORDER BY u.name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def add_message(
        self,
        telegram_id: int,
        role: str,
        content: str,
        company_id: str = "",
        module_id: str = "",
    ) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("Role pesan tidak valid")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO messages (
                    telegram_id, company_id, module_id, role, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (telegram_id, company_id, module_id, role, content, _now()),
            )

    def set_pending_request(self, telegram_id, company_id, module_id, content, context_hash):
        with closing(self._connect()) as c, c:
            c.execute("""INSERT INTO module_pending_requests VALUES(?,?,?,?,?,?)
                ON CONFLICT(telegram_id,company_id,module_id) DO UPDATE SET
                content=excluded.content,context_hash=excluded.context_hash,created_at=excluded.created_at""",
                (telegram_id, company_id, module_id, content, context_hash, _now()))

    def get_pending_request(self, telegram_id, company_id, module_id):
        with closing(self._connect()) as c:
            row = c.execute("SELECT content,context_hash FROM module_pending_requests WHERE telegram_id=? AND company_id=? AND module_id=?",
                            (telegram_id, company_id, module_id)).fetchone()
            return dict(row) if row else None

    def complete_pending_request(self, telegram_id, company_id, module_id, content, answer, context_hash):
        # Save the successful pair and remove the pending input atomically.
        with closing(self._connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            result = c.execute("DELETE FROM module_pending_requests WHERE telegram_id=? AND company_id=? AND module_id=? AND content=? AND context_hash=?",
                               (telegram_id, company_id, module_id, content, context_hash))
            if result.rowcount != 1:
                raise ValueError("Pending request changed")
            c.executemany("INSERT INTO messages(telegram_id,company_id,module_id,role,content,created_at) VALUES(?,?,?,?,?,?)",
                          [(telegram_id,company_id,module_id,role,text,_now()) for role,text in (("user",content),("assistant",answer))])

    def get_history(
        self,
        telegram_id: int,
        limit: int,
        company_id: str = "",
        module_id: str = "",
    ) -> list[dict[str, str]]:
        if limit <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content FROM messages
                WHERE telegram_id = ? AND company_id = ? AND module_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (telegram_id, company_id, module_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def clear_history(
        self, telegram_id: int, company_id: str = "", module_id: str = ""
    ) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM module_pending_requests WHERE telegram_id=? AND company_id=? AND module_id=?",
                               (telegram_id, company_id, module_id))
            connection.execute(
                """
                DELETE FROM messages
                WHERE telegram_id = ? AND company_id = ? AND module_id = ?
                """,
                (telegram_id, company_id, module_id),
            )

    def prune_history(
        self,
        telegram_id: int,
        company_id: str = "",
        module_id: str = "",
        keep: int = 100,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM messages
                WHERE telegram_id = ? AND company_id = ? AND module_id = ?
                  AND id NOT IN (
                    SELECT id FROM messages
                    WHERE telegram_id = ? AND company_id = ? AND module_id = ?
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (
                    telegram_id,
                    company_id,
                    module_id,
                    telegram_id,
                    company_id,
                    module_id,
                    keep,
                ),
            )


def _membership_from_row(row: sqlite3.Row) -> Membership:
    return Membership(
        telegram_id=row["telegram_id"],
        company_id=row["company_id"],
        company_name=row["company_name"],
        job_title=row["job_title"],
        division=row["division"],
        role_level=row["role_level"],
        communication_profile=row["communication_profile"],
        custom_instruction=row["custom_instruction"],
        is_default=bool(row["is_default"]),
        active=bool(row["active"]),
    )


def _module_from_row(row: sqlite3.Row) -> AIModule:
    return AIModule(
        company_id=row["company_id"],
        company_name=row["company_name"],
        module_id=row["module_id"],
        short_code=row["short_code"],
        name=row["name"],
        description=row["description"],
        ai_runtime_profile_id=str(row["ai_runtime_profile_id"] or ""),
        backup_ai_runtime_profile_id=str(row["backup_ai_runtime_profile_id"] or ""),
        ai_model=row["ai_model"],
        backup_ai_model=row["backup_ai_model"],
        active=bool(row["active"]),
    )


def _ai_runtime_profile_from_row(row: sqlite3.Row) -> AIRuntimeProfile:
    return AIRuntimeProfile(
        profile_id=row["profile_id"],
        label=row["label"],
        provider=row["provider"],
        api_key_env=row["api_key_env"],
        model=row["model"],
        base_url=row["base_url"],
        active=bool(row["active"]),
        api_key_ciphertext=row["api_key_ciphertext"],
        updated_at=row["updated_at"],
        last_test_status=row["last_test_status"],
        last_test_at=row["last_test_at"],
        catalog_updated_at=row["catalog_updated_at"],
    )


def _user_from_row(row: sqlite3.Row) -> User:
    return User(
        telegram_id=row["telegram_id"],
        name=row["name"],
        role=row["role"],
        division=row["division"],
        communication_profile=row["communication_profile"],
        custom_instruction=row["custom_instruction"],
        active=bool(row["active"]),
    )


def _company_from_row(row: sqlite3.Row) -> Company:
    return Company(
        company_id=row["company_id"],
        name=row["name"],
        profile_file=row["profile_file"],
        instruction_file=row["instruction_file"],
        knowledge_dir=row["knowledge_dir"],
        active=bool(row["active"]),
    )


def _safe_relative_path(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Path konfigurasi harus relatif dan tidak boleh memakai '..': {text}")
    return path.as_posix()


def _validate_company_id(value: object) -> str:
    company_id = str(value or "").strip().casefold()
    if not COMPANY_ID_PATTERN.fullmatch(company_id):
        raise ValueError(
            "Company ID harus 1-64 karakter dan hanya memakai huruf kecil, angka, atau tanda minus"
        )
    return company_id


def _identifier_from_label(value: object, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    identifier = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return (identifier or fallback)[:64].rstrip("-")


def _next_unique_identifier(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    base: str,
    *,
    where_column: str = "",
    where_value: object = None,
) -> str:
    candidate = base
    counter = 2
    while True:
        query = f"SELECT 1 FROM {table} WHERE {column} = ?"
        params: tuple[object, ...] = (candidate,)
        if where_column:
            query += f" AND {where_column} = ?"
            params += (where_value,)
        if connection.execute(query, params).fetchone() is None:
            return candidate
        suffix = f"-{counter}"
        candidate = f"{base[: 64 - len(suffix)].rstrip('-')}{suffix}"
        counter += 1


def _validate_company_name(value: object) -> str:
    name = " ".join(str(value or "").split())
    if not 2 <= len(name) <= 100:
        raise ValueError("Nama company harus terdiri dari 2-100 karakter")
    return name


def _validate_module_short_code(value: object) -> str:
    code = str(value or "").strip()
    if not code:
        return ""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,2}", code):
        raise ValueError("Kode singkat harus 2-3 huruf/angka, diawali huruf (contoh TG)")
    code = code.casefold()
    if code in {"start", "help", "whoami", "company", "module", "reset", "general", "none", "off"}:
        raise ValueError("Kode singkat tersebut dipakai oleh perintah sistem")
    return code


def _require_unique_module_command(
    connection: sqlite3.Connection, company_id: str, module_id: str, short_code: str
) -> None:
    if connection.execute(
        "SELECT 1 FROM shared_modules WHERE lower(short_code) IN (?, ?)",
        (module_id, short_code),
    ).fetchone():
        raise ValueError("Kode atau ID berbenturan dengan modul bersama/Learning")
    # Keep aliases and canonical IDs unambiguous, including inactive modules.
    conflict = connection.execute(
        """
        SELECT 1 FROM modules WHERE company_id = ? AND module_id != ? AND (
            (short_code != '' AND lower(short_code) IN (?, ?))
            OR (? != '' AND lower(module_id) = ?)
        ) LIMIT 1
        """,
        (company_id, module_id, module_id, short_code, short_code, short_code),
    ).fetchone()
    if conflict:
        raise ValueError("Kode singkat atau ID sudah digunakan module lain pada company ini")


def _validate_module_id(value: object) -> str:
    module_id = str(value or "").strip().casefold()
    if not COMPANY_ID_PATTERN.fullmatch(module_id):
        raise ValueError(
            "Module ID harus 1-64 karakter dan hanya memakai huruf kecil, "
            "angka, atau tanda minus"
        )
    return module_id


def _validate_runtime_profile_id(value: object) -> str:
    profile_id = str(value or "").strip().casefold()
    if not COMPANY_ID_PATTERN.fullmatch(profile_id):
        raise ValueError(
            "Credential profile wajib dipilih dan harus memakai ID yang valid"
        )
    return profile_id


def _validate_backup_profile_id(
    value: object, primary_profile_id: str, primary_model: object = "", backup_model: object = ""
) -> str:
    if not str(value or "").strip():
        return ""
    backup = _validate_runtime_profile_id(value)
    if backup == primary_profile_id and str(primary_model or "").strip() == str(backup_model or "").strip():
        raise ValueError("AI utama dan AI cadangan harus berbeda")
    return backup


def _validate_runtime_profile_label(value: object) -> str:
    label = " ".join(str(value or "").split())
    if not 2 <= len(label) <= 100:
        raise ValueError("Nama credential profile harus terdiri dari 2-100 karakter")
    return label


def _validate_runtime_profile_fields(
    provider: object, api_key_env: object, model: object, base_url: object, *,
    encrypted: bool = False, allow_empty_model: bool = False,
) -> dict[str, str]:
    normalized_provider = str(provider or "").strip().casefold()
    if normalized_provider not in SUPPORTED_AI_PROVIDERS:
        raise ValueError("Pilih provider OpenAI, Anthropic, DeepSeek, atau Gemini")
    normalized_environment = str(api_key_env or "").strip().upper()
    if encrypted:
        normalized_environment = ""
    elif not ENVIRONMENT_NAME_PATTERN.fullmatch(normalized_environment):
        raise ValueError(
            "Nama environment variable API key hanya boleh memakai A-Z, 0-9, "
            "dan underscore"
        )
    normalized_model = str(model or "").strip()
    if not (allow_empty_model and encrypted and not normalized_model) and not 1 <= len(normalized_model) <= 150:
        raise ValueError("Nama model harus terdiri dari 1-150 karakter")
    normalized_base_url = str(base_url or "").strip()
    if len(normalized_base_url) > 500:
        raise ValueError("AI base URL maksimal 500 karakter")
    if normalized_base_url and not re.fullmatch(
        r"https?://[^\s]+", normalized_base_url
    ):
        raise ValueError("AI base URL harus berupa URL HTTP atau HTTPS yang valid")
    if encrypted:
        validate_encrypted_endpoint(normalized_provider, normalized_base_url)
    return {
        "provider": normalized_provider,
        "api_key_env": normalized_environment,
        "model": normalized_model,
        "base_url": normalized_base_url,
    }


def _validate_module_name(value: object) -> str:
    name = " ".join(str(value or "").split())
    if not 2 <= len(name) <= 100:
        raise ValueError("Nama module harus terdiri dari 2-100 karakter")
    return name


def _validate_module_description(value: object) -> str:
    description = " ".join(str(value or "").split())
    if len(description) > 500:
        raise ValueError("Deskripsi module maksimal 500 karakter")
    return description


def _validate_module_playbook(value: object) -> str:
    playbook = str(value or "").strip()
    if len(playbook) > MAX_MODULE_PLAYBOOK_CHARS:
        raise ValueError("Playbook module maksimal 50.000 karakter")
    return playbook


def _validate_knowledge_document_key(value: object) -> str:
    document_key = str(value or "").strip().casefold()
    if not COMPANY_ID_PATTERN.fullmatch(document_key):
        raise ValueError(
            "Document key harus 1-64 karakter dan hanya memakai huruf kecil, "
            "angka, atau tanda minus"
        )
    return document_key


def _validate_knowledge_title(value: object) -> str:
    title = " ".join(str(value or "").split())
    if not 2 <= len(title) <= 150:
        raise ValueError("Judul knowledge harus terdiri dari 2-150 karakter")
    return title


def _validate_knowledge_content(value: object) -> str:
    content = str(value or "").strip()
    if len(content) > MAX_KNOWLEDGE_DOCUMENT_CHARS:
        raise ValueError(
            "Isi knowledge maksimal "
            f"{MAX_KNOWLEDGE_DOCUMENT_CHARS:,} karakter".replace(",", ".")
        )
    return content


def _validate_knowledge_source(
    filename: object,
    media_type: object,
    size_bytes: object,
    sha256: object,
) -> dict[str, object]:
    normalized_filename = Path(
        str(filename or "").replace("\\", "/")
    ).name.strip()[:255]
    normalized_media_type = str(media_type or "").strip()[:150]
    try:
        normalized_size = int(size_bytes or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Ukuran source knowledge tidak valid") from exc
    if normalized_size < 0:
        raise ValueError("Ukuran source knowledge tidak valid")
    normalized_sha256 = str(sha256 or "").strip().casefold()
    if normalized_sha256 and not re.fullmatch(r"[a-f0-9]{64}", normalized_sha256):
        raise ValueError("Checksum source knowledge tidak valid")
    if normalized_filename and not normalized_sha256:
        raise ValueError("Checksum source knowledge wajib tersedia untuk file upload")
    return {
        "filename": normalized_filename,
        "media_type": normalized_media_type,
        "size_bytes": normalized_size,
        "sha256": normalized_sha256,
    }


def _validate_telegram_id(value: object) -> int:
    try:
        telegram_id = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Telegram ID harus berupa angka") from exc
    if not 1 <= telegram_id <= MAX_TELEGRAM_ID:
        raise ValueError("Telegram ID berada di luar rentang yang valid")
    return telegram_id


def _validate_user_name(value: object) -> str:
    name = " ".join(str(value or "").split())
    if not 2 <= len(name) <= 100:
        raise ValueError("Nama user harus terdiri dari 2-100 karakter")
    return name


def _validate_company_instruction(value: object) -> str:
    content = str(value or "").strip()
    if len(content) > MAX_COMPANY_INSTRUCTION_CHARS:
        raise ValueError(
            f"Company instruction maksimal {MAX_COMPANY_INSTRUCTION_CHARS:,} karakter"
        )
    return content


def _validate_membership_fields(
    company_id: object,
    job_title: object,
    division: object,
    role_level: object,
    communication_profile: object,
    custom_instruction: object,
) -> dict[str, str]:
    normalized_role = str(role_level or "").strip().casefold()
    if normalized_role not in ROLE_LEVELS:
        raise ValueError("Role level harus owner, gm, manager, supervisor, atau staff")
    normalized_profile = str(communication_profile or "").strip().casefold()
    if normalized_profile not in COMMUNICATION_PROFILES:
        raise ValueError("Communication profile tidak valid")
    normalized_job = " ".join(str(job_title or "").split())
    normalized_division = " ".join(str(division or "").split())
    if not 2 <= len(normalized_job) <= 100:
        raise ValueError("Jabatan harus terdiri dari 2-100 karakter")
    if normalized_division and not 2 <= len(normalized_division) <= 100:
        raise ValueError("Divisi lama harus kosong atau terdiri dari 2-100 karakter")
    instruction = str(custom_instruction or "").strip()
    if len(instruction) > 5_000:
        raise ValueError("Custom instruction maksimal 5000 karakter")
    return {
        "company_id": _validate_company_id(company_id),
        "job_title": normalized_job,
        "division": normalized_division,
        "role_level": normalized_role,
        "communication_profile": normalized_profile,
        "custom_instruction": instruction,
    }


def _require_user(connection: sqlite3.Connection, telegram_id: int) -> None:
    if connection.execute(
        "SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone() is None:
        raise ValueError("User tidak ditemukan")


def _require_active_company(
    connection: sqlite3.Connection, company_id: str
) -> None:
    row = connection.execute(
        "SELECT active FROM companies WHERE company_id = ?", (company_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Company tidak ditemukan")
    if not bool(row["active"]):
        raise ValueError("Company sedang nonaktif")


def _require_active_runtime_profile(
    connection: sqlite3.Connection, profile_id: object
) -> sqlite3.Row:
    normalized_profile = _validate_runtime_profile_id(profile_id)
    row = connection.execute(
        """
        SELECT profile_id, active, model FROM ai_runtime_profiles
        WHERE profile_id = ?
        """,
        (normalized_profile,),
    ).fetchone()
    if row is None:
        raise ValueError("Credential profile Module tidak ditemukan")
    if not bool(row["active"]):
        raise ValueError("Credential profile Module sedang nonaktif")
    return row


def _validate_model_selection(connection: sqlite3.Connection, profile_id: str, model: object) -> str:
    profile = _require_active_runtime_profile(connection, profile_id)
    selected = str(model or "").strip()
    if not selected and profile["model"]:
        return ""  # Existing fixed-model profiles are retained, never silently migrated.
    found = connection.execute(
        "SELECT selectable FROM ai_model_catalog WHERE profile_id = ? AND model_id = ?",
        (profile_id, selected),
    ).fetchone()
    if not found or not found["selectable"]:
        raise ValueError("Pilih model chat yang tersedia. Aktifkan provider lalu Tes & ambil model di Settings AI.")
    return selected


def _validate_distinct_model_choices(
    connection: sqlite3.Connection, primary: str, model: str, backup: str, backup_model: str
) -> None:
    if primary == backup:
        legacy_model = _require_active_runtime_profile(connection, primary)["model"]
        if (model or legacy_model) == (backup_model or legacy_model):
            raise ValueError("AI utama dan AI cadangan harus berbeda")


def _membership_audit_details(
    values: dict[str, str], *, is_default: bool
) -> dict[str, object]:
    return {
        "company_id": values["company_id"],
        "job_title": values["job_title"],
        "division": values["division"],
        "role_level": values["role_level"],
        "communication_profile": values["communication_profile"],
        "is_default": is_default,
        "has_custom_instruction": bool(values["custom_instruction"]),
    }


def _write_audit(
    connection: sqlite3.Connection,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, object],
) -> None:
    connection.execute(
        """
        INSERT INTO admin_audit_events (
            actor, action, entity_type, entity_id, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            actor,
            action,
            entity_type,
            entity_id,
            json.dumps(details, ensure_ascii=False, sort_keys=True),
            _now(),
        ),
    )


def _validate_admin_username(value: object) -> str:
    username = str(value).strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,49}", username):
        raise ValueError(
            "Username admin harus 3–50 karakter berupa huruf, angka, titik, garis bawah, atau tanda hubung"
        )
    return username


def _hash_admin_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    rounds = 310_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return f"pbkdf2_sha256${rounds}${salt.hex()}${digest.hex()}"


def _verify_admin_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds_text, salt_hex, expected_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds_text)
        )
        return secrets.compare_digest(actual.hex(), expected_hex)
    except (ValueError, TypeError):
        return False


def _admin_user_from_row(row: sqlite3.Row) -> AdminUser:
    return AdminUser(
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
        active=bool(row["active"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deactivate_missing(
    connection: sqlite3.Connection,
    table: str,
    key_column: str,
    active_ids: list[str] | list[int],
) -> None:
    if active_ids:
        placeholders = ",".join("?" for _ in active_ids)
        connection.execute(
            f"UPDATE {table} SET active = 0 WHERE {key_column} NOT IN ({placeholders})",
            active_ids,
        )
    else:
        connection.execute(f"UPDATE {table} SET active = 0")


def _assign_legacy_messages(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE messages
        SET company_id = (
            SELECT m.company_id
            FROM user_company_memberships m
            WHERE m.telegram_id = messages.telegram_id
              AND m.active = 1
              AND m.is_default = 1
            LIMIT 1
        )
        WHERE company_id = ''
          AND EXISTS (
              SELECT 1
              FROM user_company_memberships m
              WHERE m.telegram_id = messages.telegram_id
                AND m.active = 1
                AND m.is_default = 1
          )
        """
    )
    connection.execute(
        """
        UPDATE messages
        SET company_id = (
            SELECT m.company_id
            FROM user_company_memberships m
            WHERE m.telegram_id = messages.telegram_id AND m.active = 1
            LIMIT 1
        )
        WHERE company_id = ''
          AND 1 = (
              SELECT COUNT(*)
              FROM user_company_memberships m
              WHERE m.telegram_id = messages.telegram_id AND m.active = 1
          )
        """
    )
