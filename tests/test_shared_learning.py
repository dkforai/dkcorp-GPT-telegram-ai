import asyncio
import json
import re
import sqlite3
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.admin import create_admin_app
from app.learning_ingestion import extract_book, _extract
from app.shared_modules import SharedStore, local_time
from test_core import _help_bot, _database_rows


@pytest.fixture
def env(tmp_path):
    bot = _help_bot(tmp_path)
    store = bot.shared_store
    profile = bot.database.list_ai_runtime_profiles()[0].profile_id
    values = dict(name="Modul Global", short_code="GL", context_mode="independent", description="Deskripsi modul\nBaris kedua",
                  instruction="INSTRUKSI GLOBAL", ai_selection=profile, backup_ai_selection="", active="1")
    return bot, store, values


def save(store, values, module_id=None, publish=True):
    row = store.module(module_id) if module_id else None
    return store.save_module(module_id, values, "admin", publish=publish, expected_revision=row["updated_at"] if row else None)


def source(store, text="Kebiasaan baik dimulai dengan langkah kecil. Ulangi kebiasaan setiap hari."):
    return store.save_source("buku.pdf", b"%PDF-test" + text.encode(),
        {"pages": [text, ""], "report": {"pages": 2, "readable_pages": 1, "characters": len(text), "empty_pages": [2]}}, "admin")


def book_values(store, **overrides):
    profile = store.db.list_ai_runtime_profiles()[0].profile_id
    values = dict(title="Buku Kebiasaan", description="Selamat belajar!\n\nPilih tujuanmu.", instruction="Ajukan pertanyaan bertahap sesuai buku.",
                  starts_at="2026-09-01T00:00", ends_at="2026-10-01T00:00", source_id=source(store), reviewed="1", active="1",
                  ai_selection=profile, backup_ai_selection="")
    return {**values, **overrides}


def publish_book(store, values, book_id=None):
    row = store.book(book_id) if book_id else None
    return store.save_book(book_id, values, "admin", publish=True, expected_revision=row["updated_at"] if row else None)


def call(bot, method, text="", args=None, user_id=42):
    replies = []
    async def reply(text, **kwargs):
        replies.append((text, kwargs))
    async def action(*args, **kwargs):
        pass
    update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(id=user_id),
                             effective_message=SimpleNamespace(text=text, reply_text=reply))
    context = SimpleNamespace(args=args or [], bot=SimpleNamespace(username="test_bot", send_chat_action=action))
    asyncio.run(getattr(bot, method)(update, context))
    return replies


def test_retrieval_budget_prioritizes_match_over_earlier_neighbor(env):
    _, store, _ = env
    source_id = store.save_source("rank.pdf", b"%PDF-ranking", {
        "pages": ["Pembukaan umum " * 50, "Topik zebraterang dijelaskan di sini.", "Penutup buku."],
        "report": {"pages": 3, "readable_pages": 3, "characters": 760, "empty_pages": []},
    }, "admin")
    text, matched = store.retrieve(source_id, "zebraterang", [], budget=100)
    assert matched and "zebraterang" in text
    assert len(text) <= 100


@pytest.mark.parametrize("question", [
    "Apa fungsi buku ini untuk saya?",
    "Kita bisa belajar apa dari buku ini?",
    "Jelaskan buku ini",
])
def test_general_book_questions_use_overview_without_ai_router(env, question):
    _, store, _ = env
    source_id = source(store, "Isi utama buku untuk pengambilan keputusan.")
    excerpts, matched = store.retrieve(source_id, question, [])
    assert matched and "pengambilan keputusan" in excerpts


def test_learning_book_owns_published_ai_selection(env, monkeypatch):
    bot, store, values = env
    save(store, {**values, "short_code": "learning", "name": "Learning"}, "learning")
    profile = bot.database.create_ai_runtime_profile(
        "book-ai", "Book AI", "openai", "BOOK_AI_KEY", "book-model", "",
        "admin",
    )
    monkeypatch.setenv("BOOK_AI_KEY", "secret")
    publish_book(store, book_values(store, ai_selection=profile.profile_id))
    monkeypatch.setattr("app.shared_modules.now", lambda: "2026-09-03T00:00:00+00:00")
    runtime = store.runtime_module("learning", 42)
    assert runtime["ai_selection"] == profile.profile_id


def test_shared_draft_publish_snapshot_and_stale_edit(env):
    bot, store, values = env
    key = save(store, values, publish=False)
    assert not store.available(42)
    save(store, values, key)
    assert store.available(42)[0]["instruction"] == "INSTRUKSI GLOBAL"
    stale = store.module(key)["updated_at"]
    save(store, {**values, "instruction": "DRAFT ONLY"}, key, publish=False)
    assert store.runtime_module(key, 42)["instruction"] == "INSTRUKSI GLOBAL"
    with pytest.raises(ValueError, match="Form sudah berubah"):
        store.save_module(key, values, "admin", expected_revision=stale)
    save(store, {**values, "instruction": "NEW LIVE"}, key)
    assert store.runtime_module(key, 42)["revision"] == 2


@pytest.mark.parametrize("code", ["G", "long", "off", "tg!", "12", "learning", "shared"])
def test_global_command_validation(env, code):
    _, store, values = env
    with pytest.raises(ValueError):
        save(store, {**values, "short_code": code})


def test_alias_collision_both_directions_and_inactive(env):
    bot, store, values = env
    module = bot.database.get_module_admin("malang-strudel", "threads-generator")
    bot.database.update_module("malang-strudel", module.module_id, module.name, module.description, actor="admin", ai_runtime_profile_id=module.ai_runtime_profile_id, short_code="TG")
    with pytest.raises(ValueError, match="berbenturan"):
        save(store, {**values, "short_code": "tg"})
    key = save(store, values)
    save(store, {**values, "active": "0"}, key, publish=False)
    with pytest.raises(ValueError, match="berbenturan"):
        bot.database.create_module("other", "global", "Global", "", "admin", ai_runtime_profile_id=values["ai_selection"], short_code="GL")


def test_all_whitelisted_users_no_membership_and_revocation(env):
    bot, store, values = env
    key = save(store, values)
    with sqlite3.connect(bot.database.path) as c:
        c.execute("UPDATE user_company_memberships SET active=0")
    assert store.runtime_module(key, 42)
    assert not store.runtime_module(key, 999)
    assert store.select("gL", 42)["id"] == key
    save(store, {**values, "context_mode": "company"}, key)
    assert not store.runtime_module(key, 42)
    save(store, values, key)
    with sqlite3.connect(bot.database.path) as c:
        c.execute("UPDATE users SET active=0")
    assert not store.runtime_module(key, 42)


@pytest.mark.parametrize("route,args,text", [("module", ["GL"], ""), ("module_shortcut", [], "/gl"), ("module_shortcut", [], "/GL@test_bot")])
def test_shared_selection_description_and_no_ai(env, route, args, text):
    bot, store, values = env
    key = save(store, values)
    before = _database_rows(bot.database.path)
    replies = call(bot, route, text, args)
    assert replies[0][0].endswith("\n\n" + values["description"])
    assert replies[0][1]["parse_mode"] is None
    assert store.selected(42) == key
    after = _database_rows(bot.database.path)
    for table in before:
        if table != "shared_sessions":
            assert after[table] == before[table]


def test_shared_help_without_company_and_company_switch_leaves_global(env):
    bot, store, values = env
    save(store, values)
    call(bot, "module_shortcut", "/GL")
    assert store.selected(42)
    call(bot, "company", args=["amazing-malang"])
    assert store.selected(42) is None
    with sqlite3.connect(bot.database.path) as c:
        c.execute("UPDATE user_company_memberships SET active=0")
    text = "".join(t for t, _ in call(bot, "help"))
    assert "/GL" in text and "/company" not in text


def test_independent_prompt_and_history_never_include_company(env):
    bot, store, values = env
    key = save(store, values)
    store.select("GL", 42)
    captured = []
    async def generate(primary, backup, system, history, text):
        captured.append((system, history))
        return "Jawaban global"
    bot.module_provider_resolver = SimpleNamespace(generate=generate)
    call(bot, "chat", "Pertanyaan pribadi")
    system, history = captured[0]
    for secret in ("Malang Strudel", "Keep custom instruction", "Knowledge content", "Threads history", "General history"):
        assert secret not in system and secret not in str(history)
    assert "INSTRUKSI GLOBAL" in system
    assert len(store.history(42, key, "", "module:1:independent")) == 2
    assert not store.history(999, key, "", "module:1:independent")
    save(store, {**values, "context_mode": "company"}, key)
    call(bot, "chat", "Pertanyaan perusahaan")
    assert "Malang Strudel" in captured[-1][0] and "Knowledge content" in captured[-1][0]
    assert not captured[-1][1]
    save(store, values, key)
    call(bot, "chat", "Kembali independen")
    assert not captured[-1][1] and "Knowledge content" not in captured[-1][0]


def test_unavailable_selected_does_not_fall_back_to_company(env):
    bot, store, values = env
    key = save(store, values)
    store.select("GL", 42)
    save(store, {**values, "active": "0"}, key, publish=False)
    assert "tidak tersedia" in call(bot, "chat", "halo")[0][0]


def test_schedule_boundaries_overlap_and_reactivation(env):
    _, store, _ = env
    values = book_values(store, starts_at="2026-09-07T00:00", ends_at="2026-09-14T00:00")
    first = publish_book(store, values)
    assert not store.active_book("2026-09-06T16:59:59+00:00")
    assert store.active_book("2026-09-06T17:00:00+00:00")["id"] == first
    assert store.active_book("2026-09-13T16:59:59+00:00")["id"] == first
    assert not store.active_book("2026-09-13T17:00:00+00:00")
    with pytest.raises(ValueError, match="bertumpuk"):
        publish_book(store, {**values, "title": "Overlap"})
    second = publish_book(store, {**values, "starts_at": "2026-09-14T00:00", "ends_at": "2026-09-21T00:00"})
    assert store.active_book("2026-09-13T17:00:00+00:00")["id"] == second
    publish_book(store, {**values, "active": "0"}, first)
    third = publish_book(store, values)
    with pytest.raises(ValueError, match="bertumpuk"):
        store.save_book(first, values, "admin", expected_revision=store.book(first)["updated_at"])
    assert third != first


@pytest.mark.parametrize("changes", [{"ends_at": "2026-08-01T00:00"}, {"starts_at": "bad"}, {"reviewed": "0"}, {"source_id": "missing"}])
def test_invalid_book_never_publishes(env, changes):
    _, store, _ = env
    with pytest.raises(ValueError):
        publish_book(store, book_values(store, **changes))
    assert not store.books()


def test_book_draft_does_not_change_live_content(env):
    _, store, _ = env
    values = book_values(store)
    key = publish_book(store, values)
    store.save_book(key, {**values, "description": "Draft text"}, "admin", expected_revision=store.book(key)["updated_at"])
    assert store.active_book("2026-09-03T00:00:00+00:00")["description"] == values["description"]


def test_source_index_shared_deduplicated_and_scoped(env):
    _, store, _ = env
    key = source(store)
    assert source(store) == key
    other = source(store, "Informasi rahasia tentang perusahaan lain")
    excerpt, found = store.retrieve(key, "kebiasaan", [])
    assert found and "kebiasaan" in excerpt.lower()
    assert "rahasia" not in excerpt
    assert store.retrieve(key, "zzzzxxxxnonexistent", []) == ("", False)
    assert store.retrieve(other, "rahasia", [])[1]
    with store.connect() as c:
        assert c.execute("SELECT count(*) FROM learning_sources").fetchone()[0] == 2


def test_learning_opening_static_and_book_version_history_isolation(env, monkeypatch):
    bot, store, values = env
    save(store, {**values, "short_code": "learning", "name": "Learning"}, "learning")
    book = publish_book(store, book_values(store))
    monkeypatch.setattr("app.shared_modules.now", lambda: "2026-09-03T00:00:00+00:00")
    before = _database_rows(bot.database.path)
    assert call(bot, "learning")[0][0] == "Selamat belajar!\n\nPilih tujuanmu."
    assert call(bot, "learning")[0][0] == "Selamat belajar!\n\nPilih tujuanmu."
    assert _database_rows(bot.database.path)["shared_messages"] == before["shared_messages"]
    captured = []
    async def generate(primary, backup, system, history, text):
        captured.append((system, history))
        return "Mulai satu langkah kecil."
    bot.module_provider_resolver = SimpleNamespace(generate=generate)
    call(bot, "chat", "Bagaimana membangun kebiasaan?")
    assert "langkah kecil" in captured[-1][0]
    assert "Malang Strudel" not in captured[-1][0]
    publish_book(store, book_values(store, description="Versi baru"), book)
    assert call(bot, "chat", "lanjut")[0][0] == "Versi baru"
    call(bot, "chat", "kebiasaan")
    assert captured[-1][1] == []
    monkeypatch.setattr("app.shared_modules.now", lambda: "2026-10-01T00:00:00+00:00")
    assert "berakhir" in call(bot, "chat", "lanjut")[0][0]
    assert len(captured) == 2


def test_expiry_during_generation_discards_answer(env, monkeypatch):
    bot, store, values = env
    save(store, {**values, "short_code": "learning"}, "learning")
    publish_book(store, book_values(store))
    clock = ["2026-09-03T00:00:00+00:00"]
    monkeypatch.setattr("app.shared_modules.now", lambda: clock[0])
    call(bot, "learning")
    async def generate(*args):
        clock[0] = "2026-10-01T00:00:00+00:00"
        return "Should never be delivered"
    bot.module_provider_resolver = SimpleNamespace(generate=generate)
    response = call(bot, "chat", "kebiasaan")
    assert "jadwal berubah" in response[0][0]
    assert not _database_rows(bot.database.path)["shared_messages"]


def pdf_bytes(text="Learning text for habits", blank=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=500, height=500)
    if not blank:
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 50 450 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_pdf_extraction_worker_and_scan_rejection():
    result = extract_book(pdf_bytes())
    assert result["pages"] == ["Learning text for habits"]
    assert result["report"]["readable_pages"] == 1
    with pytest.raises(ValueError, match="OCR"):
        extract_book(pdf_bytes(blank=True))
    with pytest.raises(ValueError):
        extract_book(b"not PDF")
    assert "error" in _extract(b"%PDF-invalid")


def test_admin_forms_auth_csrf_upload_review_publish(env):
    bot, store, values = env
    with TestClient(create_admin_app(bot.settings, bot.database)) as client:
        assert client.get("/admin/shared-modules", follow_redirects=False).status_code == 303
        assert client.post("/admin/learning/books/new", data={}).status_code == 200  # login redirect followed
        client.post("/admin/login", data={"username": "admin", "password": "strong-password"})
        page = client.get("/admin/shared-modules/new")
        csrf = re.search(r'name="csrf_token" value="([a-f0-9]+)"', page.text)[1]
        assert client.post("/admin/shared-modules/new", data={**values, "action": "publish"}).status_code == 403
        result = client.post("/admin/shared-modules/new", data={**values, "action": "publish", "csrf_token": csrf})
        assert result.status_code == 200 and "Published v1" in result.text
        assert client.get("/admin/learning").status_code == 200
        legacy_editor = client.get("/admin/shared-modules/learning", follow_redirects=False)
        assert legacy_editor.status_code == 303
        assert legacy_editor.headers["location"] == "/admin/learning"
        data = {**book_values(store), "source_id": "", "reviewed": "1", "action": "draft", "csrf_token": csrf}
        result = client.post("/admin/learning/books/new", data=data, files={"file": ("real.pdf", pdf_bytes(), "application/pdf")})
        assert result.status_code == 200, result.text
        assert "Ekstraksi siap ditinjau" in result.text
        row = store.books()[0]
        draft = json.loads(row["draft_json"])
        assert not draft["reviewed"] and not row["live_json"]
        assert client.get(f"/admin/learning/sources/{draft['source_id']}").status_code == 200
        result = client.post(f"/admin/learning/books/{row['id']}", data={**draft, "active": "1", "reviewed": "1", "revision": row["updated_at"], "csrf_token": csrf, "action": "publish"})
        assert result.status_code == 200, result.text
        assert store.book(row["id"])["live_json"]
        assert client.get("/admin/learning/sources/missing").status_code == 404
        assert client.get(f"/admin/learning/sources/{draft['source_id']}?page_number=999").status_code == 404


def test_company_id_migration_preserves_shared_history_and_pdf_blob(env):
    bot, store, values = env
    key = save(store, {**values, "context_mode": "company"})
    store.add_turn(42, key, "malang-strudel", "module:1:company", "Question", "Answer")
    source_id = source(store)
    bot.database.migrate_company_ids({"malang-strudel": "ms"}, actor="admin")
    assert store.history(42, key, "ms", "module:1:company")[0]["content"] == "Question"
    assert store.source(source_id)
