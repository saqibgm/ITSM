"""Pure unit tests for app/services/ai/kb_chunker.py (no DB, no LLM calls)."""

from app.services.ai.kb_chunker import chunk_article


def test_chunk_article_empty_content_returns_empty_list():
    assert chunk_article("Title", "") == []
    assert chunk_article("Title", None) == []


def test_chunk_article_splits_on_markdown_headings():
    content = (
        "## Overview\n"
        "This section explains the refund policy in general terms for customers.\n\n"
        "## Timing\n"
        "Refunds are issued within 5-7 business days of the returned item being received."
    )
    chunks = chunk_article("Refund Policy", content)
    headings = [c["heading"] for c in chunks]
    assert "Overview" in headings
    assert "Timing" in headings
    for c in chunks:
        assert c["content"].strip()


def test_chunk_article_falls_back_to_paragraphs_without_headings():
    content = "First paragraph about the topic.\n\nSecond paragraph with more detail."
    chunks = chunk_article("General Info", content)
    assert len(chunks) >= 1
    assert chunks[0]["heading"] == "General Info"


def test_chunk_article_splits_long_section_into_multiple_parts():
    # ~500 words under one heading, well above the 220-word max chunk size
    long_body = " ".join(f"sentence{i} content word filler text." for i in range(150))
    content = f"## Long Section\n{long_body}"
    chunks = chunk_article("Big Article", content)
    assert len(chunks) > 1
    assert chunks[0]["heading"] == "Long Section"
    assert chunks[1]["heading"] == "Long Section (Part 2)"
    for c in chunks:
        word_count = len(c["content"].split())
        assert word_count <= 230  # max_words=220 plus small sentence-boundary slack
