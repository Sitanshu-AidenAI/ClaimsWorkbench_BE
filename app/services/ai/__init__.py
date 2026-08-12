"""The AI provider layer.

Strictly separated from FNOL domain logic. A provider knows how to turn a prompt
and a JSON schema into a validated object; it knows nothing about policies,
losses or claims. `app.services.fnol.*` knows about those and knows nothing about
HTTP, tokens or retries.

That line is what makes the provider replaceable, and it is also what makes the
fallback honest: when no key is configured, `HeuristicProvider` answers the same
interface with rule-based parsing, and every record it produces is stamped with
`provider="heuristic"` so nothing downstream — or on screen — can mistake it for
a model's read.
"""

from __future__ import annotations

from app.services.ai.base import AIProvider, AIProviderError, AIResponse
from app.services.ai.factory import get_ai_provider
from app.services.ai.openai_provider import OpenAIProvider

__all__ = [
    "AIProvider",
    "AIProviderError",
    "AIResponse",
    "OpenAIProvider",
    "get_ai_provider",
]
