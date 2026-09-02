"""Credential and provider tests use disposable SQLite and synthetic HTTP only."""
import asyncio
import json
import os
import re
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.admin import create_admin_app
from app.config import Settings
from app.credentials import (
    ENCRYPTION_KEY_ENV, PROVIDERS, CredentialStorageError, resolve_api_key,
)
from app.database import Database
from app.model_catalog import CatalogResult
from app import providers

SECRET = "synthetic-secret-not-a-real-api-key"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv(ENCRYPTION_KEY_ENV, Fernet.generate_key().decode())
    database = Database(tmp_path / "ai.db")
    database.initialize()
    return database


def registered(db, provider="openai", **kwargs):
    return db.create_ai_runtime_profile("", f"Test {provider}", provider, "", "test-model", "", "tester", api_key=SECRET, **kwargs)


@pytest.fixture
def client(db, tmp_path):
    settings = Settings(
        telegram_bot_token="fake", ai_provider="openai", ai_api_key="fake", ai_model="test",
        ai_base_url=None, database_path=db.path, users_file=tmp_path / "users.json",
        companies_file=tmp_path / "companies.json", role_profiles_file=Path("config/role_profiles.json"),
        project_root=Path("."), history_limit=12, max_concurrent_updates=4,
        knowledge_max_chars=50000, max_response_chars=12000, log_level="INFO",
        admin_username="admin", admin_password="strong-password", admin_session_secret="x" * 32,
        admin_host="127.0.0.1", admin_port=8080, admin_cookie_secure=False,
    )
    with TestClient(create_admin_app(settings, db)) as browser:
        yield browser


def login(client):
    client.post("/admin/login", data={"username": "admin", "password": "strong-password"})
    page = client.get("/admin/runtime-profiles/new")
    return re.search(r'name="csrf_token" value="([a-f0-9]+)"', page.text).group(1)


def update(db, profile, **kwargs):
    args = dict(label=profile.label, provider=profile.provider, api_key_env=profile.api_key_env,
                model=profile.model, base_url=profile.base_url, actor="tester")
    args.update(kwargs)
    return db.update_ai_runtime_profile(profile.profile_id, **args)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_encryption_roundtrip_no_plaintext_or_repr(db, provider):
    profile = registered(db, provider)
    assert resolve_api_key(profile) == SECRET
    assert profile.api_key_env == ""
    assert profile.api_key_ciphertext and SECRET not in profile.api_key_ciphertext
    assert profile.api_key_ciphertext not in repr(profile)
    with sqlite3.connect(db.path) as conn:
        assert SECRET not in "\n".join(conn.iterdump())
    for path in db.path.parent.glob("ai.db*"):
        assert SECRET.encode() not in path.read_bytes()


def test_secret_preserved_rotated_and_test_invalidated(db):
    profile = registered(db)
    assert db.record_ai_connection_test(profile, "success", "tester")
    edited = update(db, profile, label="New label", api_key="")
    assert edited.api_key_ciphertext == profile.api_key_ciphertext
    assert not edited.last_test_status
    rotated = update(db, edited, api_key="different-synthetic-key")
    assert resolve_api_key(rotated) == "different-synthetic-key"
    assert not db.record_ai_connection_test(profile, "success", "tester")
    assert not db.get_ai_runtime_profile(profile.profile_id).last_test_status


def test_binding_tamper_and_wrong_master_fail_closed(db, monkeypatch):
    profile = registered(db)
    for changed in (replace(profile, profile_id="other"), replace(profile, provider="gemini"),
                    replace(profile, api_key_ciphertext="invalid")):
        with pytest.raises(CredentialStorageError):
            resolve_api_key(changed)
        with pytest.raises(providers.RuntimeCredentialError):
            providers.ModuleProviderResolver().resolve(changed)
    monkeypatch.setenv(ENCRYPTION_KEY_ENV, Fernet.generate_key().decode())
    with pytest.raises(CredentialStorageError):
        resolve_api_key(profile)
    assert not providers.runtime_profile_is_configured(profile)


def test_provider_change_needs_new_key_and_official_endpoint(db):
    profile = registered(db)
    with pytest.raises(ValueError, match="key baru"):
        update(db, profile, provider="anthropic")
    for endpoint in ("https://evil.example", "http://127.0.0.1:8080", "https://api.openai.com/v1@evil.example"):
        with pytest.raises(ValueError, match="resmi"):
            update(db, profile, base_url=endpoint)
        with pytest.raises(providers.RuntimeCredentialError):
            providers.ModuleProviderResolver().resolve(replace(profile, base_url=endpoint))
    changed = update(db, profile, provider="anthropic", api_key="synthetic-claude-key")
    assert resolve_api_key(changed) == "synthetic-claude-key"


def test_missing_master_leaves_legacy_usable_and_supports_conversion(db, monkeypatch):
    legacy = db.create_ai_runtime_profile("old", "Old AI", "deepseek", "TEST_OLD_KEY", "model", "", "tester")
    monkeypatch.setenv("TEST_OLD_KEY", "legacy-secret")
    master = os.environ[ENCRYPTION_KEY_ENV]
    monkeypatch.delenv(ENCRYPTION_KEY_ENV)
    assert resolve_api_key(legacy) == "legacy-secret"
    assert providers.runtime_profile_is_configured(legacy)
    with pytest.raises(CredentialStorageError):
        registered(db)
    assert len(db.list_ai_runtime_profiles()) == 1
    monkeypatch.setenv(ENCRYPTION_KEY_ENV, master)
    converted = update(db, legacy, api_key=SECRET)
    assert converted.profile_id == "old" and converted.api_key_env == ""
    assert resolve_api_key(converted) == SECRET


def test_additive_legacy_migration_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE ai_runtime_profiles (profile_id TEXT PRIMARY KEY, label TEXT, provider TEXT, api_key_env TEXT, model TEXT, base_url TEXT, active INTEGER, created_at TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO ai_runtime_profiles VALUES ('old','Old AI','openai','LEGACY_KEY','model','',1,'before','before')")
    db = Database(path)
    db.initialize()
    db.initialize()
    monkeypatch.setenv("LEGACY_KEY", SECRET)
    profile = db.get_ai_runtime_profile("old")
    assert resolve_api_key(profile) == SECRET
    assert not profile.api_key_ciphertext and profile.updated_at == "before"


def test_settings_auth_csrf_forms_and_never_echo_key(client, db, monkeypatch):
    db.create_company("company-a", "Company A", actor="tester")
    assert client.get("/admin/settings/ai", follow_redirects=False).status_code == 303
    assert client.post("/admin/runtime-profiles/unknown/test", follow_redirects=False).status_code == 303
    csrf = login(client)
    values = dict(provider="gemini", api_key=SECRET)
    assert client.post("/admin/runtime-profiles", data=values).status_code == 403
    values["csrf_token"] = csrf
    invalid = client.post("/admin/runtime-profiles", data={**values, "provider": "unsupported"})
    assert invalid.status_code == 400 and SECRET not in invalid.text
    saved = client.post("/admin/runtime-profiles", data=values)
    assert saved.status_code == 200 and SECRET not in saved.text
    assert saved.url.path == "/admin/settings/ai"
    profile = db.list_ai_runtime_profiles()[0]
    assert profile.active and not profile.model
    for url in ("/admin/settings/ai", f"/admin/runtime-profiles/{profile.profile_id}/edit", "/admin/activity", "/admin/modules/new"):
        page = client.get(url)
        assert page.status_code == 200
        assert SECRET not in page.text and profile.api_key_ciphertext not in page.text
    form = client.get("/admin/runtime-profiles/new")
    assert 'name="label"' not in form.text and 'name="model"' not in form.text
    assert f'value="{profile.profile_id}"' not in client.get("/admin/modules/new").text
    response = client.post(f"/admin/runtime-profiles/{profile.profile_id}", data={**values, "api_key": ""})
    assert response.status_code == 200
    assert resolve_api_key(db.get_ai_runtime_profile(profile.profile_id)) == SECRET
    monkeypatch.delenv(ENCRYPTION_KEY_ENV)
    invalid = client.post("/admin/runtime-profiles", data=values)
    assert invalid.status_code == 400 and SECRET not in invalid.text
    assert client.post("/admin/runtime-profiles/no-such-ai", data=values).status_code == 404


def test_form_limits_and_probe_rate_limit(client, db, monkeypatch):
    profile = registered(db)
    csrf = login(client)
    assert client.post("/admin/runtime-profiles", content="x" * 16385).status_code == 413
    assert client.post("/admin/runtime-profiles", content="label=a&label=b", headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 400
    calls = []
    async def probe(profile):
        calls.append(profile.profile_id)
        return CatalogResult("success")
    monkeypatch.setattr("app.admin.discover_models", probe)
    url = f"/admin/runtime-profiles/{profile.profile_id}/test"
    assert client.post(url).status_code == 403
    for _ in range(3):
        response = client.post(url, data={"csrf_token": csrf})
        assert response.status_code == 200 and "berhasil diuji" in response.text
    assert client.post(url, data={"csrf_token": csrf}).status_code == 429
    assert len(calls) == 3
    assert db.get_ai_runtime_profile(profile.profile_id).last_test_status == "success"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_real_adapters_with_mock_http_and_probe(db, monkeypatch, provider):
    profile = registered(db, provider)
    requests = []
    real_http_client = httpx.AsyncClient
    def handler(request):
        payload = json.loads(request.content)
        requests.append((request, payload))
        if provider == "anthropic":
            assert request.headers["x-api-key"] == SECRET
            assert request.headers["anthropic-version"] == "2023-06-01"
            return httpx.Response(200, json={"content": [{"type": "thinking", "thinking": "private"}, {"type": "text", "text": "OK"}]})
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        return httpx.Response(200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
    monkeypatch.setattr(providers, "AsyncClient", lambda **kwargs: real_http_client(**kwargs, transport=httpx.MockTransport(handler)))
    async def scenario():
        resolver = providers.ModuleProviderResolver()
        adapter = resolver.resolve(profile)
        try:
            assert await adapter.generate("company context", [{"role": "user", "content": "history"}], "question") == "OK"
        finally:
            await adapter.close()
        assert await providers.test_ai_connection(profile) == "success"
    asyncio.run(scenario())
    request, payload = requests[0]
    assert str(request.url).startswith(PROVIDERS[provider][1].rstrip("/"))
    if provider == "anthropic":
        assert payload["system"] == "company context" and payload["max_tokens"] == 4096
        assert [message["role"] for message in payload["messages"]] == ["user", "user"]
    else:
        assert payload["messages"][0] == {"role": "system", "content": "company context"}
    _, probe = requests[1]
    assert probe["messages"] == [{"role": "user", "content": "Reply OK."}]
    assert "company context" not in json.dumps(probe) and "history" not in json.dumps(probe)
    assert probe.get("max_tokens", probe.get("max_completion_tokens")) == 64


@pytest.mark.parametrize("status,expected", [(401, "authentication"), (403, "authentication"), (429, "rate_limit"), (529, "unavailable"), (400, "request"), (302, "request")])
def test_probe_errors_are_safe_and_never_follow_redirects(db, monkeypatch, caplog, status, expected):
    profile = registered(db, "anthropic")
    requests = []
    real_http_client = httpx.AsyncClient
    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://evil.example"}, json={"error": SECRET})
    monkeypatch.setattr(providers, "AsyncClient", lambda **kwargs: real_http_client(**kwargs, transport=httpx.MockTransport(handler)))
    assert asyncio.run(providers.test_ai_connection(profile)) == expected
    assert len(requests) == 1 and SECRET not in caplog.text


def test_encrypted_cross_provider_failover_and_rotation(db, caplog):
    primary = registered(db, "anthropic")
    backup = registered(db, "gemini")
    calls = []
    keys = []
    class Adapter:
        def __init__(self, provider):
            self.provider = provider
        async def generate(self, *context):
            calls.append((self.provider, context))
            if self.provider == "anthropic":
                raise providers.ProviderRequestError(529)
            return "backup answer"
    def factory(provider, key, model, url):
        keys.append(key)
        return Adapter(provider)
    resolver = providers.ModuleProviderResolver(factory)
    assert asyncio.run(resolver.generate(primary, lambda: backup, "system", [], "question")) == "backup answer"
    assert calls[0][1] == calls[1][1]
    assert SECRET not in caplog.text
    rotated = update(db, primary, api_key="rotated-test-key")
    resolver.resolve(rotated)
    assert keys[-1] == "rotated-test-key"
    assert not providers._can_use_backup(providers.ProviderRequestError(401))
    assert not providers._can_use_backup(providers.ProviderRequestError(400))


def test_probe_concurrency_and_stale_result(client, db, monkeypatch):
    primary = registered(db)
    other = registered(db, "anthropic")
    third = registered(db, "gemini")
    csrf = login(client)
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        count = 0
        async def probe(profile):
            nonlocal count
            count += 1
            if count == 2:
                started.set()
            await release.wait()
            return CatalogResult("success")
        monkeypatch.setattr("app.admin.discover_models", probe)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver", cookies=client.cookies) as browser:
            def url(profile):
                return f"/admin/runtime-profiles/{profile.profile_id}/test"
            tasks = [asyncio.create_task(browser.post(url(profile), data={"csrf_token": csrf})) for profile in (primary, other)]
            await asyncio.wait_for(started.wait(), timeout=2)
            try:
                assert (await browser.post(url(third), data={"csrf_token": csrf})).status_code == 429
                assert (await browser.post(url(primary), data={"csrf_token": csrf})).status_code == 429
                update(db, primary, model="changed-during-test")
            finally:
                release.set()
                results = await asyncio.gather(*tasks)
            assert all(response.status_code == 303 for response in results)
            assert "error=" in results[0].headers["location"]
            assert count == 2
    asyncio.run(scenario())
    assert not db.get_ai_runtime_profile(primary.profile_id).last_test_status
    assert db.get_ai_runtime_profile(other.profile_id).last_test_status == "success"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_invalid_provider_response_does_not_report_success(db, monkeypatch, provider):
    profile = registered(db, provider)
    real_http_client = httpx.AsyncClient
    monkeypatch.setattr(providers, "AsyncClient", lambda **kwargs: real_http_client(**kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"invalid": "response"})) ))
    assert asyncio.run(providers.test_ai_connection(profile)) == "response"


def test_probe_timeout_closes_client_and_cancel_is_not_swallowed(db, monkeypatch):
    profile = registered(db)
    class SlowAdapter:
        closed = False
        async def probe(self):
            await asyncio.Event().wait()
        async def close(self):
            self.closed = True
    async def scenario():
        adapter = SlowAdapter()
        monkeypatch.setattr(providers, "_create_module_provider", lambda *args: adapter)
        monkeypatch.setattr(providers, "MODULE_AI_TIMEOUT_SECONDS", 0.01)
        assert await providers.test_ai_connection(profile) == "unavailable"
        assert adapter.closed
        adapter.closed = False
        monkeypatch.setattr(providers, "MODULE_AI_TIMEOUT_SECONDS", 30)
        task = asyncio.create_task(providers.test_ai_connection(profile))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert adapter.closed
    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["", "contains space", "newline\nin-key", "☃", "a" * 4097])
def test_invalid_keys_do_not_persist(db, key):
    with pytest.raises(CredentialStorageError):
        db.create_ai_runtime_profile("", "New AI", "openai", "", "model", "", "tester", api_key=key)
    assert db.list_ai_runtime_profiles() == []


def test_encrypted_failure_cannot_fall_back_to_legacy_env(db, monkeypatch):
    profile = registered(db)
    monkeypatch.setenv("SOME_KEY", "environment-key")
    broken = replace(profile, api_key_env="SOME_KEY", api_key_ciphertext="invalid")
    with pytest.raises(CredentialStorageError):
        resolve_api_key(broken)


def test_inactive_profile_not_selectable_and_cannot_probe(client, db, monkeypatch):
    db.create_company("company-a", "Company A", actor="tester")
    profile = registered(db, active=False)
    csrf = login(client)
    assert profile.label not in client.get("/admin/modules/new").text
    with pytest.raises(providers.RuntimeCredentialError):
        providers.ModuleProviderResolver().resolve(profile)
    async def probe(profile):
        raise AssertionError("Inactive provider must not be contacted")
    monkeypatch.setattr("app.admin.discover_models", probe)
    response = client.post(f"/admin/runtime-profiles/{profile.profile_id}/test", data={"csrf_token": csrf})
    assert response.status_code == 200 and "Aktifkan provider" in response.text
    assert not db.get_ai_runtime_profile(profile.profile_id).active
