from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


COMPANY_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
ROLE_LEVELS = {"gm", "manager", "staff"}
COMMUNICATION_PROFILES = {"executive", "manager", "staff", "default"}
MAX_TELEGRAM_ID = 9_007_199_254_740_991


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
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id),
                    FOREIGN KEY (active_company_id) REFERENCES companies(company_id)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    company_id TEXT NOT NULL DEFAULT '',
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
                """
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

            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_user_company_id
                ON messages(telegram_id, company_id, id DESC);

                CREATE INDEX IF NOT EXISTS idx_memberships_user
                ON user_company_memberships(telegram_id, active, company_id);

                CREATE INDEX IF NOT EXISTS idx_admin_audit_created
                ON admin_audit_events(created_at DESC, id DESC);
                """
            )

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
        normalized_id = _validate_company_id(company_id)
        normalized_name = _validate_company_name(name)
        base = f"companies/{normalized_id}"
        now = _now()
        try:
            with self._connect() as connection:
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
            current = connection.execute(
                """
                SELECT * FROM user_company_memberships
                WHERE telegram_id = ? AND company_id = ?
                """,
                (normalized_user, values["company_id"]),
            ).fetchone()
            if current is None:
                raise ValueError("Membership tidak ditemukan")
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
        self, telegram_id: int, role: str, content: str, company_id: str = ""
    ) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("Role pesan tidak valid")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO messages (telegram_id, company_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (telegram_id, company_id, role, content, _now()),
            )

    def get_history(
        self, telegram_id: int, limit: int, company_id: str = ""
    ) -> list[dict[str, str]]:
        if limit <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content FROM messages
                WHERE telegram_id = ? AND company_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (telegram_id, company_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def clear_history(self, telegram_id: int, company_id: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM messages WHERE telegram_id = ? AND company_id = ?",
                (telegram_id, company_id),
            )

    def prune_history(
        self, telegram_id: int, company_id: str = "", keep: int = 100
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM messages
                WHERE telegram_id = ? AND company_id = ? AND id NOT IN (
                    SELECT id FROM messages
                    WHERE telegram_id = ? AND company_id = ?
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (telegram_id, company_id, telegram_id, company_id, keep),
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


def _validate_company_name(value: object) -> str:
    name = " ".join(str(value or "").split())
    if not 2 <= len(name) <= 100:
        raise ValueError("Nama company harus terdiri dari 2-100 karakter")
    return name


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
        raise ValueError("Role level harus gm, manager, atau staff")
    normalized_profile = str(communication_profile or "").strip().casefold()
    if normalized_profile not in COMMUNICATION_PROFILES:
        raise ValueError("Communication profile tidak valid")
    normalized_job = " ".join(str(job_title or "").split())
    normalized_division = " ".join(str(division or "").split())
    if not 2 <= len(normalized_job) <= 100:
        raise ValueError("Jabatan harus terdiri dari 2-100 karakter")
    if not 2 <= len(normalized_division) <= 100:
        raise ValueError("Divisi harus terdiri dari 2-100 karakter")
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
