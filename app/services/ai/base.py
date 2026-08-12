"""The provider contract.

One method, `structured`, because the only thing this application ever asks a
model for is a typed object. There is no free-text completion path on purpose:
prose that has to be parsed downstream is prose that will eventually be parsed
wrongly, and a claims record is not a place to find that out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


class AIProviderError(Exception):
    """The provider could not answer.

    Raised for transport failures, exhausted retries, and — importantly — for a
    response that did not validate against the requested schema. Callers treat all
    three the same way: the analysis is recorded as failed and the officer is told,
    rather than a partially-parsed object being written to the case.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(slots=True)
class AIResponse[T: BaseModel]:
    """A validated answer, plus what it cost and who gave it."""

    data: T
    provider: str
    model: str | None
    latency_ms: int
    #: Token counts where the provider reports them. Logged, never stored per-case.
    usage: dict[str, int] = field(default_factory=dict)


@runtime_checkable
class AIProvider(Protocol):
    """What every provider implementation offers."""

    name: str

    @property
    def model(self) -> str | None:
        """The model identifier to record against an analysis, if there is one."""

    async def structured[T: BaseModel](
        self,
        *,
        schema: type[T],
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        max_output_tokens: int | None = None,
    ) -> AIResponse[T]:
        """Return an instance of `schema`, or raise `AIProviderError`.

        Implementations must validate before returning. A caller is entitled to
        assume that a returned object satisfies every constraint on the model,
        including the domain-level validators — that assumption is the whole
        defence against a hallucinated policy number reaching the database.
        """

    async def aclose(self) -> None:
        """Release any transport resources."""


def pydantic_json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """A JSON Schema a strict structured-output API will accept.

    Providers that enforce strict mode reject schemas with optional keys, so every
    property is listed as required and nullability is expressed in the type
    itself. Pydantic emits `anyOf: [T, null]` for `T | None`, which is already the
    right shape; this only has to fix `required` and pin
    `additionalProperties: false` on every object, including the nested ones.
    """
    document = schema.model_json_schema()
    _tighten(document)
    for definition in document.get("$defs", {}).values():
        _tighten(definition)
    return document


def _tighten(node: dict[str, Any]) -> None:
    if node.get("type") != "object" and "properties" not in node:
        return
    properties = node.get("properties", {})
    node["additionalProperties"] = False
    node["required"] = list(properties.keys())
    for child in properties.values():
        if isinstance(child, dict):
            _tighten(child)
            items = child.get("items")
            if isinstance(items, dict):
                _tighten(items)
