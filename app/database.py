from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


COMPANY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


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
                """
            )

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
                    f"Company ID '{company_id}' tidak valid. Gunakan huruf kecil, angka, dan tanda minus."
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

    def get_company(self, company_id: str) -> Company | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM companies WHERE company_id = ? AND active = 1",
                (company_id,),
            ).fetchone()
        return _company_from_row(row) if row else None

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
