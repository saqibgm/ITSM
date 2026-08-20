"""
Heading-aware KB article chunker (KB_WIKI_CURATION_RAG_PLAN Phase 3).

Ported from Project-IQ-V2's web/api/kb_cache_manager.py
(_split_article_sections/_chunk_section_body) — identified in the original
plan (§4.6) as the better of two existing chunkers in that repo. No
dependencies beyond stdlib re, so it can be unit-tested without a DB.
"""

import re

MIN_WORDS = 60
MAX_WORDS = 220


def chunk_article(title: str, content: str, min_words: int = MIN_WORDS, max_words: int = MAX_WORDS) -> list[dict]:
    """Split article content into heading-bounded, word-count-bounded chunks.

    Returns a list of {"heading": str, "content": str} dicts, in document order.
    Falls back to paragraph-block splitting when no markdown headings are present.
    """
    content = (content or "").strip()
    if not content:
        return []

    content = re.sub(r"\r\n", "\n", content)
    content = re.sub(r"\n{3,}", "\n\n", content)

    chunks: list[dict] = []
    heading_matches = list(re.finditer(r"(?m)^(#{1,3})\s+(.+)$", content))

    if heading_matches:
        split_points = [m.start() for m in heading_matches] + [len(content)]
        for idx, start in enumerate(split_points[:-1]):
            end = split_points[idx + 1]
            block = content[start:end].strip()
            if not block:
                continue
            lines = block.splitlines()
            heading_line = lines[0].lstrip("#").strip() if lines else title
            body = "\n".join(lines[1:]).strip()
            if not body:
                continue
            chunks.extend(_chunk_section_body(heading_line, body, min_words, max_words))
    else:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
        if paragraphs:
            joined = "\n\n".join(paragraphs)
            chunks.extend(_chunk_section_body(title, joined, min_words, max_words))

    return chunks


def _chunk_section_body(heading: str, body: str, min_words: int, max_words: int) -> list[dict]:
    """Chunk a section body into logical, paragraph-bounded pieces."""
    body = body.strip()
    if not body:
        return []

    max_words = max(max_words, min_words)
    min_words = max(20, min_words)

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if not paragraphs:
        paragraphs = [body]

    chunks: list[dict] = []
    current: list[str] = []
    current_words = 0
    part = 1

    def flush(force: bool = False):
        nonlocal part, current, current_words
        if not current:
            return
        if not force and current_words < min_words and chunks:
            return
        chunk_text = "\n\n".join(current).strip()
        if not chunk_text:
            return
        chunk_heading = heading if part == 1 else f"{heading} (Part {part})"
        chunks.append({"heading": chunk_heading, "content": chunk_text})
        part += 1
        current = []
        current_words = 0

    for paragraph in paragraphs:
        para_words = paragraph.split()
        if len(para_words) > max_words:
            sentences = re.split(r"(?<=[.!?])\s+", paragraph)
            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence:
                    continue
                sentence_words = sentence.split()
                if current_words + len(sentence_words) > max_words and current_words >= min_words:
                    flush(force=True)
                current.append(sentence)
                current_words += len(sentence_words)
                if current_words >= max_words:
                    flush(force=True)
            continue

        if current_words + len(para_words) > max_words and current_words >= min_words:
            flush(force=True)
        current.append(paragraph)
        current_words += len(para_words)

    flush(force=True)
    return chunks
