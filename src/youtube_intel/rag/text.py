"""Pure text helpers for RAG: Vietnamese-aware normalisation and chunking."""

from __future__ import annotations

import hashlib
import re
import unicodedata

_URL_ONLY = re.compile(r"^\s*(?:[-•*>]\s*)?(?:https?://\S+|www\.\S+)\s*$", re.I)
_SPACES = re.compile(r"[ \t]+")


def normalize(text: str) -> str:
    """Lowercase, strip accents. "Đọc SÁCH" -> "doc sach".

    `đ`/`Đ` are separate letters in Unicode, not `d` + a combining mark, so NFD
    alone leaves them as is - map them explicitly or "doc sach" never matches
    "đọc sách".
    """
    text = (text or "").replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return _SPACES.sub(" ", text.lower()).strip()


def clean_description(description: str | None) -> str:
    """Drop lines that are only a URL (subscribe links, social links)."""
    lines = [ln.rstrip() for ln in (description or "").splitlines()]
    return "\n".join(ln for ln in lines if not _URL_ONLY.match(ln)).strip()


def chunk_video(title: str | None, description: str | None, *, chunk_chars: int = 1000,
                overlap: int = 150, max_chunks: int = 5) -> list[str]:
    """Split title + description into overlapping chunks, paragraph-aligned.

    Chunk 0 always starts with the title. Paragraphs are packed greedily up to
    chunk_chars; a paragraph longer than that is cut. Each later chunk starts
    with the last `overlap` characters of the previous one, so a sentence cut
    at a boundary is still searchable. At most max_chunks chunks.
    """
    if overlap >= chunk_chars:
        raise ValueError("overlap must be smaller than chunk_chars")
    title = (title or "").strip()
    body = clean_description(description)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]

    pieces: list[str] = []                      # paragraphs cut to fit
    for p in paragraphs:
        while len(p) > chunk_chars:
            pieces.append(p[:chunk_chars])
            p = p[chunk_chars - overlap:]
        pieces.append(p)

    chunks: list[str] = []
    current = title
    for p in pieces:
        candidate = f"{current}\n\n{p}" if current else p
        if len(candidate) <= chunk_chars:
            current = candidate
            continue
        chunks.append(current)
        if len(chunks) == max_chunks:
            return chunks
        tail = current[-overlap:] if overlap else ""
        current = f"{tail}\n\n{p}" if tail else p
    if current:
        chunks.append(current)
    return chunks[:max_chunks]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def query_terms(query: str, *, min_len: int = 2, max_terms: int = 12) -> list[str]:
    """Normalised, de-duplicated word terms for a full-text OR query."""
    words = re.findall(r"\w+", normalize(query))
    return list(dict.fromkeys(w for w in words if len(w) >= min_len))[:max_terms]
