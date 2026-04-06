"""IEEE Thesaurus search module for keyword grounding.

Loads IEEE Thesaurus v1.04 data and provides search functionality
so the Bedrock LLM can find standardized IEEE terms via tool use.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict

logger = logging.getLogger(__name__)

DEFAULT_DATA_PATH = os.path.join(
    os.path.dirname(__file__), "data", "ieee_thesaurus_v104.json"
)


class ThesaurusSearch:
    """Search index over IEEE Thesaurus v1.04 for keyword grounding."""

    def __init__(self, data_path: str | None = None):
        self._preferred_terms: set[str] = set()
        self._preferred_lower: dict[str, str] = {}  # lowered → original
        self._synonym_index: dict[str, str] = {}  # lowered synonym → preferred term
        self._word_index: dict[str, set[str]] = defaultdict(set)  # word → preferred terms
        self._scope_notes: dict[str, str] = {}  # preferred term → scope note
        self._broader: dict[str, list[str]] = {}  # preferred term → broader terms

        path = data_path or DEFAULT_DATA_PATH
        if os.path.exists(path):
            self._load(path)
        else:
            logger.warning("Thesaurus file not found: %s", path)

    @property
    def term_count(self) -> int:
        return len(self._preferred_terms)

    def _load(self, path: str) -> None:
        with open(path) as f:
            data = json.load(f)

        for entry in data.get("terms", []):
            pref = entry.get("preferred_term", "").strip()
            if not pref:
                continue

            self._preferred_terms.add(pref)
            self._preferred_lower[pref.lower()] = pref
            self._scope_notes[pref] = entry.get("scope_note") or ""
            self._broader[pref] = entry.get("broader_terms", [])

            # Index words from preferred term
            for word in pref.lower().split():
                if len(word) > 2:
                    self._word_index[word].add(pref)

            # Index USE FOR synonyms
            for syn in entry.get("use_for", []):
                syn_stripped = syn.strip()
                if syn_stripped:
                    self._synonym_index[syn_stripped.lower()] = pref
                    for word in syn_stripped.lower().split():
                        if len(word) > 2:
                            self._word_index[word].add(pref)

        logger.info(
            "Loaded IEEE Thesaurus: %d preferred terms, %d synonyms",
            len(self._preferred_terms),
            len(self._synonym_index),
        )

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Search for thesaurus terms matching a query string.

        Uses multi-strategy matching:
        1. Exact match against preferred terms and synonyms
        2. Substring match against preferred terms
        3. Word-overlap scoring for broader matching

        Returns list of dicts with preferred_term, scope_note, broader_terms.
        """
        if not self._preferred_terms:
            return []

        query = query.strip()
        if not query:
            return []

        results: dict[str, float] = {}
        query_lower = query.lower()
        query_words = {w for w in query_lower.split() if len(w) > 2}

        # Strategy 1: Exact match on preferred term or synonym (highest score)
        if query_lower in self._preferred_lower:
            pref = self._preferred_lower[query_lower]
            results[pref] = 100.0
        if query_lower in self._synonym_index:
            pref = self._synonym_index[query_lower]
            results[pref] = results.get(pref, 0) + 90.0

        # Strategy 2: Substring match on preferred terms
        for pref_lower, pref in self._preferred_lower.items():
            if query_lower in pref_lower or pref_lower in query_lower:
                results[pref] = max(results.get(pref, 0), 50.0)

        # Strategy 3: Word-overlap scoring
        for word in query_words:
            for pref in self._word_index.get(word, set()):
                pref_words = set(pref.lower().split())
                overlap = len(query_words & pref_words)
                # Score based on overlap ratio relative to term length
                score = (overlap / max(len(pref_words), 1)) * 30.0
                results[pref] = max(results.get(pref, 0), score)

        # Sort by score descending, limit results
        sorted_terms = sorted(results.items(), key=lambda x: -x[1])[:limit]

        return [
            {
                "preferred_term": term,
                "scope_note": self._scope_notes.get(term, ""),
                "broader_terms": self._broader.get(term, []),
            }
            for term, _score in sorted_terms
        ]

    def is_preferred_term(self, term: str) -> bool:
        """Check if a term exists in the thesaurus (case-insensitive)."""
        return term.strip().lower() in self._preferred_lower

    def coverage(self, keywords: list[str]) -> tuple[int, list[str]]:
        """Return (count, matched_terms) for thesaurus coverage reporting."""
        matched = [kw for kw in keywords if self.is_preferred_term(kw)]
        return len(matched), matched
