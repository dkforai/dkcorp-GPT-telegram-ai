"""Synthetic catalog HTTP and disposable SQLite; no live keys or provider calls."""
import asyncio
from dataclasses import replace

import httpx
import pytest

from app import model_catalog as catalog
from app.database import Database
from app.model_catalog import CatalogModel, CatalogResult
from app.providers import ModuleProviderResolver, ProviderRequestError
from test_ai_settings import SECRET, client, db, login, update


def connection(db, provider="openai"):
    return db.create_ai_connection(provider, SECRET, "tester")


def snapshot(*ids):
    return CatalogResult("success", tuple(CatalogModel(i, True) for i in ids))


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(catalog.httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("provider,endpoint,header", [
    ("openai", "https://api.openai.com/v1/models", "authorization"),
    ("deepseek", "https://api.deepseek.com/models", "authorization"),
    ("anthropic", "https://api.anthropic.com/v1/models", "x-api-key"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/models", "x-goog-api-key"),
])
def test_provider_catalog_http_and_pagination(db, monkeypatch, provider, endpoint, header):
    profile = connection(db, provider)
    requests = []
    def handler(request):
        assert str(request.url).split("?")[0] == endpoint
        assert request.method == "GET" and not request.content
        assert request.headers[header] == (f"Bearer {SECRET}" if header == "authorization" else SECRET)
        assert SECRET not in str(request.url)
        requests.append(request)
        page = len(requests)
        if provider == "gemini":
            if page == 1:
                return httpx.Response(200, json={"models": [
                    {"name": "models/gemini-text-test", "supportedGenerationMethods": ["generateContent"]}],
                    "nextPageToken": "page-two"})
            assert request.url.params["pageToken"] == "page-two"
            return httpx.Response(200, json={"models": [
                {"name": "models/embedding-test", "supportedGenerationMethods": ["embedContent"]}]})
        if provider == "anthropic":
            assert request.headers["anthropic-version"] == "2023-06-01"
            if page == 1:
                return httpx.Response(200, json={"data": [{"id": "claude-test-1"}], "has_more": True, "last_id": "claude-test-1"})
            assert request.url.params["after_id"] == "claude-test-1"
            return httpx.Response(200, json={"data": [{"id": "claude-test-2"}], "has_more": False})
        model = "gpt-4.1" if provider == "openai" else "deepseek-chat"
        return httpx.Response(200, json={"data": [{"id": model}, {"id": "embedding-test"}]})
    mock_http(monkeypatch, handler)
    result = asyncio.run(catalog.discover_models(profile))
    assert result.status == "success" and len(result.models) == 2
    assert sum(m.selectable for m in result.models) == (2 if provider == "anthropic" else 1)
    assert len(requests) == (2 if provider in {"gemini", "anthropic"} else 1)


@pytest.mark.parametrize("status,expected", [(401, "authentication"), (403, "authentication"),
    (429, "rate_limit"), (500, "unavailable"), (408, "unavailable"), (400, "request"), (302, "request")])
def test_discovery_errors_never_echo_secret_or_follow_redirect(db, monkeypatch, status, expected):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://evil.example"}, text=SECRET)
    mock_http(monkeypatch, handler)
    result = asyncio.run(catalog.discover_models(connection(db)))
    assert result.status == expected and not result.models
    assert len(requests) == 1 and SECRET not in repr(result)


@pytest.mark.parametrize("body", [[], {}, {"data": {}}, {"data": [None]},
    {"data": [{"id": "<script>"}]}, {"data": [{"id": SECRET}]},
    {"data": [{"id": "gpt-4.1"}], "has_more": True},
    {"data": [{"id": "gpt-4.1"}], "has_more": "false"}])
def test_malformed_catalog_rejected(db, monkeypatch, body):
    mock_http(monkeypatch, lambda r: httpx.Response(200, json=body))
    assert asyncio.run(catalog.discover_models(connection(db))).status == "response"


def test_catalog_limits_and_partial_page_failure(db, monkeypatch):
    profile = connection(db, "anthropic")
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"data": [{"id": "claude-test"}], "has_more": True, "last_id": "claude-test"})
        return httpx.Response(500, text=SECRET)
    mock_http(monkeypatch, handler)
    result = asyncio.run(catalog.discover_models(profile))
    assert result.status == "unavailable" and not result.models


@pytest.mark.parametrize("limit", ["MAX_RESPONSE_BYTES", "MAX_MODELS", "MAX_PAGES"])
def test_catalog_bounds(db, monkeypatch, limit):
    profile = connection(db, "anthropic")
    monkeypatch.setattr(catalog, limit, 1)
    def handler(r):
        return httpx.Response(200, json={"data": [{"id": "claude-one"}, {"id": "claude-two"}],
                                        "has_more": True, "last_id": "claude-two"})
    mock_http(monkeypatch, handler)
    assert asyncio.run(catalog.discover_models(profile)).status == "response"


def test_repeated_cursor_rejected(db, monkeypatch):
    profile = connection(db, "anthropic")
    calls = []
    def handler(r):
        calls.append(r)
        return httpx.Response(200, json={"data": [{"id": "claude-test"}], "has_more": True, "last_id": "claude-test"})
    mock_http(monkeypatch, handler)
    assert asyncio.run(catalog.discover_models(profile)).status == "response"
    assert len(calls) == 2


def test_secret_reflected_in_pagination_is_never_put_in_url(db, monkeypatch):
    profile = connection(db, "gemini")
    requests = []
    def handler(request):
        requests.append(request)
        assert SECRET not in str(request.url)
        return httpx.Response(200, json={"models": [{"name": "models/gemini-test",
            "supportedGenerationMethods": ["generateContent"]}], "nextPageToken": SECRET})
    mock_http(monkeypatch, handler)
    result = asyncio.run(catalog.discover_models(profile))
    assert result.status == "response" and len(requests) == 1


def test_configuration_and_inactive_do_not_contact_provider(db, monkeypatch):
    profile = connection(db)
    def handler(r):
        raise AssertionError("Unexpected network call")
    mock_http(monkeypatch, handler)
    for p in (replace(profile, active=False), replace(profile, api_key_ciphertext="corrupt"),
              replace(profile, base_url="https://evil.example")):
        assert asyncio.run(catalog.discover_models(p)).status == "configuration"


def test_snapshot_rotation_staleness_failure_and_empty_result(db):
    profile = connection(db)
    assert db.record_model_catalog(profile, snapshot("gpt-4.1"), "tester")
    assert db.record_model_catalog(profile, CatalogResult("unavailable"), "tester")
    assert [m["model_id"] for m in db.list_ai_models(profile.profile_id)] == ["gpt-4.1"]
    rotated = update(db, profile, api_key="replacement-synthetic-key")
    assert db.list_ai_models(profile.profile_id) == []
    assert not db.record_model_catalog(profile, snapshot("gpt-4.1"), "tester")
    assert db.record_model_catalog(rotated, snapshot("gpt-4.1"), "tester")
    assert db.record_model_catalog(rotated, snapshot(), "tester")
    assert db.list_ai_models(profile.profile_id) == []
    inactive = db.set_ai_runtime_profile_active(profile.profile_id, False, "tester")
    assert not db.record_model_catalog(rotated, snapshot("gpt-4.1"), "tester")
    assert not db.record_model_catalog(inactive, snapshot("gpt-4.1"), "tester")


def test_module_model_selection_is_separate_from_key_and_other_modules(db):
    db.create_company("company", "Company", "tester")
    profile = connection(db)
    db.record_model_catalog(profile, snapshot("gpt-4.1", "gpt-4.1-mini"), "tester")
    module = db.create_module("company", "one", "One", "", "tester",
        ai_runtime_profile_id=profile.profile_id, ai_model="gpt-4.1",
        backup_ai_runtime_profile_id=profile.profile_id, backup_ai_model="gpt-4.1-mini")
    other = db.create_module("company", "two", "Two", "", "tester",
        ai_runtime_profile_id=profile.profile_id, ai_model="gpt-4.1-mini")
    assert db.get_module_ai_profile(module).model == "gpt-4.1"
    assert db.get_module_ai_profile(module, backup=True).model == "gpt-4.1-mini"
    assert db.get_module_ai_profile(other).model == "gpt-4.1-mini"
    assert db.get_ai_runtime_profile(profile.profile_id).model == ""
    assert db.get_ai_runtime_profile(profile.profile_id).api_key_ciphertext == profile.api_key_ciphertext
    db.initialize()
    assert db.get_module_admin("company", "one") == module
    changed = db.update_module("company", "one", "Updated", "", "tester", profile.profile_id)
    assert changed.ai_model == module.ai_model and changed.backup_ai_model == module.backup_ai_model
    assert db.get_module_admin("company", "two") == other
    assert db.get_module_playbook_admin("company", "one")["ai_model"] == "gpt-4.1"
    changed = db.update_module("company", "one", "Updated", "", "tester", profile.profile_id,
        ai_model="gpt-4.1-mini", backup_ai_model="gpt-4.1")
    assert db.get_module_ai_profile(changed).model == "gpt-4.1-mini"
    assert db.get_module_ai_profile(changed, backup=True).model == "gpt-4.1"
    assert db.get_module_admin("company", "two") == other
    update(db, profile, api_key="replacement-synthetic-key")
    assert db.get_module_ai_profile(module) is None


def test_invalid_selection_never_persists(db):
    db.create_company("company", "Company", "tester")
    profile = connection(db)
    db.record_model_catalog(profile, CatalogResult("success", (CatalogModel("embedding-test", False),)), "tester")
    for model in ("", "forged", "embedding-test"):
        with pytest.raises(ValueError):
            db.create_module("company", "one", "One", "", "tester", profile.profile_id, ai_model=model)
    db.record_model_catalog(profile, snapshot("gpt-4.1"), "tester")
    with pytest.raises(ValueError):
        db.create_module("company", "one", "One", "", "tester", profile.profile_id,
            ai_model="gpt-4.1", backup_ai_runtime_profile_id=profile.profile_id, backup_ai_model="gpt-4.1")
    db.set_ai_runtime_profile_active(profile.profile_id, False, "tester")
    with pytest.raises(ValueError):
        db.create_module("company", "one", "One", "", "tester", profile.profile_id, ai_model="gpt-4.1")
    assert db.get_module_admin("company", "one") is None


def test_settings_to_module_end_to_end(client, db, monkeypatch):
    db.create_company("company", "Company", "tester")
    csrf = login(client)
    saved = client.post("/admin/runtime-profiles", data={"csrf_token": csrf, "provider": "openai", "api_key": SECRET,
        "label": "forged-label", "model": "forged-model", "base_url": "https://evil.example"})
    assert saved.status_code == 200
    profile = db.list_ai_runtime_profiles()[0]
    assert not profile.model and not profile.base_url and profile.active
    assert profile.label != "forged-label"
    empty = client.get("/admin/modules/new").text
    assert 'name="ai_selection"' not in empty
    assert 'Belum ada model AI yang tersedia' in empty
    assert 'href="/admin/settings/ai"' in empty and 'Tes &amp; ambil model' in empty
    assert 'Belum ada AI aktif' not in empty
    async def discover(p):
        assert p.profile_id == profile.profile_id
        return CatalogResult("success", (CatalogModel("gpt-4.1", True), CatalogModel("gpt-4.1-mini", True),
                                         CatalogModel("embedding-test", False, "Belum didukung")))
    monkeypatch.setattr("app.admin.discover_models", discover)
    tested = client.post(f"/admin/runtime-profiles/{profile.profile_id}/test", data={"csrf_token": csrf})
    assert tested.status_code == 200 and "3 model" in tested.text and "embedding-test" in tested.text
    page = client.get("/admin/modules/new")
    assert "gpt-4.1" in page.text and "embedding-test" in page.text
    assert 'name="ai_selection"' in page.text
    response = client.post("/admin/modules", data={"csrf_token": csrf, "company_id": "company",
        "name": "Marketing", "ai_selection": f"{profile.profile_id}|gpt-4.1",
        "backup_ai_selection": f"{profile.profile_id}|gpt-4.1-mini", "active": "1"})
    assert response.status_code == 200 and response.url.path == "/admin/modules/company/marketing"
    assert f'value="{profile.profile_id}|gpt-4.1" selected' in response.text
    assert f'value="{profile.profile_id}|gpt-4.1-mini" selected' in response.text
    module = db.get_module_admin("company", "marketing")
    assert module.ai_model == "gpt-4.1" and module.backup_ai_model == "gpt-4.1-mini"
    kept = client.post(f"/admin/runtime-profiles/{profile.profile_id}", data={"csrf_token": csrf, "provider": "openai", "api_key": ""})
    assert kept.status_code == 200 and len(db.list_ai_models(profile.profile_id)) == 3
    rejected = client.post("/admin/modules/company/marketing", data={"csrf_token": csrf, "name": "Changed",
        "ai_selection": f"{profile.profile_id}|embedding-test", "backup_ai_selection": ""})
    assert "Pilih model chat" in rejected.text
    assert db.get_module_admin("company", "marketing") == module
    client.post(f"/admin/runtime-profiles/{profile.profile_id}", data={"csrf_token": csrf,
        "provider": "openai", "api_key": "replacement-synthetic-key"})
    unavailable = client.get("/admin/modules/company/marketing")
    assert "tidak tersedia, tes provider" in unavailable.text
    assert db.get_module_ai_profile(module) is None


def test_same_provider_different_models_runtime_and_backup(db):
    db.create_company("company", "Company", "tester")
    profile = connection(db)
    db.record_model_catalog(profile, snapshot("gpt-4.1", "gpt-4.1-mini"), "tester")
    module = db.create_module("company", "one", "One", "", "tester", profile.profile_id,
        ai_model="gpt-4.1", backup_ai_runtime_profile_id=profile.profile_id, backup_ai_model="gpt-4.1-mini")
    calls = []
    class Adapter:
        def __init__(self, model): self.model = model
        async def generate(self, *args):
            calls.append(self.model)
            if self.model == "gpt-4.1": raise ProviderRequestError(503)
            return "Backup result"
    def factory(provider, key, model, base_url):
        assert provider == "openai" and key == SECRET
        return Adapter(model)
    resolver = ModuleProviderResolver(factory)
    response = asyncio.run(resolver.generate(db.get_module_ai_profile(module),
        lambda: db.get_module_ai_profile(module, backup=True), "system", [], "message"))
    assert response == "Backup result" and calls == ["gpt-4.1", "gpt-4.1-mini"]


@pytest.mark.parametrize("provider,model,expected", [
    ("openai", "gpt-4.1", True), ("openai", "ft:gpt-4.1:org:custom", True),
    ("openai", "gpt-5-pro", False), ("openai", "o3-deep-research", False),
    ("openai", "gpt-realtime", False), ("openai", "text-embedding-3-small", False),
    ("openai", "gpt-5-codex", False), ("openai", "o1-mini", False),
    ("gemini", "gemini-text-test", True), ("gemini", "gemini-image-test", False),
    ("gemini", "gemini-tts-test", False), ("gemini", "embedding-test", False),
])
def test_model_capability_policy(provider, model, expected):
    assert catalog.classify_model(provider, model, {"supportedGenerationMethods": ["generateContent"]}).selectable == expected


def test_discovery_timeout_and_cancellation_close_client(db, monkeypatch):
    profile = connection(db)
    original = httpx.AsyncClient
    clients = []
    async def handler(request):
        await asyncio.Event().wait()
    def factory(**kwargs):
        instance = original(**kwargs, transport=httpx.MockTransport(handler))
        clients.append(instance)
        return instance
    monkeypatch.setattr(catalog.httpx, "AsyncClient", factory)
    monkeypatch.setattr(catalog, "DISCOVERY_TIMEOUT_SECONDS", 0.02)
    assert asyncio.run(catalog.discover_models(profile)).status == "unavailable"
    assert clients[-1].is_closed
    async def cancel():
        monkeypatch.setattr(catalog, "DISCOVERY_TIMEOUT_SECONDS", 30)
        task = asyncio.create_task(catalog.discover_models(profile))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())
    assert clients[-1].is_closed


def test_legacy_and_catalog_cannot_select_same_effective_backup(db):
    db.create_company("company", "Company", "tester")
    legacy = db.create_ai_runtime_profile("old", "Old AI", "openai", "", "gpt-4.1", "", "tester", api_key=SECRET)
    db.record_model_catalog(legacy, snapshot("gpt-4.1"), "tester")
    for primary, backup in (("", "gpt-4.1"), ("gpt-4.1", "")):
        with pytest.raises(ValueError, match="berbeda"):
            db.create_module("company", "one", "One", "", "tester", legacy.profile_id,
                ai_model=primary, backup_ai_runtime_profile_id=legacy.profile_id, backup_ai_model=backup)


def test_empty_catalog_and_legacy_probe_status_are_distinguishable(client, db):
    csrf = login(client)
    legacy = db.create_ai_runtime_profile("old", "Old AI", "openai", "", "gpt-4.1", "", "tester", api_key=SECRET)
    db.record_ai_connection_test(legacy, "success", "tester")
    assert "Tes model lama; katalog belum diambil" in client.get("/admin/settings/ai").text
    profile = connection(db)
    db.record_model_catalog(profile, snapshot(), "tester")
    assert db.get_ai_runtime_profile(profile.profile_id).catalog_updated_at
    page = client.get("/admin/settings/ai")
    assert "Provider mengembalikan 0 model" in page.text


def test_failed_refresh_keeps_selected_models_but_successful_removal_blocks_runtime(db):
    db.create_company("company", "Company", "tester")
    profile = connection(db)
    db.record_model_catalog(profile, snapshot("gpt-4.1"), "tester")
    module = db.create_module("company", "one", "One", "", "tester", profile.profile_id, ai_model="gpt-4.1")
    db.record_model_catalog(profile, CatalogResult("unavailable"), "tester")
    assert db.get_module_ai_profile(module).model == "gpt-4.1"
    db.record_model_catalog(profile, snapshot("gpt-4.1-mini"), "tester")
    assert db.get_module_ai_profile(module) is None
    assert db.get_module_admin("company", "one").ai_model == "gpt-4.1"


def test_missing_discovery_permission_never_persists_provider_body(client, db, monkeypatch):
    profile = connection(db)
    csrf = login(client)
    async def failed(p): return CatalogResult("authentication")
    monkeypatch.setattr("app.admin.discover_models", failed)
    response = client.post(f"/admin/runtime-profiles/{profile.profile_id}/test", data={"csrf_token": csrf})
    assert "tidak punya izin membaca daftar model" in response.text
    assert SECRET not in response.text and not db.list_ai_models(profile.profile_id)


def test_provider_change_rejected_no_key_or_catalog_mutation(client, db):
    profile = connection(db)
    db.record_model_catalog(profile, snapshot("gpt-4.1"), "tester")
    csrf = login(client)
    response = client.post(f"/admin/runtime-profiles/{profile.profile_id}", data={"csrf_token": csrf,
        "provider": "anthropic", "api_key": "different-synthetic-key"})
    assert response.status_code == 400 and "tidak dapat diganti" in response.text
    assert "different-synthetic-key" not in response.text
    assert db.get_ai_runtime_profile(profile.profile_id).api_key_ciphertext == profile.api_key_ciphertext
    assert db.list_ai_models(profile.profile_id)


def test_publish_activation_require_current_model_catalog(db):
    db.create_company("company", "Company", "tester")
    profile = connection(db)
    db.record_model_catalog(profile, snapshot("gpt-4.1"), "tester")
    db.create_module("company", "one", "One", "", "tester", profile.profile_id, ai_model="gpt-4.1")
    db.save_module_playbook_draft("company", "one", "Test playbook", "tester")
    assert db.publish_module_playbook("company", "one", "tester") == 1
    db.set_module_active("company", "one", False, "tester")
    rotated = update(db, profile, api_key="replacement-synthetic-key")
    with pytest.raises(ValueError, match="Pilih model chat"):
        db.publish_module_playbook("company", "one", "tester")
    with pytest.raises(ValueError, match="Pilih model chat"):
        db.set_module_active("company", "one", True, "tester")
    db.record_model_catalog(rotated, snapshot("gpt-4.1"), "tester")
    assert db.set_module_active("company", "one", True, "tester").active


def test_migration_of_existing_module_preserves_choices_and_ciphertext(db):
    db.create_company("company", "Company", "tester")
    legacy = db.create_ai_runtime_profile("old", "Old AI", "openai", "", "gpt-4.1", "", "tester", api_key=SECRET)
    backup = db.create_ai_runtime_profile("backup", "Backup", "deepseek", "", "deepseek-chat", "", "tester", api_key=SECRET)
    original = db.create_module("company", "one", "One", "", "tester", legacy.profile_id,
        backup_ai_runtime_profile_id=backup.profile_id)
    db.save_module_playbook_draft("company", "one", "Original playbook", "tester")
    db.publish_module_playbook("company", "one", "tester")
    with db._connect() as conn:
        conn.execute("ALTER TABLE modules DROP COLUMN ai_model")
        conn.execute("ALTER TABLE modules DROP COLUMN backup_ai_model")
        conn.execute("ALTER TABLE ai_runtime_profiles DROP COLUMN catalog_updated_at")
        conn.execute("DROP TABLE ai_model_catalog")
    db.initialize()
    db.initialize()
    assert db.get_module_admin("company", "one") == original
    assert db.get_module_ai_profile(original).model == "gpt-4.1"
    assert db.get_module_ai_profile(original, backup=True).model == "deepseek-chat"
    assert db.get_ai_runtime_profile(legacy.profile_id).api_key_ciphertext == legacy.api_key_ciphertext
    assert db.get_module_playbook_admin("company", "one")["published_content"] == "Original playbook"
