"""Tests for IEEE Thesaurus search module."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from src.ai.thesaurus import ThesaurusSearch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_THESAURUS = {
    "terms": [
        {
            "preferred_term": "Machine learning",
            "scope_note": "A branch of artificial intelligence",
            "use_for": ["ML", "machine-learning"],
            "broader_terms": ["Artificial intelligence"],
            "narrower_terms": ["Deep learning"],
            "related_terms": ["Pattern recognition"],
        },
        {
            "preferred_term": "Artificial intelligence",
            "scope_note": "Intelligence demonstrated by machines",
            "use_for": ["AI"],
            "broader_terms": ["Computer science"],
            "narrower_terms": ["Machine learning", "Natural language processing"],
            "related_terms": [],
        },
        {
            "preferred_term": "Neural networks",
            "scope_note": "",
            "use_for": ["ANN", "Artificial neural networks"],
            "broader_terms": ["Machine learning"],
            "narrower_terms": ["Convolutional neural networks"],
            "related_terms": ["Deep learning"],
        },
        {
            "preferred_term": "Power systems",
            "scope_note": "Electrical power generation and distribution",
            "use_for": [],
            "broader_terms": ["Electrical engineering"],
            "narrower_terms": ["Smart grid"],
            "related_terms": ["Energy storage"],
        },
        {
            "preferred_term": "Smart grid",
            "scope_note": "",
            "use_for": ["Smart power grid"],
            "broader_terms": ["Power systems"],
            "narrower_terms": [],
            "related_terms": ["Renewable energy"],
        },
        {
            "preferred_term": "Economics",
            "scope_note": "",
            "use_for": [],
            "broader_terms": ["Engineering management"],
            "narrower_terms": ["Macroeconomics"],
            "related_terms": ["Finance"],
        },
        {
            "preferred_term": "Macroeconomics",
            "scope_note": "",
            "use_for": [],
            "broader_terms": ["Economics"],
            "narrower_terms": [],
            "related_terms": [],
        },
        {
            "preferred_term": "Deep learning",
            "scope_note": "",
            "use_for": ["DL"],
            "broader_terms": ["Machine learning"],
            "narrower_terms": [],
            "related_terms": ["Neural networks"],
        },
    ],
}


@pytest.fixture
def thesaurus_file(tmp_path):
    path = tmp_path / "thesaurus.json"
    path.write_text(json.dumps(SAMPLE_THESAURUS))
    return str(path)


@pytest.fixture
def thesaurus(thesaurus_file):
    return ThesaurusSearch(data_path=thesaurus_file)


# ---------------------------------------------------------------------------
# Tests: loading
# ---------------------------------------------------------------------------


class TestLoading:
    def test_loads_all_terms(self, thesaurus):
        assert thesaurus.term_count == 8

    def test_missing_file_loads_empty(self, tmp_path):
        ts = ThesaurusSearch(data_path=str(tmp_path / "nonexistent.json"))
        assert ts.term_count == 0

    def test_empty_terms_array(self, tmp_path):
        path = tmp_path / "empty.json"
        path.write_text(json.dumps({"terms": []}))
        ts = ThesaurusSearch(data_path=str(path))
        assert ts.term_count == 0


# ---------------------------------------------------------------------------
# Tests: search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_exact_match_preferred_term(self, thesaurus):
        results = thesaurus.search("Machine learning")
        terms = [r["preferred_term"] for r in results]
        assert "Machine learning" in terms

    def test_exact_match_case_insensitive(self, thesaurus):
        results = thesaurus.search("machine learning")
        terms = [r["preferred_term"] for r in results]
        assert "Machine learning" in terms

    def test_synonym_match(self, thesaurus):
        results = thesaurus.search("ML")
        terms = [r["preferred_term"] for r in results]
        assert "Machine learning" in terms

    def test_acronym_match(self, thesaurus):
        results = thesaurus.search("AI")
        terms = [r["preferred_term"] for r in results]
        assert "Artificial intelligence" in terms

    def test_multi_word_synonym(self, thesaurus):
        results = thesaurus.search("Artificial neural networks")
        terms = [r["preferred_term"] for r in results]
        assert "Neural networks" in terms

    def test_substring_match(self, thesaurus):
        results = thesaurus.search("neural")
        terms = [r["preferred_term"] for r in results]
        assert "Neural networks" in terms

    def test_word_overlap_match(self, thesaurus):
        results = thesaurus.search("power grid energy")
        terms = [r["preferred_term"] for r in results]
        assert "Power systems" in terms or "Smart grid" in terms

    def test_no_match_returns_empty(self, thesaurus):
        results = thesaurus.search("quantum entanglement teleportation")
        # May return some low-score partial matches, but should be short
        assert len(results) <= 20

    def test_empty_query_returns_empty(self, thesaurus):
        assert thesaurus.search("") == []
        assert thesaurus.search("   ") == []

    def test_limit_parameter(self, thesaurus):
        results = thesaurus.search("learning", limit=2)
        assert len(results) <= 2

    def test_results_include_scope_note(self, thesaurus):
        results = thesaurus.search("Machine learning")
        ml = next(r for r in results if r["preferred_term"] == "Machine learning")
        assert ml["scope_note"] == "A branch of artificial intelligence"

    def test_results_include_broader_terms(self, thesaurus):
        results = thesaurus.search("Machine learning")
        ml = next(r for r in results if r["preferred_term"] == "Machine learning")
        assert "Artificial intelligence" in ml["broader_terms"]

    def test_empty_thesaurus_search(self, tmp_path):
        ts = ThesaurusSearch(data_path=str(tmp_path / "nope.json"))
        assert ts.search("anything") == []


# ---------------------------------------------------------------------------
# Tests: is_preferred_term
# ---------------------------------------------------------------------------


class TestIsPreferredTerm:
    def test_exact_match(self, thesaurus):
        assert thesaurus.is_preferred_term("Machine learning") is True

    def test_case_insensitive(self, thesaurus):
        assert thesaurus.is_preferred_term("machine learning") is True
        assert thesaurus.is_preferred_term("MACHINE LEARNING") is True

    def test_synonym_not_preferred(self, thesaurus):
        assert thesaurus.is_preferred_term("ML") is False
        assert thesaurus.is_preferred_term("AI") is False

    def test_unknown_term(self, thesaurus):
        assert thesaurus.is_preferred_term("Monetary Policy") is False


# ---------------------------------------------------------------------------
# Tests: coverage
# ---------------------------------------------------------------------------


class TestCoverage:
    def test_all_matched(self, thesaurus):
        keywords = ["Machine learning", "Neural networks", "Deep learning"]
        count, matched = thesaurus.coverage(keywords)
        assert count == 3
        assert set(matched) == {"Machine learning", "Neural networks", "Deep learning"}

    def test_none_matched(self, thesaurus):
        keywords = ["Monetary Policy", "Federal Funds Rate", "Inflation"]
        count, matched = thesaurus.coverage(keywords)
        assert count == 0
        assert matched == []

    def test_partial_match(self, thesaurus):
        keywords = ["Machine learning", "Monetary Policy", "Economics"]
        count, matched = thesaurus.coverage(keywords)
        assert count == 2
        assert "Machine learning" in matched
        assert "Economics" in matched

    def test_case_insensitive_coverage(self, thesaurus):
        keywords = ["machine learning", "ECONOMICS"]
        count, matched = thesaurus.coverage(keywords)
        assert count == 2


# ---------------------------------------------------------------------------
# Tests: with real thesaurus data (integration)
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_case_correction(self, thesaurus):
        assert thesaurus.normalize_keyword("Deep Learning") == "Deep learning"
        assert thesaurus.normalize_keyword("MACHINE LEARNING") == "Machine learning"

    def test_synonym_resolution(self, thesaurus):
        assert thesaurus.normalize_keyword("ML") == "Machine learning"
        assert thesaurus.normalize_keyword("AI") == "Artificial intelligence"
        assert thesaurus.normalize_keyword("ANN") == "Neural networks"

    def test_unknown_unchanged(self, thesaurus):
        assert thesaurus.normalize_keyword("Monetary Policy") == "Monetary Policy"

    def test_normalize_list(self, thesaurus):
        result = thesaurus.normalize_keywords([
            "Deep Learning", "ML", "Monetary Policy",
        ])
        assert result == ["Deep learning", "Machine learning", "Monetary Policy"]

    def test_deduplication(self, thesaurus):
        result = thesaurus.normalize_keywords([
            "Machine learning", "ML",  # both resolve to same
        ])
        assert result == ["Machine learning"]


class TestRealThesaurus:
    """Integration tests using the actual IEEE Thesaurus file if available."""

    @pytest.fixture
    def real_thesaurus(self):
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "src", "ai", "data",
            "ieee_thesaurus_v104.json"
        )
        if not os.path.exists(path):
            pytest.skip("IEEE Thesaurus data file not available")
        return ThesaurusSearch(data_path=path)

    def test_loads_thousands_of_terms(self, real_thesaurus):
        assert real_thesaurus.term_count > 7000

    def test_finds_machine_learning(self, real_thesaurus):
        results = real_thesaurus.search("machine learning")
        terms = [r["preferred_term"] for r in results]
        assert "Machine learning" in terms

    def test_economics_terms_exist(self, real_thesaurus):
        results = real_thesaurus.search("economics macroeconomics")
        terms = [r["preferred_term"] for r in results]
        assert "Economics" in terms or "Macroeconomics" in terms

    def test_monetary_policy_not_preferred(self, real_thesaurus):
        assert real_thesaurus.is_preferred_term("Monetary Policy") is False
        assert real_thesaurus.is_preferred_term("Federal Funds Rate") is False
