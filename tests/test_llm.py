import json

import httpx
import pytest
from sqlmodel import select

from app.models import ContentItem, ContentStatus, SourceType, TokenPurpose, TokenUsage
from app.settings import Settings
from services.llm import LLMClient, LLMResponseError, token_cost_usd


def _settings() -> Settings:
    return Settings(
        llm_api_key="test-key",
        llm_base_url="https://llm.example/v1",
        llm_primary_model="test-model",
        llm_price_per_million={"test-model": {"input": 2.0, "output": 4.0}},
        _env_file=None,
    )


def _completion(content: str, prompt_tokens: int = 1000, completion_tokens: int = 500) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def test_complete_records_token_usage_and_price(session):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://llm.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        body = request.read()
        assert b"test-model" in body
        return httpx.Response(200, json=_completion("готово"))

    client = LLMClient(
        session,
        settings=_settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert (
        client.complete("system", "user", item_id=None, purpose=TokenPurpose.TRANSLATE) == "готово"
    )

    usage = session.exec(select(TokenUsage)).one()
    assert usage.model == "test-model"
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 500
    assert usage.purpose == TokenPurpose.TRANSLATE
    assert usage.cost_usd == pytest.approx(token_cost_usd(_settings(), "test-model", 1000, 500))
    assert usage.cost_usd == pytest.approx(0.004)


def test_complete_retries_429_and_network_errors(session):
    calls = {"count": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, json={"error": "slow down"})
        if calls["count"] == 2:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json=_completion("ok", 1, 1))

    client = LLMClient(
        session,
        settings=_settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=sleeps.append,
        initial_delay=0.5,
    )
    assert client.complete("system", "user") == "ok"
    assert calls["count"] == 3
    assert sleeps == [0.5, 1.0]
    assert len(session.exec(select(TokenUsage)).all()) == 1


def test_complete_stops_after_three_failures(session):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(429, text="no")

    client = LLMClient(
        session,
        settings=_settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda delay: None,
    )
    with pytest.raises(Exception, match="429"):
        client.complete("system", "user")
    assert calls["count"] == 3
    assert session.exec(select(TokenUsage)).all() == []


def test_complete_does_not_retry_other_http_errors(session):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(401, json={"error": "bad key"})

    client = LLMClient(
        session,
        settings=_settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleeper=lambda delay: None,
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.complete("system", "user")
    assert calls["count"] == 1


def test_embed_records_token_usage(session):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://llm.example/v1/embeddings"
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["model"] == "embed-model"
        assert body["input"] == "гиростабилизация"
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [0.25, 0.5]}],
                "usage": {"prompt_tokens": 12, "total_tokens": 12},
            },
        )

    settings = Settings(
        llm_api_key="test-key",
        llm_base_url="https://llm.example/v1",
        embedding_model="embed-model",
        llm_price_per_million={"embed-model": {"input": 2.0, "output": 0.0}},
        _env_file=None,
    )
    client = LLMClient(
        session,
        settings=settings,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.embed("гиростабилизация", item_id=4) == [0.25, 0.5]
    usage = session.exec(select(TokenUsage)).one()
    assert usage.purpose == TokenPurpose.EMBED
    assert usage.model == "embed-model"
    assert usage.item_id == 4
    assert usage.input_tokens == 12
    assert usage.output_tokens == 0
    assert usage.cost_usd == pytest.approx(12 * 2 / 1_000_000)


def test_unknown_model_price_is_zero():
    settings = _settings()
    assert token_cost_usd(settings, "missing-model", 100, 100) == 0.0


def _fetched_item(session) -> ContentItem:
    item = ContentItem(
        source_url="https://example.com/usage",
        source_type=SourceType.NEWS,
        title_original="Gyro",
        body_original="Body",
        title_ru="",
        status=ContentStatus.FETCHED,
        content_hash="usage-hash",
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def _client(session, handler) -> LLMClient:
    return LLMClient(
        session,
        settings=_settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_complete_commits_usage_and_leaves_pending_edit(session):
    item = _fetched_item(session)
    item.title_ru = "черновик"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("готово", 3, 4))

    assert _client(session, handler).complete("system", "user") == "готово"
    with session.no_autoflush:
        usage = session.exec(select(TokenUsage)).one()
        assert usage.input_tokens == 3
        assert usage.output_tokens == 4
        assert session.is_modified(item)

    session.commit()
    session.refresh(item)
    assert item.title_ru == "черновик"


def test_rollback_drops_pending_edit_and_keeps_token_usage(session):
    item = _fetched_item(session)
    item.title_ru = "черновик"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("готово", 3, 4))

    _client(session, handler).complete("system", "user")
    session.rollback()
    session.refresh(item)
    assert item.title_ru == ""
    usage = session.exec(select(TokenUsage)).one()
    assert usage.input_tokens == 3
    assert usage.output_tokens == 4


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {"content": None}}]},
    ],
)
def test_incomplete_response_is_not_retried(session, payload):
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=payload)

    with pytest.raises(LLMResponseError):
        _client(session, handler).complete("system", "user")
    assert calls["count"] == 1
    assert session.exec(select(TokenUsage)).all() == []


def test_empty_content_is_returned_and_recorded(session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("", 8, 1))

    assert _client(session, handler).complete("system", "user") == ""
    usage = session.exec(select(TokenUsage)).one()
    assert usage.input_tokens == 8
    assert usage.output_tokens == 1
