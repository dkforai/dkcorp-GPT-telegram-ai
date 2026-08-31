from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class User:
    telegram_id: int
    name: str
    role: str
    division: str
    communication_profile: str
    custom_instruction: str
    active: bool


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
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

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_user_id
                ON messages(telegram_id, id DESC);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "communication_profile" not in columns:
                connection.execute(
                    """
                    ALTER TABLE users
                    ADD COLUMN communication_profile TEXT NOT NULL DEFAULT ''
                    """
                )

    def sync_users(self, users_file: Path) -> int:
        if not users_file.exists():
            return 0
        raw_users = json.loads(users_file.read_text(encoding="utf-8"))
        if not isinstance(raw_users, list):
            raise ValueError(f"{users_file} harus berisi JSON array")

        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for item in raw_users:
            rows.append(
                (
                    int(item["telegram_id"]),
                    str(item["name"]).strip(),
                    str(item.get("role", "")).strip(),
                    str(item.get("division", "")).strip(),
                    str(item.get("communication_profile", "")).strip(),
                    str(item.get("custom_instruction", "")).strip(),
                    int(bool(item.get("active", True))),
                    now,
                )
            )

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
                rows,
            )
        return len(rows)

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

    def add_message(self, telegram_id: int, role: str, content: str) -> None:
        if role not in {"user", "assistant"}:
            raise ValueError("Role pesan tidak valid")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO messages (telegram_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (telegram_id, role, content, datetime.now(timezone.utc).isoformat()),
            )

    def get_history(self, telegram_id: int, limit: int) -> list[dict[str, str]]:
        if limit <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT role, content FROM messages
                WHERE telegram_id = ? ORDER BY id DESC LIMIT ?
                """,
                (telegram_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def clear_history(self, telegram_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM messages WHERE telegram_id = ?", (telegram_id,)
            )

    def prune_history(self, telegram_id: int, keep: int = 100) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM messages WHERE telegram_id = ? AND id NOT IN (
                    SELECT id FROM messages WHERE telegram_id = ?
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (telegram_id, telegram_id, keep),
            )
