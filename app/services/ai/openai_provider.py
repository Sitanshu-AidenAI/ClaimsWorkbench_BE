"""OpenAI, over the chat completions API with strict structured outputs.

Built on `HttpClient` so it inherits the project's retry policy rather than
growing its own. The key is read from settings and never logged; prompts are
never logged either — they contain claim documents, which is exactly the material
an observability pipeline must not hold.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import AISettings, settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.integrations.http import HttpClient
from app.services.ai.base import AIProviderError, AIResponse, pydantic_json_schema

logger = get_logger(__name__)


class OpenAIProvider:
    """A structured-output client for OpenAI-compatible endpoints.

    "OpenAI-compatible" is doing real work in that sentence: `base_url` is
    configuration, so Azure OpenAI or any gateway speaking the same wire format is
    a settings change rather than a new class.
    """

    name = "openai"

    def __init__(self, config: AISettings | None = None) -> None:
        self._config = config or settings.ai
        if not self._config.api_key:
            raise ValueError("OpenAIProvider requires an API key.")
        self._client = HttpClient(
            self._config.base_url,
            timeout=httpx.Timeout(self._config.timeout_seconds, connect=10.0),
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
            max_attempts=self._config.max_attempts,
        )

    @property
    def model(self) -> str | None:
        return self._config.model

    async def aclose(self) -> None:
        await self._client.aclose()

    async def structured[T: BaseModel](
        self,
        *,
        schema: type[T],
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        max_output_tokens: int | None = None,
    ) -> AIResponse[T]:
        payload: dict[str, Any] = {
            "model": self._config.model,
            # `max_completion_tokens` rather than `max_tokens`: the GPT-5 family
            # rejects the older name, and GPT-4-class models accept the newer one,
            # so there is nothing to branch on.
            "max_completion_tokens": max_output_tokens or self._config.max_output_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": pydantic_json_schema(schema),
                },
            },
        }
        # Both are omitted unless configured. A reasoning model rejects any
        # temperature but its own default, so sending the key at all is what breaks
        # the call — not the value.
        if self._config.temperature is not None:
            payload["temperature"] = self._config.temperature
        if self._config.reasoning_effort is not None:
            payload["reasoning_effort"] = self._config.reasoning_effort

        started = time.perf_counter()
        try:
            response = await self._client.post("/chat/completions", json=payload)
        except ExternalServiceError as exc:
            # Logged without the prompt: the body is claim material.
            logger.error("ai_request_failed", provider=self.name, model=self._config.model)
            raise AIProviderError(str(exc), retryable=True) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code >= 400:
            logger.error(
                "ai_response_rejected",
                provider=self.name,
                status_code=response.status_code,
            )
            raise AIProviderError(
                f"The model provider returned {response.status_code}.",
                retryable=response.status_code >= 500,
            )

        content = _first_message_content(response)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            logger.warning("ai_response_not_json", provider=self.name)
            raise AIProviderError("The model returned a response that was not JSON.") from exc

        try:
            data = schema.model_validate(parsed)
        except ValidationError as exc:
            # The count, not the content: a validation message can echo the value
            # that failed, which may be claim data.
            logger.warning(
                "ai_response_schema_mismatch",
                provider=self.name,
                schema=schema_name,
                error_count=len(exc.errors()),
            )
            raise AIProviderError(
                f"The model's response did not match the {schema_name} schema."
            ) from exc

        usage = response.json().get("usage") or {}
        logger.info(
            "ai_request_completed",
            provider=self.name,
            model=self._config.model,
            schema=schema_name,
            latency_ms=latency_ms,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

        return AIResponse(
            data=data,
            provider=self.name,
            model=self._config.model,
            latency_ms=latency_ms,
            usage={key: int(value) for key, value in usage.items() if isinstance(value, int)},
        )


def _first_message_content(response: httpx.Response) -> str:
    try:
        body = response.json()
        choice = body["choices"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise AIProviderError("The model provider returned an unrecognised envelope.") from exc

    if choice.get("finish_reason") == "length":
        # Truncated JSON parses as invalid rather than as partial data, but saying
        # so plainly is the difference between a fixable bug report and a mystery.
        raise AIProviderError("The model's response was truncated before it completed.")

    content = (choice.get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise AIProviderError("The model returned an empty response.")
    return content
