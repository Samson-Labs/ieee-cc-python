"""Bedrock Claude Integration for IEEE metadata generation.

Sends extracted document text to AWS Bedrock (Claude Sonnet) with the IEEE
Technical Metadata Specialist system prompt (v1.2) and returns structured
metadata: abstract, keywords, learning_level, intended_audience, category.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import TypedDict

import boto3
from botocore.exceptions import ClientError

from src.common.metrics import publish_metrics

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
MAX_TOKENS = 2048
TEMPERATURE = 0.3
TEXT_TRUNCATION_LIMIT = 180_000  # characters — fits within Claude's context window

# Retry configuration for Bedrock throttling (429)
MAX_RETRIES = 3
BACKOFF_BASE = 1  # seconds: 1, 2, 4

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

SYSTEM_PROMPT = (
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
    '1. **abstract** — A two-paragraph summary of the content. Each paragraph '
    "should be 50–150 words. Separate the two paragraphs with a blank line (\\n\\n). "
    "The first paragraph should describe the main topic, approach, and scope of the "
    "presentation or publication. "
    "The second paragraph should cover key findings, contributions, and implications.\n\n"
    '2. **keywords** — An array of 8–12 keyword strings that capture the content\'s '
    "core topics, technologies, methodologies, and application domains. Prefer specific "
    "technical terms over generic ones. When a relevant IEEE Thesaurus term exists, "
    "prefer it over a synonym.\n\n"
    '3. **learning_level** — One of the following:\n'
    '   - "Foundational" — introductory material suitable for students or newcomers\n'
    '   - "Professional" — intermediate material for practicing engineers\n'
    '   - "Expert" — advanced material requiring deep domain expertise\n\n'
    '4. **intended_audience** — One of the following:\n'
    '   - "Non-Engineer" — general public, managers, or policy-makers\n'
    '   - "Engineering Adjacent Professional" — technical writers, project managers\n'
    '   - "New Engineer" — early-career engineers, recent graduates\n'
    '   - "Seasoned Engineering Professional" — experienced engineers, researchers\n\n'
    '5. **category** — One of the following:\n'
    '   - "Research Papers and Publications" — original research, conference papers\n'
    '   - "Professional Development" — tutorials, courses, certification material\n'
    '   - "Society Outreach" — newsletters, community reports, event summaries\n'
    '   - "Technical Tutorial" — how-to guides, implementation walkthroughs\n\n'
    "Return ONLY a valid JSON object with these five fields. Do not include any text "
    "before or after the JSON. Do not wrap it in markdown code fences."
)

JSON_RETRY_SUFFIX = (
    "\n\nIMPORTANT: Your previous response was not valid JSON. "
    "Return ONLY a raw JSON object — no markdown, no explanation, no text outside "
    "the JSON braces."
)


class InferenceResult(TypedDict):
    abstract: str
    keywords: list[str]
    learning_level: str
    intended_audience: str
    category: str
    processing_time_ms: int
    input_tokens: int
    output_tokens: int


class BedrockInference:
    """Calls AWS Bedrock Claude to generate structured metadata from document text."""

    def __init__(self, bedrock_client=None, model_id: str | None = None, cloudwatch_client=None):
        self._bedrock = bedrock_client or boto3.client(
            "bedrock-runtime", region_name="us-east-1"
        )
        self._model_id = model_id or os.environ.get(
            "BEDROCK_MODEL_ID", DEFAULT_MODEL_ID
        )
        self._cloudwatch = cloudwatch_client

    def generate_metadata(
        self,
        text: str,
        thesaurus_terms: list[str] | None = None,
    ) -> InferenceResult:
        """Generate structured metadata from extracted document text.

        Args:
            text: Extracted document text (will be truncated if too long).
            thesaurus_terms: Optional IEEE Thesaurus terms to prioritize for keywords.

        Returns:
            InferenceResult with abstract, keywords, learning_level,
            intended_audience, category, processing_time_ms,
            input_tokens, and output_tokens.

        Raises:
            ValueError: If Bedrock response fails validation after retries.
            ClientError: If Bedrock API call fails (non-throttle errors).
        """
        start = time.monotonic()

        truncated_text = text[:TEXT_TRUNCATION_LIMIT]

        system_prompt = SYSTEM_PROMPT
        if thesaurus_terms:
            terms_str = ", ".join(thesaurus_terms)
            system_prompt += (
                f"\n\nWhen selecting keywords, prioritize terms from this "
                f"IEEE Thesaurus subset: {terms_str}"
            )

        # First attempt
        raw, input_tokens, output_tokens = self._invoke(system_prompt, truncated_text)
        parsed = self._try_parse_json(raw)

        # If JSON parsing failed, retry once with explicit JSON instruction
        if parsed is None:
            logger.warning("Invalid JSON response, retrying with explicit instruction")
            retry_prompt = system_prompt + JSON_RETRY_SUFFIX
            raw, retry_in, retry_out = self._invoke(retry_prompt, truncated_text)
            input_tokens += retry_in
            output_tokens += retry_out
            parsed = self._try_parse_json(raw)
            if parsed is None:
                raise ValueError(
                    f"Bedrock returned invalid JSON after retry. Raw response: {raw[:500]}"
                )

        self._validate_result(parsed)

        elapsed_ms = int((time.monotonic() - start) * 1000)

        publish_metrics(self._cloudwatch, [
            {"MetricName": "bedrock-input-tokens", "Value": input_tokens, "Unit": "Count"},
            {"MetricName": "bedrock-output-tokens", "Value": output_tokens, "Unit": "Count"},
        ])

        return InferenceResult(
            abstract=parsed["abstract"],
            keywords=parsed["keywords"],
            learning_level=parsed["learning_level"],
            intended_audience=parsed["intended_audience"],
            category=parsed["category"],
            processing_time_ms=elapsed_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
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

        for attempt in range(MAX_RETRIES):
            try:
                response = self._bedrock.invoke_model(
                    modelId=self._model_id,
                    contentType="application/json",
                    accept="application/json",
                    body=body,
                )
                result = json.loads(response["body"].read())
                usage = result.get("usage", {})
                return (
                    result["content"][0]["text"],
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                )

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
    def _validate_result(result: dict) -> None:
        """Validate that the parsed result has all required fields with valid values."""
        required = {"abstract", "keywords", "learning_level", "intended_audience", "category"}
        missing = required - set(result.keys())
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(sorted(missing))}")

        # abstract: string with two paragraphs
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

        # keywords: array of 8–12 strings
        keywords = result["keywords"]
        if not isinstance(keywords, list):
            raise ValueError("keywords must be an array")
        if not (8 <= len(keywords) <= 12):
            raise ValueError(
                f"keywords must have 8–12 items, got {len(keywords)}"
            )
        if not all(isinstance(k, str) and k.strip() for k in keywords):
            raise ValueError("All keywords must be non-empty strings")

        # learning_level
        if result["learning_level"] not in VALID_LEARNING_LEVELS:
            raise ValueError(
                f"Invalid learning_level: {result['learning_level']!r}. "
                f"Must be one of {sorted(VALID_LEARNING_LEVELS)}"
            )

        # intended_audience
        if result["intended_audience"] not in VALID_AUDIENCES:
            raise ValueError(
                f"Invalid intended_audience: {result['intended_audience']!r}. "
                f"Must be one of {sorted(VALID_AUDIENCES)}"
            )

        # category
        if result["category"] not in VALID_CATEGORIES:
            raise ValueError(
                f"Invalid category: {result['category']!r}. "
                f"Must be one of {sorted(VALID_CATEGORIES)}"
            )
