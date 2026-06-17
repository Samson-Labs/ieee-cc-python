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

DEFAULT_CUSTOM_SYNONYMS_PATH = os.path.join(
    os.path.dirname(__file__), "data", "custom_synonyms.json"
)


def _stem(word: str) -> str:
    """Crude stemmer: strip common suffixes for fuzzy word matching.

    Aims for convergence: "detection", "detectors", "detecting" all → "detect".
    Uses two-pass stripping for compound suffixes (e.g., -tion then -t).
    """
    w = word.lower()
    # Pass 1: strip primary suffixes
    for suffix in ("ation", "tion", "sion", "ment", "ness", "ing", "ors", "ers", "or", "er", "es", "ed", "ly", "s"):
        if len(w) > len(suffix) + 2 and w.endswith(suffix):
            w = w[: -len(suffix)]
            break
    # Pass 2: normalize residual endings so "detec" → "detect" converges
    # with "detect". We add back common consonant endings if the stem
    # looks truncated (ends in vowel + consonant pair).
    # Simpler approach: just use first N chars as the stem for matching.
    # 5 chars is enough to distinguish most roots while allowing convergence.
    if len(w) > 7:
        return w[:7]
    return w


class ThesaurusSearch:
    """Search index over IEEE Thesaurus v1.04 for keyword grounding."""

    def __init__(self, data_path: str | None = None, custom_synonyms_path: str | None = None):
        self._preferred_terms: set[str] = set()
        self._preferred_lower: dict[str, str] = {}  # lowered → original
        self._synonym_index: dict[str, str] = {}  # lowered synonym → preferred term
        self._word_index: dict[str, set[str]] = defaultdict(set)  # word → preferred terms
        self._stem_index: dict[str, set[str]] = defaultdict(set)  # stemmed word → preferred terms
        self._stem_to_terms: dict[frozenset, str] = {}  # frozenset of stems → preferred term
        self._scope_notes: dict[str, str] = {}  # preferred term → scope note
        self._broader: dict[str, list[str]] = {}  # preferred term → broader terms

        path = data_path or DEFAULT_DATA_PATH
        if os.path.exists(path):
            self._load(path)
        else:
            logger.warning("Thesaurus file not found: %s", path)

        synonyms_path = custom_synonyms_path or DEFAULT_CUSTOM_SYNONYMS_PATH
        if os.path.exists(synonyms_path):
            self._load_custom_synonyms(synonyms_path)

    @property
    def term_count(self) -> int:
        return len(self._preferred_terms)

    def _load(self, path: str) -> None:
        with open(path, encoding="utf-8") as f:
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
                    self._stem_index[_stem(word)].add(pref)

            # Index USE FOR synonyms
            for syn in entry.get("use_for", []):
                syn_stripped = syn.strip()
                if syn_stripped:
                    self._synonym_index[syn_stripped.lower()] = pref
                    for word in syn_stripped.lower().split():
                        if len(word) > 2:
                            self._word_index[word].add(pref)
                            self._stem_index[_stem(word)].add(pref)

        # Build stem signature index for fuzzy normalization.
        # Maps frozenset(stems) → preferred term for multi-word terms (≥2 words).
        for pref in self._preferred_terms:
            words = [w for w in pref.lower().split() if len(w) > 2]
            if len(words) >= 2:
                stems = frozenset(_stem(w) for w in words)
                # Only store if not already claimed (first-come wins)
                if stems not in self._stem_to_terms:
                    self._stem_to_terms[stems] = pref

        # Auto-generate singular forms for plural preferred terms.
        # Many thesaurus terms are plural ("Rectennas", "AC motors") but the LLM
        # often outputs singulars. This adds the singular as a synonym so
        # normalize_keyword() can resolve it.
        auto_count = 0
        for pref in list(self._preferred_terms):
            pref_lower = pref.lower()
            for suffix, cut, add in [("ies", 3, "y"), ("ses", 2, ""), ("es", 2, ""), ("s", 1, "")]:
                if pref_lower.endswith(suffix) and len(pref) > len(suffix) + 3:
                    singular = (pref[:-cut] + add) if cut else pref
                    singular_lower = singular.lower()
                    # Only add if the singular isn't already a term or synonym
                    if (
                        singular_lower not in self._preferred_lower
                        and singular_lower not in self._synonym_index
                    ):
                        self._synonym_index[singular_lower] = pref
                        auto_count += 1
                    break  # only apply the first matching suffix rule

        logger.info(
            "Loaded IEEE Thesaurus: %d preferred terms, %d synonyms "
            "(%d auto-generated singular forms)",
            len(self._preferred_terms),
            len(self._synonym_index),
            auto_count,
        )

    def _load_custom_synonyms(self, path: str) -> None:
        """Load additional synonym mappings from a JSON file.

        Format: {"synonym": "Preferred Term", ...}

        These supplement the IEEE Thesaurus USE FOR entries to handle
        common abbreviations, singular/plural variants, and other
        mappings that the thesaurus doesn't cover natively.
        """
        with open(path, encoding="utf-8") as f:
            mappings = json.load(f)

        count = 0
        for synonym, preferred_term in mappings.items():
            if synonym.startswith("_"):
                continue
            synonym_lower = synonym.strip().lower()
            # Only add if the preferred term actually exists in the thesaurus
            if preferred_term.strip().lower() in self._preferred_lower:
                self._synonym_index[synonym_lower] = self._preferred_lower[
                    preferred_term.strip().lower()
                ]
                count += 1
            else:
                logger.warning(
                    "Custom synonym %r maps to %r which is not a preferred term",
                    synonym, preferred_term,
                )

        if count:
            logger.info("Loaded %d custom synonym mappings from %s", count, path)

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
        query_stems = {_stem(w) for w in query_words}

        # Strategy 1: Exact match on preferred term or synonym (highest score)
        if query_lower in self._preferred_lower:
            pref = self._preferred_lower[query_lower]
            results[pref] = 100.0
        if query_lower in self._synonym_index:
            pref = self._synonym_index[query_lower]
            results[pref] = results.get(pref, 0) + 90.0

        # Strategy 1b: Multi-word sub-phrases from the query against terms/synonyms
        words_list = query_lower.split()
        for i in range(len(words_list)):
            for j in range(i + 2, min(i + 5, len(words_list) + 1)):
                phrase = " ".join(words_list[i:j])
                if phrase in self._preferred_lower:
                    pref = self._preferred_lower[phrase]
                    results[pref] = max(results.get(pref, 0), 80.0)
                if phrase in self._synonym_index:
                    pref = self._synonym_index[phrase]
                    results[pref] = max(results.get(pref, 0), 75.0)

        # Strategy 2: Substring match on preferred terms
        for pref_lower, pref in self._preferred_lower.items():
            if query_lower in pref_lower or pref_lower in query_lower:
                results[pref] = max(results.get(pref, 0), 50.0)

        # Strategy 3: Word-overlap scoring (exact words)
        for word in query_words:
            for pref in self._word_index.get(word, set()):
                pref_words = set(pref.lower().split())
                overlap = len(query_words & pref_words)
                score = (overlap / max(len(pref_words), 1)) * 30.0
                results[pref] = max(results.get(pref, 0), score)

        # Strategy 4: Stem-overlap scoring (handles plurals, -ing, -tion, etc.)
        for stem in query_stems:
            for pref in self._stem_index.get(stem, set()):
                pref_stems = {_stem(w) for w in pref.lower().split() if len(w) > 2}
                overlap = len(query_stems & pref_stems)
                score = (overlap / max(len(pref_stems), 1)) * 25.0
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

    def normalize_keyword(self, keyword: str) -> str:
        """Resolve a keyword to its exact IEEE Thesaurus preferred term.

        Checks (in order):
        1. Exact preferred term (case-insensitive) → return canonical form
        2. USE FOR synonym (case-insensitive) → return preferred term
        3. Fuzzy stem match — if the keyword's stems are a superset of a
           preferred term's stems, return that term (e.g., "gas detection
           sensor" → "Gas detectors" because stems {gas, detect} ⊆ {gas, detect, sensor})
        4. No match → return keyword unchanged

        This fixes LLM issues like wrong capitalization ("Deep Learning" →
        "Deep learning"), wrong pluralization ("Rectenna" → "Rectennas"),
        and acronyms ("AI" → "Artificial intelligence").
        """
        lower = keyword.strip().lower()

        # Check preferred terms
        if lower in self._preferred_lower:
            return self._preferred_lower[lower]

        # Check synonyms (includes acronyms and auto-singulars)
        if lower in self._synonym_index:
            return self._synonym_index[lower]

        # Fuzzy stem match: keyword stems must be a superset of a term's stems
        kw_words = [w for w in lower.split() if len(w) > 2]
        if len(kw_words) >= 2:
            kw_stems = frozenset(_stem(w) for w in kw_words)
            best_match = None
            best_len = 0
            for term_stems, pref in self._stem_to_terms.items():
                # Term's stems must be a subset of keyword's stems
                # and term must have ≥2 stems to avoid false positives
                if len(term_stems) >= 2 and term_stems <= kw_stems:
                    # Prefer the longest (most specific) match
                    if len(term_stems) > best_len:
                        best_len = len(term_stems)
                        best_match = pref
            if best_match:
                return best_match

        return keyword

    def normalize_keywords(self, keywords: list[str]) -> list[str]:
        """Normalize a list of keywords, preserving order and deduplicating."""
        seen: set[str] = set()
        normalized: list[str] = []
        for kw in keywords:
            norm = self.normalize_keyword(kw)
            key = norm.lower()
            if key not in seen:
                seen.add(key)
                normalized.append(norm)
        return normalized

    def is_preferred_term(self, term: str) -> bool:
        """Check if a term exists in the thesaurus (case-insensitive)."""
        return term.strip().lower() in self._preferred_lower

    def coverage(self, keywords: list[str]) -> tuple[int, list[str]]:
        """Return (count, matched_terms) for thesaurus coverage reporting."""
        matched = [kw for kw in keywords if self.is_preferred_term(kw)]
        return len(matched), matched
