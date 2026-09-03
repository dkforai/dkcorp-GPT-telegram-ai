"""Global module catalog and Learning. No synthetic company or inherited ACLs."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.database import AIModule, _validate_model_selection, _validate_distinct_model_choices, _write_audit

WIB = ZoneInfo("Asia/Jakarta")
RESERVED = {"start", "help", "whoami", "company", "module", "reset", "general", "none", "off", "learning", "shared", "ulang"}


def initialize_schema(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS shared_modules (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('shared','learning')),
        short_code TEXT NOT NULL UNIQUE COLLATE NOCASE,
        draft_json TEXT NOT NULL, live_json TEXT, revision INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS shared_versions (
        module_id TEXT NOT NULL REFERENCES shared_modules(id), version INTEGER NOT NULL,
        content_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(module_id,version)
    );
    CREATE TABLE IF NOT EXISTS learning_sources (
        id TEXT PRIMARY KEY, filename TEXT NOT NULL, pdf BLOB NOT NULL,
        pages_json TEXT NOT NULL, report_json TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS learning_chunks (
        id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES learning_sources(id),
        ordinal INTEGER NOT NULL, page INTEGER NOT NULL, text TEXT NOT NULL,
        UNIQUE(source_id,ordinal)
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS learning_search USING fts5(
        text, source_id UNINDEXED, ordinal UNINDEXED, tokenize='unicode61'
    );
    CREATE TABLE IF NOT EXISTS learning_books (
        id TEXT PRIMARY KEY, draft_json TEXT NOT NULL, live_json TEXT,
        revision INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS learning_versions (
        book_id TEXT NOT NULL REFERENCES learning_books(id), version INTEGER NOT NULL,
        content_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(book_id,version)
    );
    CREATE TABLE IF NOT EXISTS shared_sessions (
        telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id),
        module_id TEXT NOT NULL REFERENCES shared_modules(id)
    );
    CREATE TABLE IF NOT EXISTS learning_session_books (
        telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id), book_scope TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS shared_messages (
        id INTEGER PRIMARY KEY, telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
        module_id TEXT NOT NULL REFERENCES shared_modules(id), company_id TEXT NOT NULL DEFAULT '',
        source_scope TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant')),
        content TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS shared_history_scope ON shared_messages(
        telegram_id,module_id,company_id,source_scope,id DESC
    );
    """)
    c.execute("INSERT OR IGNORE INTO shared_modules(id,kind,short_code,draft_json,updated_at) VALUES('learning','learning','learning',?,?)",
              (json.dumps({"name": "Learning", "description": "", "context_mode": "independent", "instruction": "", "ai_selection": "", "backup_ai_selection": ""}), now()))


def now():
    return datetime.now(timezone.utc).isoformat()


def text_field(values, key, limit, required=False):
    value = str(values.get(key, "")).strip()
    if len(value) > limit or (required and not value):
        raise ValueError(f"{key} wajib diisi dan maksimal {limit:,} karakter" if required else f"{key} maksimal {limit:,} karakter")
    return value


def local_time(value):
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is not None:
            raise ValueError
        return parsed.replace(tzinfo=WIB).astimezone(timezone.utc).isoformat()
    except ValueError:
        raise ValueError("Tanggal/jam tidak valid. Gunakan waktu WIB.") from None


def display_time(value):
    return datetime.fromisoformat(value).astimezone(WIB).strftime("%d-%m-%Y %H:%M WIB")


class SharedStore:
    def __init__(self, database):
        self.db = database

    @contextmanager
    def connect(self, write=False):
        c = self.db._connect()
        try:
            if write:
                c.execute("BEGIN IMMEDIATE")
            with c:
                yield c
        finally:
            c.close()

    def modules(self, kind="shared"):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM shared_modules WHERE kind=? ORDER BY updated_at DESC", (kind,))]

    def module(self, module_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM shared_modules WHERE id=?", (module_id,)).fetchone()
            return dict(row) if row else None

    def _ai(self, c, data):
        primary, _, model = data["ai_selection"].partition("|")
        backup, _, backup_model = data["backup_ai_selection"].partition("|")
        if not primary:
            raise ValueError("Pilih AI utama terlebih dahulu")
        selected = _validate_model_selection(c, primary, model)
        selected_backup = _validate_model_selection(c, backup, backup_model) if backup else ""
        _validate_distinct_model_choices(c, primary, selected, backup, selected_backup)

    def save_module(self, module_id, values, actor, publish=False, expected_revision=None):
        data = {k: text_field(values, k, limit, required) for k, limit, required in (
            ("name", 100, True), ("description", 2000, False), ("instruction", 50000, False),
            ("ai_selection", 300, False), ("backup_ai_selection", 300, False), ("context_mode", 20, True),
        )}
        if data["context_mode"] not in {"company", "independent"}:
            raise ValueError("Mode konteks tidak valid")
        code = text_field(values, "short_code", 32, True).casefold()
        learning = module_id == "learning"
        if learning:
            code = "learning"
            data["context_mode"] = "independent"
        elif not re.fullmatch(r"[a-z][a-z0-9]{1,2}", code) or code in RESERVED:
            raise ValueError("Kode modul bersama harus 2–3 huruf/angka dan bukan perintah sistem")
        with self.connect(True) as c:
            old = c.execute("SELECT * FROM shared_modules WHERE id=?", (module_id,)).fetchone() if module_id else None
            if module_id and not old:
                raise ValueError("Modul tidak ditemukan")
            if old and expected_revision != old["updated_at"]:
                raise ValueError("Form sudah berubah. Muat ulang sebelum menyimpan.")
            module_id = module_id or uuid.uuid4().hex
            if c.execute("SELECT 1 FROM shared_modules WHERE short_code=? COLLATE NOCASE AND id!=?", (code, module_id)).fetchone():
                raise ValueError("Kode sudah digunakan modul bersama lain")
            if c.execute("SELECT 1 FROM modules WHERE lower(short_code)=? OR lower(module_id)=?", (code, code)).fetchone():
                raise ValueError("Kode berbenturan dengan modul perusahaan")
            if publish:
                if not learning and not data["instruction"]:
                    raise ValueError("Isi custom instruction sebelum publish")
                self._ai(c, data)
            timestamp = now()
            encoded = json.dumps(data, ensure_ascii=False)
            revision = (old["revision"] if old else 0) + int(publish)
            live = encoded if publish else (old["live_json"] if old else None)
            c.execute("""INSERT INTO shared_modules(id,kind,short_code,draft_json,live_json,revision,active,updated_at)
                         VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET short_code=excluded.short_code,
                         draft_json=excluded.draft_json,live_json=excluded.live_json,revision=excluded.revision,
                         active=excluded.active,updated_at=excluded.updated_at""",
                      (module_id, "learning" if learning else "shared", code, encoded, live, revision, int(values.get("active") == "1"), timestamp))
            if publish:
                c.execute("INSERT INTO shared_versions VALUES(?,?,?,?)", (module_id, revision, encoded, timestamp))
            _write_audit(c, actor, "shared_module.published" if publish else "shared_module.saved", "shared_module", module_id, {"revision": revision})
        return module_id

    def runtime_module(self, module_id, user_id):
        if not self.db.get_user(user_id):
            return None
        row = self.module(module_id)
        if not row or not row["active"] or not row["live_json"]:
            return None
        data = json.loads(row["live_json"])
        if data["context_mode"] == "company" and not self.db.get_active_membership(user_id):
            return None
        module = self.as_ai_module(row, data)
        if self.db.get_module_ai_profile(module) is None:
            return None
        return {**row, **data}

    @staticmethod
    def as_ai_module(row, data=None):
        data = data or row
        primary, _, model = data["ai_selection"].partition("|")
        backup, _, backup_model = data["backup_ai_selection"].partition("|")
        return AIModule("", "", row["id"], data["name"], data["description"], primary, True,
                        backup, model, backup_model, row["short_code"])

    def available(self, user_id):
        return [data for row in self.modules() if (data := self.runtime_module(row["id"], user_id))]

    def select(self, code, user_id):
        with self.connect() as c:
            row = c.execute("SELECT id FROM shared_modules WHERE short_code=? COLLATE NOCASE", (code,)).fetchone()
        module = self.runtime_module(row["id"], user_id) if row else None
        if module:
            with self.connect(True) as c:
                c.execute("INSERT INTO shared_sessions VALUES(?,?) ON CONFLICT(telegram_id) DO UPDATE SET module_id=excluded.module_id", (user_id, module["id"]))
        return module

    def selected(self, user_id):
        with self.connect() as c:
            row = c.execute("SELECT module_id FROM shared_sessions WHERE telegram_id=?", (user_id,)).fetchone()
            return row[0] if row else None

    def clear_selection(self, user_id):
        with self.connect(True) as c:
            c.execute("DELETE FROM shared_sessions WHERE telegram_id=?", (user_id,))

    def book_session(self, user_id, scope=None):
        with self.connect(write=scope is not None) as c:
            if scope is not None:
                c.execute("INSERT INTO learning_session_books VALUES(?,?) ON CONFLICT(telegram_id) DO UPDATE SET book_scope=excluded.book_scope", (user_id, scope))
            row = c.execute("SELECT book_scope FROM learning_session_books WHERE telegram_id=?", (user_id,)).fetchone()
            return row[0] if row else None

    def books(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM learning_books ORDER BY updated_at DESC")]

    def book(self, book_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM learning_books WHERE id=?", (book_id,)).fetchone()
            return dict(row) if row else None

    def source(self, source_id):
        with self.connect() as c:
            row = c.execute("SELECT id,filename,pages_json,report_json FROM learning_sources WHERE id=?", (source_id,)).fetchone()
            return dict(row) if row else None

    def save_source(self, filename, pdf, extracted, actor):
        source_id = hashlib.sha256(pdf).hexdigest()
        with self.connect(True) as c:
            if not c.execute("SELECT 1 FROM learning_sources WHERE id=?", (source_id,)).fetchone():
                c.execute("INSERT INTO learning_sources VALUES(?,?,?,?,?,?)", (source_id, filename[:200], pdf, json.dumps(extracted["pages"]), json.dumps(extracted["report"]), now()))
                ordinal = 0
                for page, content in enumerate(extracted["pages"], 1):
                    # Non-overlapping chunks plus neighbour retrieval avoid duplicating stored text.
                    for start in range(0, len(content), 2000):
                        text = content[start:start + 2000]
                        c.execute("INSERT INTO learning_chunks(source_id,ordinal,page,text) VALUES(?,?,?,?)", (source_id, ordinal, page, text))
                        c.execute("INSERT INTO learning_search(text,source_id,ordinal) VALUES(?,?,?)", (text, source_id, ordinal))
                        ordinal += 1
                _write_audit(c, actor, "learning.source_processed", "learning_source", source_id, extracted["report"])
        return source_id

    def save_book(self, book_id, values, actor, publish=False, expected_revision=None):
        data = {k: text_field(values, k, limit, required) for k, limit, required in (
            ("title", 150, True), ("description", 8000, True), ("instruction", 50000, True),
            ("starts_at", 30, True), ("ends_at", 30, True), ("source_id", 64, True),
        )}
        data["start_utc"], data["end_utc"] = local_time(data["starts_at"]), local_time(data["ends_at"])
        if data["end_utc"] <= data["start_utc"]:
            raise ValueError("Waktu berakhir harus setelah waktu mulai")
        data["reviewed"] = values.get("reviewed") == "1"
        with self.connect(True) as c:
            old = c.execute("SELECT * FROM learning_books WHERE id=?", (book_id,)).fetchone() if book_id else None
            if book_id and not old:
                raise ValueError("Buku tidak ditemukan")
            if old and expected_revision != old["updated_at"]:
                raise ValueError("Form sudah berubah. Muat ulang sebelum menyimpan.")
            if not c.execute("SELECT 1 FROM learning_sources WHERE id=?", (data["source_id"],)).fetchone():
                raise ValueError("Unggah dan proses PDF terlebih dahulu")
            book_id = book_id or uuid.uuid4().hex
            active = values.get("active") == "1"
            if publish:
                if not data["reviewed"]:
                    raise ValueError("Tinjau hasil ekstraksi dan centang konfirmasi sebelum publish")
                if active:
                    self._check_overlap(c, book_id, data)
            elif active and old and old["live_json"]:
                self._check_overlap(c, book_id, json.loads(old["live_json"]))
            timestamp = now()
            encoded = json.dumps(data, ensure_ascii=False)
            revision = (old["revision"] if old else 0) + int(publish)
            live = encoded if publish else (old["live_json"] if old else None)
            c.execute("""INSERT INTO learning_books VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                         draft_json=excluded.draft_json,live_json=excluded.live_json,revision=excluded.revision,
                         active=excluded.active,updated_at=excluded.updated_at""",
                      (book_id, encoded, live, revision, int(active), timestamp))
            if publish:
                c.execute("INSERT INTO learning_versions VALUES(?,?,?,?)", (book_id, revision, encoded, timestamp))
            _write_audit(c, actor, "learning.published" if publish else "learning.saved", "learning_book", book_id, {"revision": revision, "source_id": data["source_id"]})
        return book_id

    @staticmethod
    def _check_overlap(c, book_id, data):
        for row in c.execute("SELECT live_json FROM learning_books WHERE id!=? AND active=1 AND live_json IS NOT NULL", (book_id,)):
            other = json.loads(row[0])
            if data["start_utc"] < other["end_utc"] and other["start_utc"] < data["end_utc"]:
                raise ValueError("Jadwal bertumpuk dengan materi Learning published lainnya")

    def active_book(self, at=None):
        at = at or now()
        matches = []
        for row in self.books():
            if row["active"] and row["live_json"]:
                data = json.loads(row["live_json"])
                if data["start_utc"] <= at < data["end_utc"]:
                    matches.append({**row, **data})
        return matches[0] if len(matches) == 1 else None

    def history(self, user_id, module_id, company_id, scope, limit=12):
        with self.connect() as c:
            rows = c.execute("""SELECT role,content FROM shared_messages WHERE telegram_id=? AND module_id=?
                AND company_id=? AND source_scope=? ORDER BY id DESC LIMIT ?""", (user_id, module_id, company_id, scope, limit)).fetchall()
        # Whole recent messages only, with a predictable character budget.
        selected, chars = [], 0
        for row in rows:
            if chars + len(row["content"]) > 24000:
                break
            selected.append(dict(row))
            chars += len(row["content"])
        return list(reversed(selected))

    def add_turn(self, user_id, module_id, company_id, scope, prompt, answer):
        with self.connect(True) as c:
            c.executemany("INSERT INTO shared_messages(telegram_id,module_id,company_id,source_scope,role,content,created_at) VALUES(?,?,?,?,?,?,?)",
                          [(user_id, module_id, company_id, scope, role, content, now()) for role, content in (("user", prompt), ("assistant", answer))])

    def clear_history(self, user_id, module_id, company_id, scope):
        with self.connect(True) as c:
            c.execute("DELETE FROM shared_messages WHERE telegram_id=? AND module_id=? AND company_id=? AND source_scope=?", (user_id, module_id, company_id, scope))

    def retrieve(self, source_id, question, history, budget=16000):
        """Local lexical retrieval. Never pretend excerpts represent the entire book."""
        stop = {"yang", "dan", "atau", "untuk", "dari", "dengan", "saya", "apa", "ini", "itu", "the", "and", "of", "to", "lanjut", "lanjutkan", "belum", "jelaskan", "bagaimana", "buku", "mulai", "halo", "hai", "siap", "belajar", "yuk", "oke", "baik", "mau", "ingin"}
        tokens = [w for w in re.findall(r"[^\W_]+", question.casefold()) if len(w) > 2 and w not in stop][:24]
        if len(tokens) < 2:
            prior = " ".join(m["content"][-2500:] for m in history[-4:])
            tokens += [w for w in re.findall(r"[^\W_]+", prior.casefold()) if len(w) > 3 and w not in stop][:40]
        query = " OR ".join('"' + t + '"' for t in dict.fromkeys(tokens))
        with self.connect() as c:
            hits = c.execute("SELECT ordinal FROM learning_search WHERE learning_search MATCH ? AND source_id=? ORDER BY rank LIMIT 5", (query, source_id)).fetchall() if query else []
            # Budget the best matches first, not whichever page comes first.
            ordinals = [int(hit[0]) for hit in hits]
            for hit in hits:
                ordinals.extend((int(hit[0]) - 1, int(hit[0]) + 1))
            overview = any(w in question.casefold() for w in ("ringkas", "seluruh", "keseluruhan", "summary", "overview", "isi buku", "inti buku"))
            if overview:
                total = c.execute("SELECT count(*) FROM learning_chunks WHERE source_id=?", (source_id,)).fetchone()[0]
                ordinals.extend(range(0, total, max(1, total // 6)))
            elif not query:
                ordinals.extend(range(3))
            ordinals = list(dict.fromkeys(n for n in ordinals if n >= 0))
            if not ordinals:
                return "", False
            placeholders = ",".join("?" for _ in ordinals)
            rows = c.execute(f"SELECT ordinal,text FROM learning_chunks WHERE source_id=? AND ordinal IN ({placeholders}) ORDER BY ordinal", (source_id, *sorted(ordinals))).fetchall()
        available = {row["ordinal"]: row["text"] for row in rows}
        parts, count = {}, 0
        for ordinal in ordinals:
            text = available.get(ordinal)
            if text is None or count + len(text) + (2 if parts else 0) > budget:
                continue
            parts[ordinal] = text
            count += len(text) + (2 if len(parts) > 1 else 0)
        return "\n\n".join(parts[n] for n in sorted(parts)), bool(hits)
