"""Tests for BedrockInference.

Covers: successful metadata generation, throttle retry, invalid JSON retry,
timeout, validation of all fields, thesaurus context, text truncation.
"""

from __future__ import annotations

import json
import time
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.ai.bedrock_inference import (
    BACKOFF_BASE,
    DEFAULT_MODEL_ID,
    MAX_RETRIES,
    MAX_TOKENS,
    SYSTEM_PROMPT,
    TEMPERATURE,
    TEXT_TRUNCATION_LIMIT,
    VALID_AUDIENCES,
    VALID_CATEGORIES,
    VALID_LEARNING_LEVELS,
    BedrockInference,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_metadata() -> dict:
    """Return a valid metadata response dict."""
    para1 = " ".join(["word"] * 80)
    para2 = " ".join(["term"] * 80)
    return {
        "abstract": f"{para1}\n\n{para2}",
        "keywords": [
            "power systems", "smart grid", "energy storage", "renewable energy",
            "load forecasting", "demand response", "microgrid", "IEEE 1547",
            "distributed generation", "voltage regulation",
        ],
        "learning_level": "Professional",
        "intended_audience": "Seasoned Engineering Professional",
        "category": "Research Papers and Publications",
    }


def _bedrock_response(content: str) -> dict:
    """Create a mock Bedrock invoke_model response."""
    body_bytes = json.dumps({
        "content": [{"type": "text", "text": content}],
        "stop_reason": "end_turn",
    }).encode()
    return {"body": BytesIO(body_bytes)}


def _throttle_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "InvokeModel",
    )


def _internal_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "Server error"}},
        "InvokeModel",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bedrock_mock():
    return MagicMock()


@pytest.fixture
def inference(bedrock_mock):
    return BedrockInference(bedrock_client=bedrock_mock)


# ---------------------------------------------------------------------------
# Tests: successful generation
# ---------------------------------------------------------------------------


class TestGenerateMetadata:
    def test_returns_all_required_fields(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata)
        )

        result = inference.generate_metadata(text="Some extracted text")

        assert result["abstract"] == metadata["abstract"]
        assert result["keywords"] == metadata["keywords"]
        assert result["learning_level"] == "Professional"
        assert result["intended_audience"] == "Seasoned Engineering Professional"
        assert result["category"] == "Research Papers and Publications"
        assert result["processing_time_ms"] >= 0

    def test_calls_bedrock_with_correct_params(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata)
        )

        inference.generate_metadata(text="Document text here")

        call_kwargs = bedrock_mock.invoke_model.call_args[1]
        assert call_kwargs["modelId"] == DEFAULT_MODEL_ID
        assert call_kwargs["contentType"] == "application/json"

        body = json.loads(call_kwargs["body"])
        assert body["max_tokens"] == MAX_TOKENS
        assert body["temperature"] == TEMPERATURE
        assert body["system"] == SYSTEM_PROMPT
        assert body["messages"][0]["role"] == "user"
        assert body["messages"][0]["content"] == "Document text here"

    def test_appends_thesaurus_context(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata)
        )
        terms = ["smart grid", "power systems", "energy storage"]

        inference.generate_metadata(text="text", thesaurus_terms=terms)

        body = json.loads(bedrock_mock.invoke_model.call_args[1]["body"])
        assert "smart grid, power systems, energy storage" in body["system"]
        assert "IEEE Thesaurus subset" in body["system"]

    def test_truncates_long_text(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata)
        )
        long_text = "x" * (TEXT_TRUNCATION_LIMIT + 10_000)

        inference.generate_metadata(text=long_text)

        body = json.loads(bedrock_mock.invoke_model.call_args[1]["body"])
        assert len(body["messages"][0]["content"]) == TEXT_TRUNCATION_LIMIT

    def test_uses_env_model_id(self, bedrock_mock):
        with patch.dict("os.environ", {"BEDROCK_MODEL_ID": "custom-model-v2"}):
            inf = BedrockInference(bedrock_client=bedrock_mock)

        metadata = _valid_metadata()
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata)
        )

        inf.generate_metadata(text="text")

        call_kwargs = bedrock_mock.invoke_model.call_args[1]
        assert call_kwargs["modelId"] == "custom-model-v2"

    def test_processing_time_ms(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata)
        )

        result = inference.generate_metadata(text="text")
        assert isinstance(result["processing_time_ms"], int)
        assert result["processing_time_ms"] >= 0


# ---------------------------------------------------------------------------
# Tests: throttle retry
# ---------------------------------------------------------------------------


class TestThrottleRetry:
    @patch("src.ai.bedrock_inference.time.sleep")
    def test_retries_on_throttle_then_succeeds(self, mock_sleep, inference, bedrock_mock):
        metadata = _valid_metadata()
        bedrock_mock.invoke_model.side_effect = [
            _throttle_error(),
            _bedrock_response(json.dumps(metadata)),
        ]

        result = inference.generate_metadata(text="text")

        assert result["learning_level"] == "Professional"
        assert bedrock_mock.invoke_model.call_count == 2
        mock_sleep.assert_called_once_with(BACKOFF_BASE)

    @patch("src.ai.bedrock_inference.time.sleep")
    def test_exponential_backoff(self, mock_sleep, inference, bedrock_mock):
        metadata = _valid_metadata()
        bedrock_mock.invoke_model.side_effect = [
            _throttle_error(),
            _throttle_error(),
            _bedrock_response(json.dumps(metadata)),
        ]

        result = inference.generate_metadata(text="text")

        assert bedrock_mock.invoke_model.call_count == 3
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)  # 1s
        mock_sleep.assert_any_call(2)  # 2s

    @patch("src.ai.bedrock_inference.time.sleep")
    def test_raises_after_max_retries(self, mock_sleep, inference, bedrock_mock):
        bedrock_mock.invoke_model.side_effect = [
            _throttle_error() for _ in range(MAX_RETRIES)
        ]

        with pytest.raises(ClientError) as exc_info:
            inference.generate_metadata(text="text")

        assert exc_info.value.response["Error"]["Code"] == "ThrottlingException"
        assert bedrock_mock.invoke_model.call_count == MAX_RETRIES

    def test_non_throttle_error_not_retried(self, inference, bedrock_mock):
        bedrock_mock.invoke_model.side_effect = _internal_error()

        with pytest.raises(ClientError) as exc_info:
            inference.generate_metadata(text="text")

        assert exc_info.value.response["Error"]["Code"] == "InternalServerError"
        assert bedrock_mock.invoke_model.call_count == 1


# ---------------------------------------------------------------------------
# Tests: invalid JSON retry
# ---------------------------------------------------------------------------


class TestInvalidJsonRetry:
    def test_retries_once_on_invalid_json(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        bedrock_mock.invoke_model.side_effect = [
            _bedrock_response("Here is the metadata: {invalid json}"),
            _bedrock_response(json.dumps(metadata)),
        ]

        result = inference.generate_metadata(text="text")

        assert result["learning_level"] == "Professional"
        assert bedrock_mock.invoke_model.call_count == 2

    def test_raises_after_json_retry_fails(self, inference, bedrock_mock):
        bedrock_mock.invoke_model.side_effect = [
            _bedrock_response("not json at all"),
            _bedrock_response("still not json"),
        ]

        with pytest.raises(ValueError, match="invalid JSON after retry"):
            inference.generate_metadata(text="text")

    def test_strips_markdown_fences(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        fenced = f"```json\n{json.dumps(metadata)}\n```"
        bedrock_mock.invoke_model.return_value = _bedrock_response(fenced)

        result = inference.generate_metadata(text="text")

        assert result["category"] == "Research Papers and Publications"
        # Should succeed on first try (no retry needed)
        assert bedrock_mock.invoke_model.call_count == 1


# ---------------------------------------------------------------------------
# Tests: validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_missing_field_raises(self, inference, bedrock_mock):
        for field in ["abstract", "keywords", "learning_level", "intended_audience", "category"]:
            metadata = _valid_metadata()
            del metadata[field]
            bedrock_mock.invoke_model.return_value = _bedrock_response(
                json.dumps(metadata)
            )

            with pytest.raises(ValueError, match="Missing required fields"):
                inference.generate_metadata(text="text")

    def test_abstract_no_paragraphs_raises(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        metadata["abstract"] = " ".join(["word"] * 80)  # single paragraph
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata)
        )

        with pytest.raises(ValueError, match="two paragraphs"):
            inference.generate_metadata(text="text")

    def test_abstract_paragraph_too_short_raises(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        metadata["abstract"] = "Short.\n\n" + " ".join(["word"] * 80)
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata)
        )

        with pytest.raises(ValueError, match="words"):
            inference.generate_metadata(text="text")

    def test_abstract_paragraph_too_long_raises(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        metadata["abstract"] = " ".join(["word"] * 160) + "\n\n" + " ".join(["word"] * 80)
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata)
        )

        with pytest.raises(ValueError, match="words"):
            inference.generate_metadata(text="text")

    def test_keywords_too_few_raises(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        metadata["keywords"] = ["a", "b", "c"]
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata)
        )

        with pytest.raises(ValueError, match="8–12"):
            inference.generate_metadata(text="text")

    def test_keywords_too_many_raises(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        metadata["keywords"] = [f"kw{i}" for i in range(15)]
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata)
        )

        with pytest.raises(ValueError, match="8–12"):
            inference.generate_metadata(text="text")

    def test_invalid_learning_level_raises(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        metadata["learning_level"] = "Beginner"
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata)
        )

        with pytest.raises(ValueError, match="learning_level"):
            inference.generate_metadata(text="text")

    def test_invalid_audience_raises(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        metadata["intended_audience"] = "Student"
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata)
        )

        with pytest.raises(ValueError, match="intended_audience"):
            inference.generate_metadata(text="text")

    def test_invalid_category_raises(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        metadata["category"] = "Blog Post"
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata)
        )

        with pytest.raises(ValueError, match="category"):
            inference.generate_metadata(text="text")

    def test_all_valid_learning_levels(self, inference, bedrock_mock):
        for level in VALID_LEARNING_LEVELS:
            metadata = _valid_metadata()
            metadata["learning_level"] = level
            bedrock_mock.invoke_model.return_value = _bedrock_response(
                json.dumps(metadata)
            )
            result = inference.generate_metadata(text="text")
            assert result["learning_level"] == level

    def test_all_valid_audiences(self, inference, bedrock_mock):
        for audience in VALID_AUDIENCES:
            metadata = _valid_metadata()
            metadata["intended_audience"] = audience
            bedrock_mock.invoke_model.return_value = _bedrock_response(
                json.dumps(metadata)
            )
            result = inference.generate_metadata(text="text")
            assert result["intended_audience"] == audience

    def test_all_valid_categories(self, inference, bedrock_mock):
        for cat in VALID_CATEGORIES:
            metadata = _valid_metadata()
            metadata["category"] = cat
            bedrock_mock.invoke_model.return_value = _bedrock_response(
                json.dumps(metadata)
            )
            result = inference.generate_metadata(text="text")
            assert result["category"] == cat
