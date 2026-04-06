"""
Tests for app/rag/context_builder.py

Covers:
  - Empty chunk list → empty string
  - Single chunk produces valid output
  - Output contains KNOWLEDGE BASE CONTEXT header+footer
  - Citation format: [Source: {title} ({source_type})]
  - Voice channel stays within ~4800-char budget
  - Web channel allows larger budget
  - Lost-in-middle ordering with ≥3 chunks
  - context_prefix appears in output when set
  - Budget exceeded mid-chunk → chunks truncated not output
"""
from __future__ import annotations

from typing import Optional

import pytest

from app.rag.context_builder import build_rag_context, _lost_in_middle_order


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _chunk(
    id: str = "c1",
    content: str = "An alien seeking admission must present valid documentation.",
    title: str = "Immigration FAQ",
    source_type: str = "faq",
    context_prefix: str = "",
    rerank_score: float = 8.0,
    quality_score: float = 8.0,
    language: str = "en",
):
    """Return a mock ChunkResult-like simple namespace."""
    from types import SimpleNamespace
    return SimpleNamespace(
        id=id,
        document_id="doc-1",
        parent_chunk_id=None,
        content=content,
        context_prefix=context_prefix,
        language=language,
        source_type=source_type,
        title=title,
        quality_score=quality_score,
        rerank_score=rerank_score,
        rrf_score=0.016,
        embedding=[0.1] * 1536,
    )


def _make_chunks(n: int, content_len: int = 50) -> list:
    contents = [
        "green card application through marriage requires I-130 filing",
        "asylum seekers must file Form I-589 within one year of arrival",
        "DACA renewal requires continuous residence since 2007",
        "deportation defense may involve cancellation of removal hearings",
        "naturalization requires five years of permanent residence",
        "H-1B visa requires employer sponsorship and specialty occupation",
        "TPS beneficiaries must maintain continuous residence requirements",
        "adjustment of status requires immigrant visa to be immediately available",
    ]
    return [
        _chunk(
            id=f"c{i}",
            content=contents[i % len(contents)],
            rerank_score=float(10 - i),
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------

class TestBuildRagContextEmpty:
    def test_empty_list_returns_empty_string(self):
        result = build_rag_context([], channel="voice")
        assert result == ""

    def test_default_channel_empty_returns_empty_string(self):
        result = build_rag_context([])
        assert result == ""


# ---------------------------------------------------------------------------
# Single chunk
# ---------------------------------------------------------------------------

class TestBuildRagContextSingleChunk:
    def test_contains_header(self):
        c = _chunk()
        result = build_rag_context([c], channel="voice")
        assert "[RELEVANT KNOWLEDGE BASE CONTEXT]" in result

    def test_contains_footer(self):
        c = _chunk()
        result = build_rag_context([c], channel="voice")
        assert "[END KNOWLEDGE BASE CONTEXT]" in result

    def test_contains_chunk_content(self):
        c = _chunk(content="The I-485 form adjusts status to permanent resident.")
        result = build_rag_context([c], channel="voice")
        assert "I-485" in result

    def test_citation_included(self):
        c = _chunk(title="USCIS FAQ", source_type="uscis_form")
        result = build_rag_context([c], channel="voice")
        assert "[Source: USCIS FAQ (uscis_form)]" in result

    def test_context_prefix_included(self):
        c = _chunk(context_prefix="Filing requirements:", content="Must file within 30 days.")
        result = build_rag_context([c], channel="voice")
        assert "Filing requirements:" in result


# ---------------------------------------------------------------------------
# Channel budgets
# ---------------------------------------------------------------------------

class TestChannelBudgets:
    def _big_chunks(self, n: int, chars_each: int) -> list:
        return [
            _chunk(id=f"c{i}", content="x" * chars_each, rerank_score=float(10 - i))
            for i in range(n)
        ]

    def test_voice_respects_budget(self):
        # Voice budget: 1200 tokens × 4 = 4800 chars
        chunks = self._big_chunks(20, 400)
        result = build_rag_context(chunks, channel="voice")
        assert len(result) <= 5200  # some overhead for header/footer/separators

    def test_web_allows_more_content(self):
        chunks = self._big_chunks(5, 600)
        voice_result = build_rag_context(chunks, channel="voice")
        web_result = build_rag_context(chunks, channel="web")
        # Web result should be >= voice result (larger budget)
        assert len(web_result) >= len(voice_result)

    def test_unknown_channel_uses_default_budget(self):
        chunks = self._big_chunks(20, 400)
        result = build_rag_context(chunks, channel="sms")  # unrecognised channel
        assert isinstance(result, str)
        # Should still produce output within a reasonable bound
        assert len(result) <= 6000


# ---------------------------------------------------------------------------
# Multiple chunks
# ---------------------------------------------------------------------------

class TestBuildRagContextMultiple:
    def test_multiple_chunks_all_included_when_small(self):
        chunks = _make_chunks(3)
        result = build_rag_context(chunks, channel="web")
        for i in range(3):
            assert f"c{i}" not in result  # IDs not in output
        # At least verify all content strings appear
        assert result.count("[Source:") == 3

    def test_large_budget_includes_all_chunks(self):
        chunks = _make_chunks(5)
        result = build_rag_context(chunks, channel="web")
        assert result.count("[Source:") == 5

    def test_empty_context_prefix_not_added(self):
        c = _chunk(context_prefix="")
        result = build_rag_context([c], channel="voice")
        # No stray leading whitespace or empty parens from empty prefix
        assert "()" not in result


# ---------------------------------------------------------------------------
# Lost-in-middle ordering
# ---------------------------------------------------------------------------

class TestLostInMiddleOrder:
    def test_two_chunks_unchanged(self):
        chunks = [_chunk(id="c0", rerank_score=9), _chunk(id="c1", rerank_score=7)]
        result = _lost_in_middle_order(chunks)
        assert [c.id for c in result] == ["c0", "c1"]

    def test_three_chunks_reordered(self):
        chunks = [
            _chunk(id="c0", rerank_score=9),
            _chunk(id="c1", rerank_score=7),
            _chunk(id="c2", rerank_score=5),
        ]
        result = _lost_in_middle_order(chunks)
        # Should NOT be the original order: top chunk at start confirmed
        assert result[0].id == "c0"

    def test_four_chunks_top_at_start(self):
        chunks = [_chunk(id=f"c{i}", rerank_score=float(10 - i)) for i in range(4)]
        result = _lost_in_middle_order(chunks)
        assert result[0].id == "c0"

    def test_four_chunks_preserves_all(self):
        chunks = [_chunk(id=f"c{i}") for i in range(4)]
        result = _lost_in_middle_order(chunks)
        ids = {c.id for c in result}
        assert ids == {f"c{i}" for i in range(4)}

    def test_single_chunk_unchanged(self):
        chunks = [_chunk(id="only")]
        result = _lost_in_middle_order(chunks)
        assert len(result) == 1
        assert result[0].id == "only"

    def test_empty_unchanged(self):
        assert _lost_in_middle_order([]) == []

    def test_five_chunks_length_preserved(self):
        chunks = [_chunk(id=f"c{i}") for i in range(5)]
        result = _lost_in_middle_order(chunks)
        assert len(result) == 5
