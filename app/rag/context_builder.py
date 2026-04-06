"""
RAG context builder: formats retrieved chunks into a prompt-ready string.

Responsibilities:
  - Token-budgeted selection (voice=1200 tokens, web=2500 tokens)
  - Lost-in-the-middle mitigation: interleave high/low scored chunks
  - Citation metadata appended per chunk
  - Graceful handling of empty chunk list
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.rag.retrieval import ChunkResult

logger = logging.getLogger(__name__)

# Token budget per channel (conservative estimates: ~4 chars/token)
_CHAR_BUDGET = {
    "voice": 1200 * 4,
    "web": 2500 * 4,
    "prefetch": 2500 * 4,
}
_DEFAULT_BUDGET = 1200 * 4


def build_rag_context(
    chunks: "list[ChunkResult]",
    channel: str = "voice",
) -> str:
    """
    Build a formatted RAG context string for injection into the system prompt.

    Uses the "lost-in-the-middle" mitigation: places the highest-scored chunks
    at the beginning and end of the context block, with lower-scored chunks in
    the middle — matching research findings on LLM attention patterns.

    Returns an empty string if chunks is empty.
    """
    if not chunks:
        return ""

    char_budget = _CHAR_BUDGET.get(channel, _DEFAULT_BUDGET)

    # Apply lost-in-the-middle ordering: top → bottom → interleaved middle
    ordered = _lost_in_middle_order(chunks)

    lines: list[str] = ["[RELEVANT KNOWLEDGE BASE CONTEXT]"]
    used_chars = len(lines[0])

    for chunk in ordered:
        prefix = f"({chunk.context_prefix}) " if chunk.context_prefix else ""
        citation = f"[Source: {chunk.title} ({chunk.source_type})]"
        entry = f"{prefix}{chunk.content}\n{citation}"
        entry_chars = len(entry) + 2  # +2 for separator

        if used_chars + entry_chars > char_budget:
            break

        lines.append(entry)
        used_chars += entry_chars

    if len(lines) <= 1:
        return ""

    lines.append("[END KNOWLEDGE BASE CONTEXT]")
    return "\n\n".join(lines)


def _lost_in_middle_order(
    chunks: "list[ChunkResult]",
) -> "list[ChunkResult]":
    """
    Reorder chunks so the most relevant appear at the start and end,
    with lower-relevance chunks in the middle.
    Requires at least 3 chunks to make a meaningful difference.
    """
    if len(chunks) <= 2:
        return chunks

    # Split into top half (higher scores) and bottom half
    mid = len(chunks) // 2
    top = chunks[:mid]
    bottom = chunks[mid:]

    # Interleave: start with top[0], then bottom items, end with remaining top
    if len(top) >= 2:
        return [top[0]] + bottom + top[1:]
    return top + bottom
