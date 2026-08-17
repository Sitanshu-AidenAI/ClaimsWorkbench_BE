"""A stub LLM provider.

Moved out of `tests/unit/test_fnol_ai.py` so the extraction, citation and API tests
import it rather than each growing their own copy — three copies of a test double is
three chances for one of them to drift into agreeing with the code instead of checking
it.

`prompt_seen` is the addition over the original. Several of the properties that matter
most are properties of the *prompt*: that retrieval narrowed it, that passages arrived
with labels, that the notification body was sent whole. Those are only assertable if the
double keeps what it was asked.
"""

from __future__ import annotations

from typing import Any

from app.services.ai.base import AIResponse


class StubProvider:
    """A provider that answers with whatever the test hands it."""

    name = "stub"
    model = "stub-model"

    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls = 0
        #: The last system and user prompt, so a test can assert on what was asked.
        self.system_prompt: str | None = None
        self.user_prompt: str | None = None

    async def structured(self, **kwargs: Any) -> object:
        self.calls += 1
        self.system_prompt = kwargs.get("system_prompt")
        self.user_prompt = kwargs.get("user_prompt")
        if self._error is not None:
            raise self._error
        return AIResponse(data=self._result, provider=self.name, model=self.model, latency_ms=1)

    async def aclose(self) -> None:
        return None

    @property
    def prompt_seen(self) -> str:
        """The user prompt, or `""` if nothing was asked yet."""
        return self.user_prompt or ""


__all__ = ["StubProvider"]
