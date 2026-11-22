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
        content = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage") or {}
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
        self.session.add(usage)
        self.session.commit()

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=60.0)
        return self._http


def token_cost_usd(settings: Settings, model: str, input_tokens: int, output_tokens: int) -> float:
    rates = settings.llm_price_per_million.get(model)
    if rates is None:
        logger.warning("no price for model %s", model)
        return 0.0
    return (
        input_tokens * float(rates["input"]) + output_tokens * float(rates["output"])
    ) / 1_000_000
