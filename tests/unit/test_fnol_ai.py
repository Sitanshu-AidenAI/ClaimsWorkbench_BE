"""What happens when the model misbehaves.

The provider layer's contract is that a caller gets a validated object or an
`AIProviderError` — never a half-parsed dictionary. These tests are the ones that
hold that line, because every downstream service is written on the assumption
that it holds.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.core.config import AISettings
from app.domain.enums import LineOfBusiness
from app.domain.extraction import ClassificationResult, FNOLExtraction
from app.services.ai.base import AIProviderError, pydantic_json_schema
from app.services.ai.factory import get_ai_provider, reset_ai_provider
from app.services.ai.openai_provider import OpenAIProvider
from app.services.fnol.classification import ClassificationService
from app.services.fnol.extraction import HEURISTIC_PROVIDER, FNOLExtractionService
from tests.fakes.ai import StubProvider

BASE_URL = "https://api.test/v1"

CLASSIFICATION = {
    "line_of_business": "property",
    "claim_type": None,
    "loss_type": "fire",
    "complexity": "standard",
    "confidence": 0.9,
    "reasoning": "The notification describes a warehouse fire.",
}


def config() -> AISettings:
    return AISettings(api_key="test-key", base_url=BASE_URL, model="test-model", max_attempts=1)


def completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        },
    )


class TestSchemaGeneration:
    def test_every_property_is_required_and_closed(self) -> None:
        # A strict structured-output endpoint rejects a schema with optional keys
        # or open objects, so the generator has to produce neither.
        schema = pydantic_json_schema(ClassificationResult)
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])

    def test_nested_objects_are_tightened_too(self) -> None:
        schema = pydantic_json_schema(FNOLExtraction)
        for definition in schema["$defs"].values():
            if definition.get("properties"):
                assert definition["additionalProperties"] is False
                assert set(definition["required"]) == set(definition["properties"])


class TestOpenAIProvider:
    async def test_the_request_omits_parameters_a_reasoning_model_rejects(
        self, respx_mock: respx.MockRouter
    ) -> None:
        # The GPT-5 family answers `max_tokens` and any explicit `temperature` with
        # a 400, so the wire payload — not just the model name — is the contract.
        route = respx_mock.post(f"{BASE_URL}/chat/completions").mock(
            return_value=completion(json.dumps(CLASSIFICATION))
        )
        provider = OpenAIProvider(config())
        await provider.structured(
            schema=ClassificationResult,
            system_prompt="s",
            user_prompt="u",
            schema_name="claim_classification",
        )
        sent = json.loads(route.calls.last.request.content)
        assert sent["max_completion_tokens"] == 4096
        assert "max_tokens" not in sent
        assert "temperature" not in sent
        assert "reasoning_effort" not in sent
        await provider.aclose()

    async def test_temperature_and_effort_are_sent_when_configured(
        self, respx_mock: respx.MockRouter
    ) -> None:
        # A gateway or a GPT-4-class model still honours both, so opting in has to
        # keep working.
        route = respx_mock.post(f"{BASE_URL}/chat/completions").mock(
            return_value=completion(json.dumps(CLASSIFICATION))
        )
        tuned = AISettings(
            api_key="test-key",
            base_url=BASE_URL,
            model="test-model",
            max_attempts=1,
            temperature=0.0,
            reasoning_effort="low",
        )
        provider = OpenAIProvider(tuned)
        await provider.structured(
            schema=ClassificationResult,
            system_prompt="s",
            user_prompt="u",
            schema_name="claim_classification",
        )
        sent = json.loads(route.calls.last.request.content)
        assert sent["temperature"] == 0.0
        assert sent["reasoning_effort"] == "low"
        await provider.aclose()

    async def test_a_valid_response_is_returned_as_a_typed_object(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.post(f"{BASE_URL}/chat/completions").mock(
            return_value=completion(json.dumps(CLASSIFICATION))
        )

        provider = OpenAIProvider(config())
        response = await provider.structured(
            schema=ClassificationResult,
            system_prompt="s",
            user_prompt="u",
            schema_name="claim_classification",
        )
        assert response.data.line_of_business == "property"
        assert response.model == "test-model"
        assert response.usage["prompt_tokens"] == 10
        await provider.aclose()

    async def test_a_non_json_response_is_an_error_not_a_guess(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.post(f"{BASE_URL}/chat/completions").mock(
            return_value=completion("Sure! Here is the classification:")
        )
        provider = OpenAIProvider(config())
        with pytest.raises(AIProviderError, match="not JSON"):
            await provider.structured(
                schema=ClassificationResult,
                system_prompt="s",
                user_prompt="u",
                schema_name="claim_classification",
            )
        await provider.aclose()

    async def test_a_response_missing_fields_is_refused(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.post(f"{BASE_URL}/chat/completions").mock(
            return_value=completion(json.dumps({"line_of_business": "property"}))
        )
        provider = OpenAIProvider(config())
        with pytest.raises(AIProviderError, match="did not match"):
            await provider.structured(
                schema=ClassificationResult,
                system_prompt="s",
                user_prompt="u",
                schema_name="claim_classification",
            )
        await provider.aclose()

    async def test_an_out_of_range_confidence_is_refused(
        self, respx_mock: respx.MockRouter
    ) -> None:
        payload = {
            "line_of_business": "property",
            "claim_type": None,
            "loss_type": "fire",
            "complexity": "standard",
            "confidence": 4.2,
            "reasoning": "Very sure.",
        }
        respx_mock.post(f"{BASE_URL}/chat/completions").mock(
            return_value=completion(json.dumps(payload))
        )
        provider = OpenAIProvider(config())
        with pytest.raises(AIProviderError):
            await provider.structured(
                schema=ClassificationResult,
                system_prompt="s",
                user_prompt="u",
                schema_name="claim_classification",
            )
        await provider.aclose()

    async def test_a_truncated_response_says_so(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.post(f"{BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '{"line_of_'}, "finish_reason": "length"}]
                },
            )
        )
        provider = OpenAIProvider(config())
        with pytest.raises(AIProviderError, match="truncated"):
            await provider.structured(
                schema=ClassificationResult,
                system_prompt="s",
                user_prompt="u",
                schema_name="claim_classification",
            )
        await provider.aclose()

    async def test_a_server_error_is_retryable(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.post(f"{BASE_URL}/chat/completions").mock(return_value=httpx.Response(500))
        provider = OpenAIProvider(config())
        with pytest.raises(AIProviderError) as caught:
            await provider.structured(
                schema=ClassificationResult,
                system_prompt="s",
                user_prompt="u",
                schema_name="claim_classification",
            )
        assert caught.value.retryable is True
        await provider.aclose()


class TestProviderSelection:
    def test_no_key_means_the_deterministic_path(self) -> None:
        from app.core.config import Settings

        reset_ai_provider()
        try:
            assert get_ai_provider(Settings(ai=AISettings(api_key=None))) is None
        finally:
            reset_ai_provider()

    def test_a_key_builds_the_openai_provider(self) -> None:
        from app.core.config import Settings

        reset_ai_provider()
        try:
            provider = get_ai_provider(Settings(ai=config()))
            assert isinstance(provider, OpenAIProvider)
            assert provider.model == "test-model"
        finally:
            reset_ai_provider()


class TestServiceFallback:
    async def test_extraction_falls_back_when_the_provider_fails(self) -> None:
        provider = StubProvider(error=AIProviderError("upstream down", retryable=True))
        service = FNOLExtractionService(repository=None, provider=provider)  # type: ignore[arg-type]

        outcome = await service.extract(
            source_text="Policy number: POL-2026-0041\nDate of loss: 03/08/2026",
            document_texts=[],
            channel="broker_email",
        )

        assert outcome.succeeded
        assert outcome.provider == HEURISTIC_PROVIDER
        assert outcome.error is not None and "deterministic" in outcome.error
        assert outcome.extraction is not None
        assert outcome.extraction.policy.policy_number.value == "POL-2026-0041"

    async def test_the_fingerprint_only_moves_when_the_inputs_do(self) -> None:
        first = FNOLExtractionService.fingerprint("body", ["doc one"])
        assert first == FNOLExtractionService.fingerprint("body", ["doc one"])
        assert first != FNOLExtractionService.fingerprint("body", ["doc one", "doc two"])
        assert first != FNOLExtractionService.fingerprint("body changed", ["doc one"])

    async def test_a_hallucinated_line_of_business_is_rejected(self) -> None:
        provider = StubProvider(
            result=ClassificationResult(
                line_of_business="Interstellar Freight",
                claim_type=None,
                loss_type="meteor",
                complexity="standard",
                confidence=0.98,
                reasoning="Confident.",
            )
        )
        service = ClassificationService(provider=provider)

        outcome = await service.classify(text="Fire at the insured warehouse in Leeds.")

        # The rejected answer is replaced by the deterministic reading, which can
        # only ever name a line the carrier actually writes.
        assert outcome.line_of_business.value in {line.value for line in LineOfBusiness}
        assert outcome.caveat is not None
        assert "Interstellar Freight" in outcome.caveat

    async def test_disagreement_with_the_keyword_reading_lowers_confidence(self) -> None:
        provider = StubProvider(
            result=ClassificationResult(
                line_of_business="marine",
                claim_type=None,
                loss_type=None,
                complexity="standard",
                confidence=0.95,
                reasoning="Looks marine to me.",
            )
        )
        service = ClassificationService(provider=provider)

        outcome = await service.classify(
            text=(
                "Fire damage to the insured warehouse premises in Leeds; the roof and "
                "racking are damaged and stock has been lost to smoke. Building is a "
                "retail unit with a sprinkler system."
            )
        )

        assert outcome.line_of_business.value == "marine"
        assert outcome.confidence <= 0.55
        assert outcome.caveat is not None and "property" in outcome.caveat
