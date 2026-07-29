"""Deterministic, offset-preserving text chunking for the knowledge base.

Chunks are the addressable units of the Storage Layer. Each chunk records its
``[start_offset, end_offset)`` span in the *source extracted text* so that a
retrieval hit can be verified and cited back to an exact location in the
original document (the basis for the EOKA Evidence Layer).

Two splitters are available:

- :func:`chunk_text` — paragraph-based, suitable for plain text and mixed
  content.  Packs blank-line-separated paragraphs into windows of at most
  ``max_chars`` with an ``overlap`` character tail.

- :func:`chunk_markdown` — structure-aware, respects Markdown headings as
  section boundaries, keeps fenced code blocks atomic, and prepends the
  heading hierarchy as context to every chunk within a section.

Offsets always index into the *original* string, never a normalized copy.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

DEFAULT_MAX_CHARS = 1200
DEFAULT_OVERLAP = 150

_PARAGRAPH_RE = re.compile(r"\n\s*\n")

# Markdown structural patterns.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_FENCED_CODE_RE = re.compile(r"^```", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    """An offset-addressed slice of the source text."""

    seq: int
    start_offset: int
    end_offset: int
    text: str
    text_hash: str | None = None

    @classmethod
    def with_hash(
        cls,
        seq: int,
        start_offset: int,
        end_offset: int,
        text: str,
    ) -> Chunk:
        """Construct a chunk with its SHA-256 text hash computed."""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        return cls(seq, start_offset, end_offset, text, text_hash=digest)


# ── Paragraph-based splitter ──────────────────────────────────────────


def _iter_paragraph_spans(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` spans for blank-line separated paragraphs."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _PARAGRAPH_RE.finditer(text):
        end = match.start()
        if end > cursor:
            spans.append((cursor, end))
        cursor = match.end()
    if cursor < len(text):
        spans.append((cursor, len(text)))
    return spans


def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Split *text* into overlapping, offset-addressed chunks.

    Args:
        text: The source extracted text.
        max_chars: Maximum characters per chunk.
        overlap: Characters of trailing context carried into the next chunk.

    Returns:
        Ordered chunks whose spans index into *text*. Empty/whitespace-only
        input yields an empty list.
    """
    if not text or not text.strip():
        return []
    max_chars = max(1, max_chars)
    overlap = max(0, min(overlap, max_chars - 1))

    # Build candidate segments: whole paragraphs, hard-split when oversized.
    segments: list[tuple[int, int]] = []
    for start, end in _iter_paragraph_spans(text):
        if end - start <= max_chars:
            segments.append((start, end))
            continue
        pos = start
        while pos < end:
            seg_end = min(pos + max_chars, end)
            segments.append((pos, seg_end))
            pos = seg_end

    chunks: list[Chunk] = []
    seq = 0
    cur_start: int | None = None
    cur_end: int | None = None
    for seg_start, seg_end in segments:
        if cur_start is None:
            cur_start, cur_end = seg_start, seg_end
            continue
        # Extend the current window if the packed paragraph still fits.
        if seg_end - cur_start <= max_chars:
            cur_end = seg_end
            continue
        chunk_str = text[cur_start:cur_end]
        if chunk_str.strip():
            chunks.append(Chunk.with_hash(seq, cur_start, cur_end, chunk_str))
            seq += 1
        # Start the next window, carrying an overlap tail from the previous one.
        overlap_start = max(cur_start, (cur_end or seg_start) - overlap)
        cur_start = min(overlap_start, seg_start)
        cur_end = seg_end

    if cur_start is not None and cur_end is not None:
        chunk_str = text[cur_start:cur_end]
        if chunk_str.strip():
            chunks.append(Chunk.with_hash(seq, cur_start, cur_end, chunk_str))

    return chunks


# ── Markdown structure-aware splitter ─────────────────────────────────


@dataclass
class _Section:
    """A Markdown section delimited by headings."""

    heading: str  # e.g. "## Installation"
    heading_level: int  # 1–6
    breadcrumb: str  # e.g. "# Guide > ## Installation"
    start: int  # byte offset of first content after heading
    end: int  # byte offset of section end (exclusive)


def _find_fenced_code_ranges(text: str) -> list[tuple[int, int]]:
    """Return ``[start, end)`` ranges of fenced code blocks (``` ... ```)."""
    ranges: list[tuple[int, int]] = []
    lines = text.split("\n")
    cursor = 0
    in_code = False
    code_start = 0
    for line in lines:
        line_end = cursor + len(line) + 1  # +1 for \n
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_code:
                code_start = cursor
                in_code = True
            else:
                ranges.append((code_start, line_end))
                in_code = False
        cursor = line_end
    if in_code:
        ranges.append((code_start, len(text)))
    return ranges


def _is_in_code_block(
    offset: int, code_ranges: list[tuple[int, int]]
) -> bool:
    """True when *offset* falls inside a fenced code block."""
    for start, end in code_ranges:
        if start <= offset < end:
            return True
        if offset < start:
            break
    return False


def _parse_sections(text: str) -> list[_Section]:
    """Split Markdown text into sections by heading hierarchy."""
    code_ranges = _find_fenced_code_ranges(text)
    sections: list[_Section] = []
    heading_stack: list[tuple[int, str]] = []  # [(level, text), ...]

    heading_matches = list(_HEADING_RE.finditer(text))
    if not heading_matches:
        return []

    for m in heading_matches:
        # Skip headings inside code blocks.
        if _is_in_code_block(m.start(), code_ranges):
            continue

        level = len(m.group(1))
        h_text = m.group(2).strip()

        # Content of previous section ends at this heading's start.
        if sections:
            sections[-1].end = m.start()

        # Update heading stack: remove deeper/equal headings.
        heading_stack = [(lvl, t) for lvl, t in heading_stack if lvl < level]
        heading_stack.append((level, h_text))

        breadcrumb = " > ".join(f"{'#' * lvl} {t}" for lvl, t in heading_stack)

        section_start = m.end()
        # Trim leading newline after heading.
        while section_start < len(text) and text[section_start] in "\r\n":
            section_start += 1

        sections.append(
            _Section(
                heading=f"{'#' * level} {h_text}",
                heading_level=level,
                breadcrumb=breadcrumb,
                start=section_start,
                end=len(text),  # updated by next iteration
            )
        )

    return sections


def chunk_markdown(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Structure-aware Markdown chunking.

    Splits on heading boundaries and prepends each chunk with its section's
    heading breadcrumb for retrieval context.  Falls back to
    :func:`chunk_text` when the input has no Markdown headings.

    Args:
        text: Markdown source text.
        max_chars: Maximum characters per chunk (including breadcrumb prefix).
        overlap: Overlap characters between consecutive chunks.

    Returns:
        Ordered chunks with heading-aware context prefixes.
    """
    if not text or not text.strip():
        return []

    sections = _parse_sections(text)
    if not sections:
        # No headings found — fall back to paragraph-based chunking.
        return chunk_text(text, max_chars=max_chars, overlap=overlap)

    chunks: list[Chunk] = []
    seq = 0

    for section in sections:
        section_text = text[section.start : section.end]
        if not section_text.strip():
            continue

        # Budget for actual content (breadcrumb takes some chars).
        prefix = f"[{section.breadcrumb}]\n\n"
        content_budget = max(100, max_chars - len(prefix))

        # Chunk the section's content using the paragraph splitter.
        sub_chunks = chunk_text(
            section_text, max_chars=content_budget, overlap=overlap
        )

        for sc in sub_chunks:
            # Map sub-chunk offsets back to the original text.
            abs_start = section.start + sc.start_offset
            abs_end = section.start + sc.end_offset
            chunk_text_str = prefix + sc.text
            chunks.append(
                Chunk.with_hash(seq, abs_start, abs_end, chunk_text_str)
            )
            seq += 1

    # If the text has content before the first heading, chunk it too.
    first_section_start = sections[0].start if sections else len(text)
    # Back up to include the heading line itself.
    if sections:
        first_heading_match = _HEADING_RE.search(text)
        if first_heading_match and not _is_in_code_block(
            first_heading_match.start(),
            _find_fenced_code_ranges(text),
        ):
            first_section_start = first_heading_match.start()

    preamble = text[:first_section_start].strip()
    if preamble:
        preamble_chunks = chunk_text(
            preamble, max_chars=max_chars, overlap=overlap
        )
        # Re-number and prepend.
        for i, pc in enumerate(preamble_chunks):
            chunks.insert(
                i,
                Chunk.with_hash(i, pc.start_offset, pc.end_offset, pc.text),
            )
        # Re-sequence all chunks.
        for idx in range(len(chunks)):
            old = chunks[idx]
            chunks[idx] = Chunk.with_hash(
                idx, old.start_offset, old.end_offset, old.text
            )

    return chunks


# ── Auto-detection helper ────────────────────────────────────────────


def chunk_auto(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
    mime_type: str | None = None,
    filename: str | None = None,
) -> list[Chunk]:
    """Automatically select the best chunking strategy.

    Uses :func:`chunk_markdown` when the source is detected as Markdown
    (by MIME type or filename), otherwise falls back to :func:`chunk_text`.
    """
    is_markdown = False
    if mime_type and "markdown" in mime_type.lower():
        is_markdown = True
    elif filename:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        is_markdown = ext in ("md", "markdown", "mdx")

    if is_markdown:
        return chunk_markdown(text, max_chars=max_chars, overlap=overlap)
    return chunk_text(text, max_chars=max_chars, overlap=overlap)
