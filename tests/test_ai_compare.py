import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import admin as admin_module
from app.config import Settings
from app.credentials import ENCRYPTION_KEY_ENV
from app.database import Database
from app.model_catalog import CatalogModel, CatalogResult
from app.shared_modules import SharedStore


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv(ENCRYPTION_KEY_ENV, Fernet.generate_key().decode())
    database = Database(tmp_path / "compare.db")
    database.initialize()
    return database


@pytest.fixture
def client(db, tmp_path):
    settings = Settings(
        telegram_bot_token="fake",
        ai_provider="openai",
        ai_api_key="fake",
        ai_model="test",
        ai_base_url=None,
        database_path=db.path,
        users_file=tmp_path / "users.json",
        companies_file=tmp_path / "companies.json",
        role_profiles_file=Path("config/role_profiles.json"),
        project_root=Path("."),
        history_limit=12,
        max_concurrent_updates=4,
        knowledge_max_chars=50000,
        max_response_chars=12000,
        log_level="INFO",
        admin_username="admin",
        admin_password="strong-password",
        admin_session_secret="x" * 32,
        admin_host="127.0.0.1",
        admin_port=8080,
        admin_cookie_secure=False,
    )
    with TestClient(admin_module.create_admin_app(settings, db)) as browser:
        yield browser


def login(client):
    client.post("/admin/login", data={"username": "admin", "password": "strong-password"})
    page = client.get("/admin/ai-compare")
    return re.search(r'name="csrf_token" value="([a-f0-9]+)"', page.text).group(1)


def _ai(db, label, provider, model):
    profile = db.create_ai_runtime_profile(
        "",
        label,
        provider,
        "",
        "",
        "",
        "tester",
        api_key="secret",
        discoverable=True,
    )
    db.record_model_catalog(
        profile,
        CatalogResult("success", (CatalogModel(model, True),)),
        "tester",
    )
    return f"{profile.profile_id}|{model}"


def _module(db, primary):
    db.create_company("company-a", "Company A", "tester")
    profile_id, _, model = primary.partition("|")
    module = db.create_module(
        "company-a",
        "",
        "Threads Generator",
        "Generate copy-ready threads",
        "tester",
        ai_runtime_profile_id=profile_id,
        ai_model=model,
    )
    db.save_module_playbook_draft("company-a", module.module_id, "Jawab singkat.", "tester")
    db.publish_module_playbook("company-a", module.module_id, "tester")
    return module


def _shared_module(db, primary):
    store = SharedStore(db)
    module_id = store.save_module(
        "",
        {
            "name": "Shared Writer",
            "description": "Write shared output",
            "instruction": "Gunakan gaya shared.",
            "short_code": "sw",
            "context_mode": "independent",
            "ai_selection": primary,
            "backup_ai_selection": "",
            "active": "1",
        },
        "tester",
        publish=True,
    )
    return module_id


def test_ai_compare_page_and_validation(client, db):
    first = _ai(db, "GPT", "openai", "gpt-5.1")
    second = _ai(db, "DeepSeek", "deepseek", "deepseek-chat")
    module = _module(db, first)
    csrf = login(client)
    page = client.get("/admin/ai-compare")
    assert page.status_code == 200
    assert "AI Compare Lab" in page.text
    assert "Company A / Threads Generator" in page.text
    response = client.post(
        "/admin/ai-compare",
        data={
            "csrf_token": csrf,
            "mode": "module",
            "module_value": f"{module.company_id}|{module.module_id}",
            "prompt": "Buat 1 contoh.",
            "ai_selection_1": first,
            "ai_selection_2": first,
        },
    )
    assert response.status_code == 400
    assert "tidak boleh sama" in response.text
    assert second in page.text


def test_ai_compare_renders_parallel_results(client, db, monkeypatch):
    first = _ai(db, "GPT", "openai", "gpt-5.1")
    second = _ai(db, "DeepSeek", "deepseek", "deepseek-chat")
    module = _module(db, first)

    async def fake_run(resolver, profiles, system_prompt, user_prompt):
        assert "Jawab singkat." in system_prompt
        assert user_prompt == "Buat 1 contoh."
        return [
            {
                "label": profiles[0].label,
                "model": profiles[0].model,
                "status": "Sukses",
                "duration_seconds": 1.2,
                "input_tokens": 1000,
                "output_tokens": 200,
                "usage_is_estimated": False,
                "cost_usd": 0.00325,
                "cost_idr": 57,
                "pricing_note": "Estimasi harga publik per 1M token",
                "content": "Jawaban GPT",
                "error": "",
            },
            {
                "label": profiles[1].label,
                "model": profiles[1].model,
                "status": "Timeout",
                "duration_seconds": 300.0,
                "input_tokens": None,
                "output_tokens": None,
                "usage_is_estimated": False,
                "cost_usd": None,
                "cost_idr": None,
                "pricing_note": "Tidak dihitung karena request gagal",
                "content": "",
                "error": "TimeoutError",
            },
        ]

    monkeypatch.setattr(admin_module, "run_ai_compare", fake_run)
    csrf = login(client)
    response = client.post(
        "/admin/ai-compare",
        data={
            "csrf_token": csrf,
            "mode": "module",
            "module_value": f"{module.company_id}|{module.module_id}",
            "prompt": "Buat 1 contoh.",
            "ai_selection_1": first,
            "ai_selection_2": second,
        },
    )
    assert response.status_code == 200
    assert "Hasil perbandingan" in response.text
    assert "Jawaban GPT" in response.text
    assert "TimeoutError" in response.text
    assert "Rp 57" in response.text


def test_ai_compare_renders_judge_recommendation(client, db, monkeypatch):
    first = _ai(db, "GPT", "openai", "gpt-5.1")
    second = _ai(db, "DeepSeek", "deepseek", "deepseek-chat")
    judge = _ai(db, "Claude", "anthropic", "claude-sonnet-4-5")
    module = _module(db, first)

    async def fake_run(resolver, profiles, system_prompt, user_prompt):
        return [
            {
                "label": profiles[0].label,
                "model": profiles[0].model,
                "status": "Sukses",
                "duration_seconds": 1.2,
                "input_tokens": 1000,
                "output_tokens": 200,
                "usage_is_estimated": False,
                "cost_usd": 0.00325,
                "cost_idr": 57,
                "pricing_note": "Estimasi harga publik per 1M token",
                "content": "Jawaban GPT lengkap",
                "error": "",
            },
            {
                "label": profiles[1].label,
                "model": profiles[1].model,
                "status": "Sukses",
                "duration_seconds": 2.0,
                "input_tokens": 800,
                "output_tokens": 180,
                "usage_is_estimated": False,
                "cost_usd": 0.0005,
                "cost_idr": 9,
                "pricing_note": "Estimasi harga publik per 1M token",
                "content": "Jawaban DeepSeek lengkap",
                "error": "",
            },
        ]

    async def fake_judge(resolver, profile, *, module_name, user_prompt, results):
        assert profile.label == "Claude"
        assert module_name == "Threads Generator"
        assert user_prompt == "Buat 1 contoh."
        assert len(results) == 2
        return {
            "label": profile.label,
            "model": profile.model,
            "status": "Sukses",
            "duration_seconds": 3.0,
            "input_tokens": 500,
            "output_tokens": 120,
            "usage_is_estimated": False,
            "cost_usd": 0.0033,
            "cost_idr": 58,
            "pricing_note": "Estimasi harga publik per 1M token",
            "content": "Rekomendasi terbaik adalah DeepSeek karena biaya lebih kecil dan kualitas cukup.",
            "error": "",
        }

    monkeypatch.setattr(admin_module, "run_ai_compare", fake_run)
    monkeypatch.setattr(admin_module, "run_ai_compare_judge", fake_judge)
    csrf = login(client)
    response = client.post(
        "/admin/ai-compare",
        data={
            "csrf_token": csrf,
            "mode": "module",
            "module_value": f"{module.company_id}|{module.module_id}",
            "prompt": "Buat 1 contoh.",
            "ai_selection_1": first,
            "ai_selection_2": second,
            "judge_selection": judge,
        },
    )
    assert response.status_code == 200
    assert "Rekomendasi AI penilai" in response.text
    assert "Rekomendasi terbaik adalah DeepSeek" in response.text
    assert "Cost penilai Rp 58" in response.text


def test_ai_compare_custom_prompt_ignores_module_context(client, db, monkeypatch):
    first = _ai(db, "GPT", "openai", "gpt-5.1")
    second = _ai(db, "DeepSeek", "deepseek", "deepseek-chat")
    _module(db, first)

    async def fake_run(resolver, profiles, system_prompt, user_prompt):
        assert "Jawab singkat." not in system_prompt
        assert "Tidak ada konteks module" in system_prompt
        assert user_prompt == "Bandingkan dua ide ini."
        return [
            {
                "label": profiles[0].label,
                "model": profiles[0].model,
                "status": "Sukses",
                "duration_seconds": 1.0,
                "input_tokens": 100,
                "output_tokens": 50,
                "usage_is_estimated": False,
                "cost_usd": 0.001,
                "cost_idr": 18,
                "pricing_note": "Estimasi harga publik per 1M token",
                "content": "Jawaban custom 1",
                "error": "",
            },
            {
                "label": profiles[1].label,
                "model": profiles[1].model,
                "status": "Sukses",
                "duration_seconds": 1.1,
                "input_tokens": 100,
                "output_tokens": 50,
                "usage_is_estimated": False,
                "cost_usd": 0.001,
                "cost_idr": 18,
                "pricing_note": "Estimasi harga publik per 1M token",
                "content": "Jawaban custom 2",
                "error": "",
            },
        ]

    monkeypatch.setattr(admin_module, "run_ai_compare", fake_run)
    csrf = login(client)
    response = client.post(
        "/admin/ai-compare",
        data={
            "csrf_token": csrf,
            "mode": "custom",
            "prompt": "Bandingkan dua ide ini.",
            "use_instruction": "1",
            "use_knowledge": "1",
            "ai_selection_1": first,
            "ai_selection_2": second,
        },
    )
    assert response.status_code == 200
    assert "Prompt sendiri" in response.text
    assert "Jawaban custom 1" in response.text


def test_ai_compare_can_use_shared_module(client, db, monkeypatch):
    first = _ai(db, "GPT", "openai", "gpt-5.1")
    second = _ai(db, "DeepSeek", "deepseek", "deepseek-chat")
    shared_id = _shared_module(db, first)

    async def fake_run(resolver, profiles, system_prompt, user_prompt):
        assert "Gunakan gaya shared." in system_prompt
        assert "Modul: Shared Writer" in system_prompt
        assert user_prompt == "Buat 1 contoh."
        return [
            {
                "label": profiles[0].label,
                "model": profiles[0].model,
                "status": "Sukses",
                "duration_seconds": 1.0,
                "input_tokens": 100,
                "output_tokens": 50,
                "usage_is_estimated": False,
                "cost_usd": 0.001,
                "cost_idr": 18,
                "pricing_note": "Estimasi harga publik per 1M token",
                "content": "Jawaban shared",
                "error": "",
            },
            {
                "label": profiles[1].label,
                "model": profiles[1].model,
                "status": "Sukses",
                "duration_seconds": 1.1,
                "input_tokens": 100,
                "output_tokens": 50,
                "usage_is_estimated": False,
                "cost_usd": 0.001,
                "cost_idr": 18,
                "pricing_note": "Estimasi harga publik per 1M token",
                "content": "Jawaban backup",
                "error": "",
            },
        ]

    monkeypatch.setattr(admin_module, "run_ai_compare", fake_run)
    csrf = login(client)
    page = client.get("/admin/ai-compare")
    assert "Modul bersama / Shared Writer /SW" in page.text
    response = client.post(
        "/admin/ai-compare",
        data={
            "csrf_token": csrf,
            "mode": "module",
            "module_value": f"shared|{shared_id}",
            "prompt": "Buat 1 contoh.",
            "ai_selection_1": first,
            "ai_selection_2": second,
        },
    )
    assert response.status_code == 200
    assert "Jawaban shared" in response.text
