import asyncio
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from app import providers
from test_core import _help_bot, _ai_pair
from test_shared_learning import call


MODULE = "artikel-web-generator"


@pytest.fixture
def article(tmp_path):
    bot = _help_bot(tmp_path)
    db = bot.database
    db.migrate_company_ids({"malang-strudel": "ms"}, actor="admin")
    profile = db.list_ai_runtime_profiles()[0].profile_id
    db.create_module("ms", MODULE, "Artikel web", "", "admin", ai_runtime_profile_id=profile)
    db.save_module_playbook_draft("ms", MODULE, "Tulis artikel panjang sesuai data user", "admin")
    db.publish_module_playbook("ms", MODULE, "admin")
    db.set_membership_module_access(42, "ms", [MODULE, "threads-generator"], "admin")
    db.set_active_module(42, MODULE)
    return bot


def fail_provider(bot):
    calls = []
    async def generate(primary, backup, system, history, text, **kwargs):
        calls.append((text, list(history), kwargs))
        raise providers.ModuleGenerationError("timeout")
    bot.module_provider_resolver = SimpleNamespace(generate=generate)
    return calls


@pytest.mark.parametrize("method,text", [("chat", "ulang"), ("retry", "/ulang")])
def test_retry_retains_exact_input_and_commits_pair_once(article, method, text):
    db = article.database
    calls = fail_provider(article)
    details = 'Chef Linda: "Snow Strudel inovasi baru". Ada 3 foto.'
    assert "tetap tersimpan" in call(article, "chat", details)[0][0]
    assert calls == [(details, [], {"timeout_seconds": 120.0})]
    assert db.get_history(42, 12, "ms", MODULE) == []
    assert db.get_pending_request(42, "ms", MODULE)["content"] == details
    async def success(primary, backup, system, history, user_text, **kwargs):
        assert user_text == details and not history
        assert kwargs["timeout_seconds"] == 120
        return "Artikel selesai"
    article.module_provider_resolver.generate = success
    assert "Artikel selesai" in call(article, method, text)[0][0]
    assert db.get_pending_request(42, "ms", MODULE) is None
    assert db.get_history(42, 12, "ms", MODULE) == [
        {"role": "user", "content": details}, {"role": "assistant", "content": "Artikel selesai"}]
    assert "Tidak ada permintaan" in call(article, method, text)[0][0]


def test_retry_isolated_reset_new_input_and_context_changes(article):
    db = article.database
    calls = fail_provider(article)
    call(article, "chat", "detail lama")
    assert db.get_pending_request(43, "ms", MODULE) is None
    assert db.get_pending_request(42, "amazing-malang", MODULE) is None
    assert db.get_pending_request(42, "ms", "threads-generator") is None
    db.set_active_module(42, "threads-generator")
    call(article, "retry", "/ulang")
    assert len(calls) == 1
    # An ordinary module retains its original timeout and normal error response.
    assert "sedang tidak tersedia" in call(article, "chat", "other question")[0][0]
    assert calls[-1][2] == {}
    db.set_active_module(42, MODULE)
    call(article, "chat", "detail baru")
    assert db.get_pending_request(42, "ms", MODULE)["content"] == "detail baru"
    db.save_module_playbook_draft("ms", MODULE, "Instruksi berubah", "admin")
    db.publish_module_playbook("ms", MODULE, "admin")
    count = len(calls)
    assert "Konteks atau history berubah" in call(article, "retry", "/ulang")[0][0]
    assert len(calls) == count
    call(article, "reset")
    assert db.get_pending_request(42, "ms", MODULE) is None
    call(article, "retry", "/ulang")
    assert len(calls) == count


@pytest.mark.parametrize("restriction", ["whitelist", "module_access", "company"])
def test_access_revoked_during_article_generation_drops_answer(article, restriction):
    db = article.database
    async def revoke(*args, **kwargs):
        if restriction == "module_access":
            db.set_membership_module_access(42, "ms", [], "admin")
        elif restriction == "whitelist":
            with sqlite3.connect(db.path) as c:
                c.execute("UPDATE users SET active=0 WHERE telegram_id=42")
        else:
            db.set_active_company(42, "amazing-malang")
        return "SECRET ANSWER"
    article.module_provider_resolver = SimpleNamespace(generate=revoke)
    replies = call(article, "chat", "Private detail")
    assert all("SECRET ANSWER" not in text for text, _ in replies)
    assert db.get_history(42, 12, "ms", MODULE) == []
    assert db.get_pending_request(42, "ms", MODULE)


def test_article_timeout_only_exact_company_module_pair(article):
    module = article.database.get_module_admin("ms", MODULE)
    assert article._is_article_module(module)
    assert not article._is_article_module(replace(module, company_id="other"))
    assert not article._is_article_module(replace(module, module_id="another"))


def test_pending_completion_is_atomic_and_migration_preserves_input(article):
    db = article.database
    db.set_pending_request(42, "ms", MODULE, "original", "hash")
    with sqlite3.connect(db.path) as c:
        c.execute("CREATE TRIGGER fail_answer BEFORE INSERT ON messages WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.complete_pending_request(42, "ms", MODULE, "original", "answer", "hash")
    assert not db.get_history(42, 10, "ms", MODULE)
    assert db.get_pending_request(42, "ms", MODULE)["content"] == "original"
    db.migrate_company_ids({"ms": "ms-new"}, actor="admin")
    assert db.get_pending_request(42, "ms-new", MODULE)["content"] == "original"


@pytest.mark.parametrize("kind", ["anthropic", "deepseek"])
def test_article_http_timeout_reaches_actual_transport(monkeypatch, kind):
    seen = []
    def handle(request):
        seen.append(request.extensions["timeout"])
        if kind == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}]})
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "test", "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]})
    monkeypatch.setattr(providers, "AsyncClient", lambda **kwargs: httpx.AsyncClient(**kwargs, transport=httpx.MockTransport(handle)))
    primary, _ = _ai_pair()
    primary = replace(primary, provider=kind, base_url="")
    monkeypatch.setenv(primary.api_key_env, "fake-key")
    resolver = providers.ModuleProviderResolver()
    async def exercise():
        assert await resolver.generate(primary, lambda: None, "context", [], "question", timeout_seconds=120) == "OK"
        assert await resolver.generate(primary, lambda: None, "context", [], "question") == "OK"
        for adapter in resolver._cache.values():
            await adapter.close()
    asyncio.run(exercise())
    assert len(seen) == 2
    assert set(seen[0].values()) == {120}
    assert set(seen[1].values()) == {30}


def test_timeout_context_isolated_across_concurrent_calls_backup_and_cancellation(monkeypatch):
    primary, backup = _ai_pair()
    for profile in (primary, backup):
        monkeypatch.setenv(profile.api_key_env, "fake-key")
    seen = []
    guards = []
    original_timeout = asyncio.timeout
    def timeout(value):
        guards.append(value)
        return original_timeout(value)
    monkeypatch.setattr(providers.asyncio, "timeout", timeout)
    def factory(_provider, _key, model, _url):
        async def generate(system, history, user_text):
            await asyncio.sleep(0)
            seen.append((user_text, model, providers.effective_timeout()))
            if user_text == "cancel":
                raise asyncio.CancelledError()
            if model == primary.model:
                raise TimeoutError()
            return "OK"
        return SimpleNamespace(generate=generate)
    resolver = providers.ModuleProviderResolver(factory)
    async def exercise():
        results = await asyncio.gather(
            resolver.generate(primary, lambda: backup, "", [], "article", timeout_seconds=120),
            resolver.generate(primary, lambda: backup, "", [], "normal"))
        assert results == ["OK", "OK"]
        with pytest.raises(asyncio.CancelledError):
            await resolver.generate(primary, lambda: pytest.fail("no fallback"), "", [], "cancel", timeout_seconds=120)
        assert providers.effective_timeout() == 30
    asyncio.run(exercise())
    assert all(value == (30 if text == "normal" else 120) for text, _, value in seen)
    assert sorted(guards) == [30, 30, 120, 120, 120]
