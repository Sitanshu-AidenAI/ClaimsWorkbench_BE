"""Provider selection.

Returns `None` when no key is configured, and the callers treat that as a
first-class state rather than an error: the FNOL services fall back to the
deterministic readers in `app.domain.heuristics`, which produce the same shapes
with lower confidence and a `heuristic` provider stamp.

That fallback is not a mock. It is a real, tested implementation that a carrier
would run behind an outage — and it is the reason the intake queue keeps working
when the model provider does not.
"""

from __future__ import annotations

from app.core.config import Settings, settings
from app.core.logging import get_logger
from app.services.ai.base import AIProvider
from app.services.ai.openai_provider import OpenAIProvider

logger = get_logger(__name__)


#: The process-wide provider. `_UNSET` distinguishes "not built yet" from the
#: legitimate answer `None`, which means the deterministic path.
_UNSET: object = object()
_provider: AIProvider | object | None = _UNSET


def get_ai_provider(config: Settings | None = None) -> AIProvider | None:
    """The configured provider, or `None` when the module runs deterministically.

    Built once per process, because the provider owns a connection pool. Not
    memoised on the argument: `Settings` is a mutable model and using it as a
    cache key would be both wrong and unhashable.
    """
    global _provider
    if _provider is _UNSET:
        config = config or settings
        if not config.ai.configured:
            logger.info(
                "ai_provider_unconfigured",
                reason="no_api_key" if config.ai.enabled else "disabled",
            )
            _provider = None
        else:
            logger.info("ai_provider_configured", provider="openai", model=config.ai.model)
            _provider = OpenAIProvider(config.ai)

    return _provider  # type: ignore[return-value]


def reset_ai_provider() -> None:
    """Forget the built provider. Used by tests and by configuration reloads."""
    global _provider
    _provider = _UNSET


async def close_ai_provider() -> None:
    """Close the built provider, if there is one."""
    global _provider
    if _provider is not _UNSET and _provider is not None:
        await _provider.aclose()  # type: ignore[union-attr]
    reset_ai_provider()
