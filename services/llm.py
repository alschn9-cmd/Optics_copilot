import logging
import time
from collections.abc import Callable

import httpx
from sqlmodel import Session

from app.models import TokenPurpose, TokenUsage
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class RetryableError(Exception):
    """Network failure or HTTP 429. The client retries these."""


class LLMResponseError(RuntimeError):
    """The provider returned a body that is not a chat completion."""


class LLMClient:
    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        http_client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
        initial_delay: float = 0.5,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self._http = http_client
        self._sleeper = sleeper
        self._max_attempts = max_attempts
        self._initial_delay = initial_delay

    def complete(
        self,
        system: str,
        user: str,
        model: str | None = None,
        *,
        item_id: int | None = None,
        purpose: TokenPurpose = TokenPurpose.CHAT,
    ) -> str:
        chosen = model or self.settings.llm_primary_model
        delay = self._initial_delay
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                content, input_tokens, output_tokens = self._request(system, user, chosen)
            except (httpx.RequestError, RetryableError) as exc:
                last_error = exc
                if attempt == self._max_attempts:
                    raise
                logger.warning("LLM attempt %s failed: %s", attempt, exc)
                self._sleeper(delay)
                delay *= 2
                continue
            self._record_usage(chosen, input_tokens, output_tokens, item_id, purpose)
            return content
        raise last_error or RuntimeError("LLM request failed")

    def _request(self, system: str, user: str, model: str) -> tuple[str, int, int]:
        url = f"{self.settings.llm_base_url.rstrip('/')}/chat/completions"
        response = self._client().post(
            url,
            headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        if response.status_code == 429:
            raise RetryableError(f"429 from {model}")
        response.raise_for_status()
        payload = response.json()
        content = _completion_text(payload)
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            usage = {}
        return (
            content,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )

    def _record_usage(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        item_id: int | None,
        purpose: TokenPurpose,
    ) -> None:
        usage = TokenUsage(
            item_id=item_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=token_cost_usd(self.settings, model, input_tokens, output_tokens),
            purpose=purpose,
        )
        _commit_usage_only(self.session, usage)

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=60.0)
        return self._http


def _completion_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise LLMResponseError("LLM response is not a JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LLMResponseError("LLM response does not contain choices")
    message = choices[0].get("message")
    if not isinstance(message, dict) or "content" not in message:
        raise LLMResponseError("LLM response does not contain a message")
    content = message.get("content")
    if content is None:
        raise LLMResponseError("LLM response content is empty")
    if not isinstance(content, str):
        raise LLMResponseError("LLM response content is not text")
    return content


def _commit_usage_only(session: Session, usage: TokenUsage) -> None:
    """Commit the usage row without committing other pending work on this session."""
    new_objects = list(session.new)
    deleted_objects = list(session.deleted)
    deleted_ids = {id(obj) for obj in deleted_objects}
    new_ids = {id(obj) for obj in new_objects}
    persistent = [
        obj
        for obj in list(session.identity_map.values())
        if id(obj) not in deleted_ids and id(obj) not in new_ids
    ]
    try:
        for obj in (*new_objects, *deleted_objects, *persistent):
            session.expunge(obj)
        session.add(usage)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        for obj in new_objects:
            session.add(obj)
        for obj in persistent:
            session.add(obj)
        for obj in deleted_objects:
            session.delete(obj)


def token_cost_usd(settings: Settings, model: str, input_tokens: int, output_tokens: int) -> float:
    rates = settings.llm_price_per_million.get(model)
    if rates is None:
        logger.warning("no price for model %s", model)
        return 0.0
    return (
        input_tokens * float(rates["input"]) + output_tokens * float(rates["output"])
    ) / 1_000_000
