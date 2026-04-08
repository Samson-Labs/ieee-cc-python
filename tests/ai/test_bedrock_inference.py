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
    ALL_FIELDS,
    BACKOFF_BASE,
    DEFAULT_MODEL_ID,
    MAX_RETRIES,
    MAX_TOKENS,
    MAX_TOOL_ITERATIONS,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_NO_TOOL,
    TEMPERATURE,
    TEXT_TRUNCATION_LIMIT,
    THESAURUS_TOOL,
    VALID_AUDIENCES,
    VALID_CATEGORIES,
    VALID_LEARNING_LEVELS,
    BedrockInference,
    _build_system_prompt,
)
from src.ai.thesaurus import ThesaurusSearch


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


def _bedrock_response(content: str, input_tokens: int = 100, output_tokens: int = 50) -> dict:
    """Create a mock Bedrock invoke_model response."""
    body_bytes = json.dumps({
        "content": [{"type": "text", "text": content}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
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
def empty_thesaurus(tmp_path):
    """Thesaurus with no data — disables tool use for legacy tests."""
    path = tmp_path / "empty.json"
    path.write_text('{"terms": []}')
    return ThesaurusSearch(data_path=str(path))


@pytest.fixture
def inference(bedrock_mock, empty_thesaurus):
    return BedrockInference(bedrock_client=bedrock_mock, thesaurus=empty_thesaurus)


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
        # Empty thesaurus → no-tool prompt (tool references not sent without thesaurus)
        assert body["system"] == SYSTEM_PROMPT_NO_TOOL
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

    def test_uses_env_model_id(self, bedrock_mock, empty_thesaurus):
        with patch.dict("os.environ", {"BEDROCK_MODEL_ID": "custom-model-v2"}):
            inf = BedrockInference(bedrock_client=bedrock_mock, thesaurus=empty_thesaurus)

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


# ---------------------------------------------------------------------------
# Tests: CloudWatch metrics
# ---------------------------------------------------------------------------


class TestCloudWatchMetrics:
    def test_publishes_token_metrics(self, bedrock_mock, empty_thesaurus):
        cw_mock = MagicMock()
        inference = BedrockInference(bedrock_client=bedrock_mock, cloudwatch_client=cw_mock, thesaurus=empty_thesaurus)
        metadata = _valid_metadata()
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata), input_tokens=500, output_tokens=200
        )

        result = inference.generate_metadata(text="text")

        assert result["input_tokens"] == 500
        assert result["output_tokens"] == 200
        cw_mock.put_metric_data.assert_called_once()
        metric_data = cw_mock.put_metric_data.call_args[1]["MetricData"]
        names = {m["MetricName"]: m for m in metric_data}
        assert names["bedrock-input-tokens"]["Value"] == 500
        assert names["bedrock-input-tokens"]["Unit"] == "Count"
        assert names["bedrock-output-tokens"]["Value"] == 200
        assert names["bedrock-output-tokens"]["Unit"] == "Count"

    def test_accumulates_tokens_on_json_retry(self, bedrock_mock, empty_thesaurus):
        cw_mock = MagicMock()
        inference = BedrockInference(bedrock_client=bedrock_mock, cloudwatch_client=cw_mock, thesaurus=empty_thesaurus)
        metadata = _valid_metadata()
        bedrock_mock.invoke_model.side_effect = [
            _bedrock_response("not json", input_tokens=100, output_tokens=50),
            _bedrock_response(json.dumps(metadata), input_tokens=100, output_tokens=50),
        ]

        result = inference.generate_metadata(text="text")

        assert result["input_tokens"] == 200
        assert result["output_tokens"] == 100

    def test_no_metrics_without_cloudwatch_client(self, inference, bedrock_mock):
        metadata = _valid_metadata()
        bedrock_mock.invoke_model.return_value = _bedrock_response(
            json.dumps(metadata)
        )

        result = inference.generate_metadata(text="text")

        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50


# ---------------------------------------------------------------------------
# Helpers: tool-use responses
# ---------------------------------------------------------------------------


def _tool_use_response(
    tool_calls: list[dict],
    text: str = "",
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> dict:
    """Create a mock Bedrock response that requests tool use."""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for tc in tool_calls:
        content.append({
            "type": "tool_use",
            "id": tc.get("id", "call_001"),
            "name": tc.get("name", "search_ieee_thesaurus"),
            "input": tc.get("input", {"query": "test"}),
        })
    body_bytes = json.dumps({
        "content": content,
        "stop_reason": "tool_use",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }).encode()
    return {"body": BytesIO(body_bytes)}


def _final_response(content: str, input_tokens: int = 100, output_tokens: int = 50) -> dict:
    """Create a mock Bedrock final response (end_turn)."""
    return _bedrock_response(content, input_tokens, output_tokens)


# ---------------------------------------------------------------------------
# Tests: tool use with thesaurus
# ---------------------------------------------------------------------------


class TestToolUse:
    @pytest.fixture
    def sample_thesaurus(self, tmp_path):
        data = {
            "terms": [
                {
                    "preferred_term": "Machine learning",
                    "scope_note": "A branch of AI",
                    "use_for": ["ML"],
                    "broader_terms": ["Artificial intelligence"],
                    "narrower_terms": [],
                    "related_terms": [],
                },
                {
                    "preferred_term": "Neural networks",
                    "scope_note": "",
                    "use_for": ["ANN"],
                    "broader_terms": [],
                    "narrower_terms": [],
                    "related_terms": [],
                },
                {
                    "preferred_term": "Economics",
                    "scope_note": "",
                    "use_for": [],
                    "broader_terms": [],
                    "narrower_terms": ["Macroeconomics"],
                    "related_terms": [],
                },
                {
                    "preferred_term": "Macroeconomics",
                    "scope_note": "",
                    "use_for": [],
                    "broader_terms": ["Economics"],
                    "narrower_terms": [],
                    "related_terms": [],
                },
            ],
        }
        path = tmp_path / "thesaurus.json"
        path.write_text(json.dumps(data))
        return ThesaurusSearch(data_path=str(path))

    @pytest.fixture
    def tool_inference(self, bedrock_mock, sample_thesaurus):
        return BedrockInference(bedrock_client=bedrock_mock, thesaurus=sample_thesaurus)

    def test_includes_tools_in_request(self, tool_inference, bedrock_mock):
        """When thesaurus is loaded, request should include tools."""
        metadata = _valid_metadata()
        # LLM responds directly without using tools
        bedrock_mock.invoke_model.return_value = _final_response(
            json.dumps(metadata)
        )

        tool_inference.generate_metadata(text="Some ML text")

        body = json.loads(bedrock_mock.invoke_model.call_args[1]["body"])
        assert "tools" in body
        assert body["tools"][0]["name"] == "search_ieee_thesaurus"

    def test_tool_use_loop(self, tool_inference, bedrock_mock):
        """LLM calls thesaurus tool, gets results, then produces final output."""
        metadata = _valid_metadata()

        bedrock_mock.invoke_model.side_effect = [
            # First call: LLM requests tool use
            _tool_use_response([{
                "id": "call_001",
                "name": "search_ieee_thesaurus",
                "input": {"query": "machine learning neural networks"},
            }]),
            # Second call: LLM produces final JSON
            _final_response(json.dumps(metadata)),
        ]

        result = tool_inference.generate_metadata(text="ML paper text")

        assert result["learning_level"] == "Professional"
        assert bedrock_mock.invoke_model.call_count == 2

        # Verify second call includes tool results in messages
        second_body = json.loads(bedrock_mock.invoke_model.call_args_list[1][1]["body"])
        messages = second_body["messages"]
        # Should have: user text, assistant tool_use, user tool_result
        assert len(messages) == 3
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[2]["role"] == "user"
        # Tool result should contain thesaurus search results
        tool_result_content = messages[2]["content"][0]["content"]
        assert "Machine learning" in tool_result_content

    def test_multiple_tool_calls(self, tool_inference, bedrock_mock):
        """LLM can make multiple tool calls in sequence."""
        metadata = _valid_metadata()

        bedrock_mock.invoke_model.side_effect = [
            # First iteration: two tool calls
            _tool_use_response([
                {"id": "call_001", "input": {"query": "machine learning"}},
                {"id": "call_002", "input": {"query": "economics"}},
            ]),
            # Second iteration: final response
            _final_response(json.dumps(metadata)),
        ]

        result = tool_inference.generate_metadata(text="ML economics text")
        assert result["learning_level"] == "Professional"
        assert bedrock_mock.invoke_model.call_count == 2

    def test_skips_tool_use_with_explicit_thesaurus_terms(
        self, tool_inference, bedrock_mock
    ):
        """When thesaurus_terms are passed explicitly, use legacy prompt path."""
        metadata = _valid_metadata()
        bedrock_mock.invoke_model.return_value = _final_response(
            json.dumps(metadata)
        )

        tool_inference.generate_metadata(
            text="text", thesaurus_terms=["smart grid", "power systems"]
        )

        body = json.loads(bedrock_mock.invoke_model.call_args[1]["body"])
        assert "tools" not in body
        assert "IEEE Thesaurus subset" in body["system"]

    def test_thesaurus_coverage_in_result(self, tool_inference, bedrock_mock):
        """Result should include thesaurus coverage count."""
        metadata = _valid_metadata()
        # Set some keywords that match our sample thesaurus
        metadata["keywords"] = [
            "Machine learning", "Neural networks", "Economics",
            "Monetary Policy", "Federal Funds Rate", "Inflation",
            "Deep learning", "Power grid", "Smart grid", "Energy storage",
        ]
        bedrock_mock.invoke_model.return_value = _final_response(
            json.dumps(metadata)
        )

        result = tool_inference.generate_metadata(text="text")

        # Machine learning, Neural networks, Economics match the sample thesaurus
        assert result["thesaurus_coverage"] == 3

    def test_zero_coverage_without_thesaurus(self, inference, bedrock_mock):
        """Without thesaurus loaded, coverage should be 0."""
        metadata = _valid_metadata()
        bedrock_mock.invoke_model.return_value = _final_response(
            json.dumps(metadata)
        )

        result = inference.generate_metadata(text="text")
        assert result["thesaurus_coverage"] == 0

    def test_max_iterations_safety(self, tool_inference, bedrock_mock):
        """Should stop after MAX_TOOL_ITERATIONS even if LLM keeps calling tools."""
        metadata = _valid_metadata()

        # All responses request tool use, except we need to handle the final one
        tool_responses = [
            _tool_use_response([{
                "id": f"call_{i:03d}",
                "input": {"query": f"search {i}"},
            }])
            for i in range(MAX_TOOL_ITERATIONS + 1)
        ]

        bedrock_mock.invoke_model.side_effect = tool_responses

        # Should not raise — extracts empty text, then JSON retry kicks in
        # which will also fail, raising ValueError
        with pytest.raises(ValueError, match="invalid JSON"):
            tool_inference.generate_metadata(text="text")
