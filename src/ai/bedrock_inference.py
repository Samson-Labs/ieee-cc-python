"""Bedrock Claude Integration for IEEE metadata generation.

Sends extracted document text to AWS Bedrock (Claude Sonnet) with the IEEE
Technical Metadata Specialist system prompt (v1.2) and returns structured
metadata: abstract, keywords, learning_level, intended_audience, category.

Supports IEEE Thesaurus grounding via Bedrock tool use: the LLM can search
the thesaurus during generation to select standardized IEEE terms as keywords.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import TypedDict

import boto3
from botocore.exceptions import ClientError

from src.ai.thesaurus import ThesaurusSearch
from src.common.metrics import publish_metrics

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
MAX_TOKENS = 2048
TEMPERATURE = 0.3
TEXT_TRUNCATION_LIMIT = 180_000  # characters — fits within Claude's context window

# Retry configuration for Bedrock throttling (429)
MAX_RETRIES = 3
BACKOFF_BASE = 1  # seconds: 1, 2, 4

# Tool-use conversation loop limit
MAX_TOOL_ITERATIONS = 5
MIN_THESAURUS_KEYWORDS = 8

VALID_LEARNING_LEVELS = frozenset([
    "Foundational",
    "Professional",
    "Expert",
])

VALID_AUDIENCES = frozenset([
    "Non-Engineer",
    "Engineering Adjacent Professional",
    "New Engineer",
    "Seasoned Engineering Professional",
])

VALID_CATEGORIES = frozenset([
    "Research Papers and Publications",
    "Professional Development",
    "Society Outreach",
    "Technical Tutorial",
])

THESAURUS_TOOL = {
    "name": "search_ieee_thesaurus",
    "description": (
        "Search the IEEE Thesaurus for official standardized terms related to a "
        "topic. Use this to find IEEE taxonomy terms for the content's topics "
        "before selecting keywords. Make 2-3 searches covering different topic "
        "areas in the content."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Topic or concept to search for (e.g., "
                    "'machine learning neural networks', "
                    "'power grid renewable energy')"
                ),
            }
        },
        "required": ["query"],
    },
}

_PROMPT_PREAMBLE = (
    "You are a Technical Metadata Specialist for the IEEE (Institute of Electrical "
    "and Electronics Engineers). Your role is to analyze technical content and "
    "generate structured metadata that helps categorize, discover, and recommend "
    "IEEE content to the right audiences.\n\n"
    "The text you receive may be a transcript of a video presentation, webinar, or "
    "tutorial, or text extracted from a PDF document. Never refer to the source as "
    'a "document", "transcript", or "text" in your output. Describe the content '
    "itself — the presentation, webinar, tutorial, paper, or research — as if the "
    "reader will consume the original media.\n\n"
    "Given the extracted text, generate a JSON object with the following fields:\n\n"
)

_KEYWORDS_WITH_TOOL = (
    '**keywords** — An array of 8–12 keyword strings. Before selecting keywords, '
    "use the search_ieee_thesaurus tool to find standardized IEEE terms for the "
    "content's main topics (make 2-3 searches covering different topic areas). "
    "When using an IEEE Thesaurus term, copy the exact preferred_term string from the "
    "tool result — do not change capitalization, punctuation, pluralization, or wording "
    '(e.g., use "Deep learning" not "Deep Learning", "Rectennas" not "Rectenna", '
    '"DC-DC power converters" not "DC DC Converter"). '
    "Strongly prefer IEEE Thesaurus terms. If the content covers topics not "
    "well-represented in the IEEE Thesaurus, you may include a small number of "
    "specific non-thesaurus terms, but thesaurus terms should be the majority.\n\n"
)

_KEYWORDS_NO_TOOL = (
    '**keywords** — An array of 8–12 keyword strings that capture the content\'s '
    "core topics, technologies, methodologies, and application domains. Prefer specific "
    "technical terms over generic ones. When a relevant IEEE Thesaurus term exists, "
    "prefer it over a synonym.\n\n"
)

ALL_FIELDS = frozenset({"abstract", "keywords", "learning_level", "intended_audience", "category"})

# Per-field instruction blocks (without numbering — assembled dynamically)
_FIELD_INSTRUCTIONS = {
    "abstract": (
        '**abstract** — A two-paragraph summary of the content. Each paragraph '
        "should be 50–150 words. Separate the two paragraphs with a blank line (\\n\\n). "
        "The first paragraph should describe the main topic, approach, and scope of the "
        "presentation or publication. "
        "The second paragraph should cover key findings, contributions, and implications.\n\n"
    ),
    "learning_level": (
        '**learning_level** — One of the following:\n'
        '   - "Foundational" — introductory material suitable for students or newcomers\n'
        '   - "Professional" — intermediate material for practicing engineers\n'
        '   - "Expert" — advanced material requiring deep domain expertise\n\n'
    ),
    "intended_audience": (
        '**intended_audience** — One of the following:\n'
        '   - "Non-Engineer" — general public, managers, or policy-makers\n'
        '   - "Engineering Adjacent Professional" — technical writers, project managers\n'
        '   - "New Engineer" — early-career engineers, recent graduates\n'
        '   - "Seasoned Engineering Professional" — experienced engineers, researchers\n\n'
    ),
    "category": (
        '**category** — One of the following:\n'
        '   - "Research Papers and Publications" — original research, conference papers\n'
        '   - "Professional Development" — tutorials, courses, certification material\n'
        '   - "Society Outreach" — newsletters, community reports, event summaries\n'
        '   - "Technical Tutorial" — how-to guides, implementation walkthroughs\n\n'
    ),
}

_PROMPT_RETURN_INSTRUCTION = (
    "Return ONLY a valid JSON object with these fields. Do not include any text "
    "before or after the JSON. Do not wrap it in markdown code fences."
)


def _build_system_prompt(
    requested_fields: frozenset[str],
    use_tool: bool = True,
) -> str:
    """Assemble system prompt with only the requested field instructions.

    Fields are numbered sequentially (1, 2, 3...) regardless of which are
    included. Each field instruction (from _FIELD_INSTRUCTIONS or keyword
    constants) is prefixed with its number.
    """
    parts = [_PROMPT_PREAMBLE]

    num = 1
    if "abstract" in requested_fields:
        parts.append(f"{num}. {_FIELD_INSTRUCTIONS['abstract']}")
        num += 1

    if "keywords" in requested_fields:
        kw_block = _KEYWORDS_WITH_TOOL if use_tool else _KEYWORDS_NO_TOOL
        parts.append(f"{num}. {kw_block}")
        num += 1

    for field in ["learning_level", "intended_audience", "category"]:
        if field in requested_fields:
            parts.append(f"{num}. {_FIELD_INSTRUCTIONS[field]}")
            num += 1

    parts.append(_PROMPT_RETURN_INSTRUCTION)
    return "".join(parts)


# Default prompts for backward compatibility
SYSTEM_PROMPT = _build_system_prompt(ALL_FIELDS, use_tool=True)
SYSTEM_PROMPT_NO_TOOL = _build_system_prompt(ALL_FIELDS, use_tool=False)

JSON_RETRY_SUFFIX = (
    "\n\nIMPORTANT: Your previous response was not valid JSON. "
    "Return ONLY a raw JSON object — no markdown, no explanation, no text outside "
    "the JSON braces."
)


class _InferenceMetrics(TypedDict):
    """Always-present metric fields in inference results."""
    processing_time_ms: int
    input_tokens: int
    output_tokens: int
    thesaurus_coverage: int


class InferenceResult(_InferenceMetrics, total=False):
    """Inference result — metadata fields are optional when requested_fields is used."""
    abstract: str
    keywords: list[str]
    learning_level: str
    intended_audience: str
    category: str


class BedrockInference:
    """Calls AWS Bedrock Claude to generate structured metadata from document text."""

    def __init__(
        self,
        bedrock_client=None,
        model_id: str | None = None,
        cloudwatch_client=None,
        thesaurus: ThesaurusSearch | None = None,
    ):
        self._bedrock = bedrock_client or boto3.client(
            "bedrock-runtime", region_name="us-east-1"
        )
        self._model_id = model_id or os.environ.get(
            "BEDROCK_MODEL_ID", DEFAULT_MODEL_ID
        )
        self._cloudwatch = cloudwatch_client
        self._thesaurus = thesaurus if thesaurus is not None else ThesaurusSearch()

    def generate_metadata(
        self,
        text: str,
        thesaurus_terms: list[str] | None = None,
        requested_fields: frozenset[str] | None = None,
    ) -> InferenceResult:
        """Generate structured metadata from extracted document text.

        When the IEEE Thesaurus is loaded, uses Bedrock tool use so the LLM
        can search the thesaurus during keyword selection. Falls back to the
        original single-request path when the thesaurus is unavailable or
        when explicit thesaurus_terms are provided.

        Args:
            text: Extracted document text (will be truncated if too long).
            thesaurus_terms: Optional IEEE Thesaurus terms to prioritize for
                keywords. When provided, uses the legacy prompt-injection
                approach instead of tool use.
            requested_fields: Optional subset of ALL_FIELDS to generate. When
                None, all 5 fields are generated (backward compatible).

        Returns:
            InferenceResult with abstract, keywords, learning_level,
            intended_audience, category, processing_time_ms,
            input_tokens, output_tokens, and thesaurus_coverage.

        Raises:
            ValueError: If Bedrock response fails validation after retries.
            ClientError: If Bedrock API call fails (non-throttle errors).
        """
        start = time.monotonic()

        effective_fields = requested_fields or ALL_FIELDS
        truncated_text = text[:TEXT_TRUNCATION_LIMIT]
        use_tool = (
            self._thesaurus.term_count > 0
            and not thesaurus_terms
            and "keywords" in effective_fields
        )

        system_prompt = _build_system_prompt(effective_fields, use_tool=use_tool)

        if use_tool:
            raw, input_tokens, output_tokens = self._invoke_with_tools(
                system_prompt, truncated_text
            )
        else:
            if thesaurus_terms and "keywords" in effective_fields:
                terms_str = ", ".join(thesaurus_terms)
                system_prompt += (
                    f"\n\nWhen selecting keywords, prioritize terms from this "
                    f"IEEE Thesaurus subset: {terms_str}"
                )
            raw, input_tokens, output_tokens = self._invoke(
                system_prompt, truncated_text
            )

        parsed = self._try_parse_json(raw)

        # If JSON parsing failed, retry once with explicit JSON instruction.
        # Use the no-tool prompt to avoid the LLM hallucinating tool calls.
        if parsed is None:
            logger.warning("Invalid JSON response, retrying with explicit instruction")
            retry_prompt = SYSTEM_PROMPT_NO_TOOL + JSON_RETRY_SUFFIX
            raw, retry_in, retry_out = self._invoke(retry_prompt, truncated_text)
            input_tokens += retry_in
            output_tokens += retry_out
            parsed = self._try_parse_json(raw)
            if parsed is None:
                raise ValueError(
                    f"Bedrock returned invalid JSON after retry. Raw response: {raw[:500]}"
                )

        # Normalize keywords: resolve case, synonyms, and acronyms to exact
        # IEEE Thesaurus preferred terms (e.g., "Deep Learning" → "Deep learning",
        # "AI" → "Artificial intelligence", "Rectenna" → "Rectennas").
        if self._thesaurus.term_count > 0 and "keywords" in effective_fields:
            raw_keywords = parsed["keywords"]
            parsed["keywords"] = self._thesaurus.normalize_keywords(raw_keywords)
            changes = [
                f"{old!r} → {new!r}"
                for old, new in zip(raw_keywords, parsed["keywords"])
                if old != new
            ]
            if changes:
                logger.info("Normalized keywords: %s", "; ".join(changes))

            # When we have enough thesaurus matches, drop non-thesaurus
            # terms — prefer a clean set of standardized terms.
            thesaurus_kws = [
                kw for kw in parsed["keywords"]
                if self._thesaurus.is_preferred_term(kw)
            ]
            non_thesaurus = [
                kw for kw in parsed["keywords"]
                if not self._thesaurus.is_preferred_term(kw)
            ]
            if len(thesaurus_kws) >= MIN_THESAURUS_KEYWORDS:
                if non_thesaurus:
                    logger.info(
                        "Dropping %d non-thesaurus keywords (have %d thesaurus matches): %s",
                        len(non_thesaurus), len(thesaurus_kws), non_thesaurus,
                    )
                parsed["keywords"] = thesaurus_kws

        # Validate after normalization/filtering so keyword count reflects final state
        self._validate_result(parsed, effective_fields)

        # Measure thesaurus coverage
        coverage_count = 0
        if self._thesaurus.term_count > 0 and "keywords" in effective_fields:
            coverage_count, matched = self._thesaurus.coverage(parsed["keywords"])
            logger.info(
                "Thesaurus coverage: %d/%d keywords matched: %s",
                coverage_count,
                len(parsed["keywords"]),
                matched,
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)

        publish_metrics(self._cloudwatch, [
            {"MetricName": "bedrock-input-tokens", "Value": input_tokens, "Unit": "Count"},
            {"MetricName": "bedrock-output-tokens", "Value": output_tokens, "Unit": "Count"},
        ])

        metadata = {k: parsed[k] for k in effective_fields}
        return InferenceResult(
            **metadata,
            processing_time_ms=elapsed_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            thesaurus_coverage=coverage_count,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _invoke(self, system_prompt: str, user_text: str) -> tuple[str, int, int]:
        """Call Bedrock invoke_model API with exponential backoff on throttling.

        Returns:
            Tuple of (response_text, input_tokens, output_tokens).
        """
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": MAX_TOKENS,
            "temperature": TEMPERATURE,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": user_text},
            ],
        })

        return self._call_bedrock(body)

    def _invoke_with_tools(
        self, system_prompt: str, user_text: str
    ) -> tuple[str, int, int]:
        """Call Bedrock with thesaurus tool use, handling multi-turn conversation.

        The LLM can call search_ieee_thesaurus to look up IEEE terms before
        selecting keywords. We loop until the LLM produces a final text response
        or we hit the iteration limit.

        Returns:
            Tuple of (response_text, total_input_tokens, total_output_tokens).
        """
        messages = [{"role": "user", "content": user_text}]
        total_input = 0
        total_output = 0

        for iteration in range(MAX_TOOL_ITERATIONS):
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": MAX_TOKENS,
                "temperature": TEMPERATURE,
                "system": system_prompt,
                "messages": messages,
                "tools": [THESAURUS_TOOL],
            })

            result = self._call_bedrock_raw(body)
            usage = result.get("usage", {})
            total_input += usage.get("input_tokens", 0)
            total_output += usage.get("output_tokens", 0)

            stop_reason = result.get("stop_reason", "end_turn")
            content_blocks = result.get("content", [])

            if stop_reason != "tool_use":
                # Final response — extract text
                text = self._extract_text(content_blocks)
                return text, total_input, total_output

            # Process tool calls
            messages.append({"role": "assistant", "content": content_blocks})

            tool_results = []
            for block in content_blocks:
                if block.get("type") != "tool_use":
                    continue

                tool_id = block["id"]
                tool_name = block["name"]
                tool_input = block.get("input", {})

                if tool_name == "search_ieee_thesaurus":
                    query = tool_input.get("query", "")
                    logger.info("Thesaurus tool call: query=%r", query)
                    search_results = self._thesaurus.search(query, limit=20)
                    result_text = json.dumps(search_results, indent=2)
                else:
                    result_text = json.dumps({"error": f"Unknown tool: {tool_name}"})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result_text,
                })

            messages.append({"role": "user", "content": tool_results})
            logger.info(
                "Tool iteration %d/%d completed, %d tool calls",
                iteration + 1, MAX_TOOL_ITERATIONS, len(tool_results),
            )

        # Exhausted iterations — try to extract any text from last response
        logger.warning("Tool use loop reached max iterations (%d)", MAX_TOOL_ITERATIONS)
        text = self._extract_text(content_blocks)
        return text, total_input, total_output

    def _call_bedrock(self, body: str) -> tuple[str, int, int]:
        """Call invoke_model and return (text, input_tokens, output_tokens)."""
        result = self._call_bedrock_raw(body)
        usage = result.get("usage", {})
        text = self._extract_text(result.get("content", []))
        return text, usage.get("input_tokens", 0), usage.get("output_tokens", 0)

    def _call_bedrock_raw(self, body: str) -> dict:
        """Call invoke_model with retry on throttling, return full response dict."""
        for attempt in range(MAX_RETRIES):
            try:
                response = self._bedrock.invoke_model(
                    modelId=self._model_id,
                    contentType="application/json",
                    accept="application/json",
                    body=body,
                )
                return json.loads(response["body"].read())

            except ClientError as exc:
                error_code = exc.response["Error"]["Code"]
                if error_code == "ThrottlingException" and attempt < MAX_RETRIES - 1:
                    wait = BACKOFF_BASE * (2 ** attempt)
                    logger.warning(
                        "Bedrock throttled (attempt %d/%d), waiting %ds",
                        attempt + 1, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    continue
                raise

    @staticmethod
    def _extract_text(content_blocks: list[dict]) -> str:
        """Extract text from Bedrock response content blocks."""
        for block in content_blocks:
            if block.get("type") == "text":
                return block["text"]
        return ""

    @staticmethod
    def _try_parse_json(raw: str) -> dict | None:
        """Try to parse JSON from raw text, stripping markdown fences if present."""
        text = raw.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```json or ```) and last line (```)
            lines = [line for line in lines[1:] if line.strip() != "```"]
            text = "\n".join(lines).strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
            return None
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _validate_abstract(result: dict) -> None:
        abstract = result["abstract"]
        if not isinstance(abstract, str) or "\n\n" not in abstract:
            raise ValueError(
                "abstract must be a string with two paragraphs separated by \\n\\n"
            )
        paragraphs = [p.strip() for p in abstract.split("\n\n") if p.strip()]
        if len(paragraphs) != 2:
            raise ValueError(
                f"abstract must contain exactly two paragraphs, got {len(paragraphs)}"
            )
        for i, para in enumerate(paragraphs):
            word_count = len(para.split())
            if word_count < 50 or word_count > 150:
                raise ValueError(
                    f"abstract paragraph {i + 1} has {word_count} words "
                    f"(expected 50–150)"
                )

    @staticmethod
    def _validate_keywords(result: dict) -> None:
        keywords = result["keywords"]
        if not isinstance(keywords, list):
            raise ValueError("keywords must be an array")
        if not (8 <= len(keywords) <= 12):
            raise ValueError(
                f"keywords must have 8–12 items, got {len(keywords)}"
            )
        if not all(isinstance(k, str) and k.strip() for k in keywords):
            raise ValueError("All keywords must be non-empty strings")

    @staticmethod
    def _validate_learning_level(result: dict) -> None:
        if result["learning_level"] not in VALID_LEARNING_LEVELS:
            raise ValueError(
                f"Invalid learning_level: {result['learning_level']!r}. "
                f"Must be one of {sorted(VALID_LEARNING_LEVELS)}"
            )

    @staticmethod
    def _validate_intended_audience(result: dict) -> None:
        if result["intended_audience"] not in VALID_AUDIENCES:
            raise ValueError(
                f"Invalid intended_audience: {result['intended_audience']!r}. "
                f"Must be one of {sorted(VALID_AUDIENCES)}"
            )

    @staticmethod
    def _validate_category(result: dict) -> None:
        if result["category"] not in VALID_CATEGORIES:
            raise ValueError(
                f"Invalid category: {result['category']!r}. "
                f"Must be one of {sorted(VALID_CATEGORIES)}"
            )

    _FIELD_VALIDATORS = {
        "abstract": _validate_abstract.__func__,
        "keywords": _validate_keywords.__func__,
        "learning_level": _validate_learning_level.__func__,
        "intended_audience": _validate_intended_audience.__func__,
        "category": _validate_category.__func__,
    }

    @staticmethod
    def _validate_result(result: dict, requested_fields: frozenset[str] | None = None) -> None:
        """Validate that the parsed result has all required fields with valid values."""
        fields_to_check = requested_fields or ALL_FIELDS
        missing = fields_to_check - set(result.keys())
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(sorted(missing))}")

        for field in fields_to_check:
            BedrockInference._FIELD_VALIDATORS[field](result)
