"""
Load the canon (C-02) from config/canon_sources.yaml.

    source (status: verified) --fetch--> data/canon_raw/<id>.txt   (cached; git-ignored)
                              --clean--> lines (Gutenberg header off, wikitext markup off)
                              --split--> passages along natural units (canon/chunk.py)
                              --load---> yti.canon_works + yti.canon_passages

C-01 rule: a source that is not `status: verified` - year, URL and license checked,
with the evidence written down - is never loaded. Re-loading a work is idempotent;
a passage whose text changed loses its embedding so it is embedded again.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests
import yaml

from canon.chunk import (ChunkError, Passage, clean_lines, levels_from_config, prepare,
                         ref_label, split)
from canon.text import content_hash, normalize_for_match, search_text
from youtube_intel.config import ConfigError

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES = ROOT / "config" / "canon_sources.yaml"
RAW_DIR = ROOT / "data" / "canon_raw"
LICENSES = ("public_domain", "own")
STATUSES = ("verified", "unverified", "excluded")
USER_AGENT = "YT_ELT-canon/1.0 (research; contact: repository owner)"


class CanonError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    id: str
    work: str
    work_vi: str
    author: str
    translator: str | None
    pub_year: int
    language: str
    pillar: str
    license: str
    status: str
    verified_on: date | None
    source_url: str
    fetch: tuple[str, ...]
    format: str                       # text | wikitext
    parser: dict
    ref_label: str
    align: str | None
    evidence: tuple[str, ...]


def load_sources(path: Path | str | None = None) -> list[Source]:
    path = Path(path) if path else DEFAULT_SOURCES
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out, seen = [], set()
    for i, s in enumerate(raw.get("sources") or []):
        where = f"{path}: sources[{i}] ({s.get('id')})"
        try:
            src = Source(
                id=s["id"], work=s["work"], work_vi=s["work_vi"], author=s["author"],
                translator=s.get("translator"), pub_year=int(s["pub_year"]),
                language=s["language"], pillar=s["pillar"], license=s["license"],
                status=s["status"], verified_on=s.get("verified_on"),
                source_url=s["source_url"], fetch=tuple(s.get("fetch") or ()),
                format=s.get("format", "text"), parser=s.get("parser") or {},
                ref_label=s["ref_label"], align=s.get("align"),
                evidence=tuple(s.get("evidence") or ()))
        except KeyError as e:
            raise ConfigError(f"{where}: missing {e}") from None
        if src.id in seen:
            raise ConfigError(f"{where}: duplicate id")
        seen.add(src.id)
        if src.license not in LICENSES or src.status not in STATUSES:
            raise ConfigError(f"{where}: license in {LICENSES}, status in {STATUSES}")
        if src.status == "verified" and (not src.verified_on or not src.evidence):
            raise ConfigError(f"{where}: a verified source needs verified_on and evidence (C-01)")
        if not re.fullmatch(r"[a-z0-9_.]+", src.id):
            raise ConfigError(f"{where}: id may use a-z 0-9 _ . only (it prefixes passage ids)")
        out.append(src)
    return out


# ---- fetching ---------------------------------------------------------------------
def fetch(src: Source, *, raw_dir: Path = RAW_DIR, refresh: bool = False) -> str:
    """Concatenated source text, downloaded once and cached."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{src.id}.txt"
    if path.exists() and not refresh:
        return path.read_text(encoding="utf-8")
    parts = []
    for n, url in enumerate(src.fetch, start=1):
        r = requests.get(url, timeout=60, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        r.encoding = "utf-8"
        # Works split over sub-pages (Analects books, Zhuangzi chapters): the page order
        # gives the number, written as a heading the parser can match.
        head = src.parser.get("fetch_heading")
        parts.append((head.format(n=n) + "\n\n" if head else "") + r.text)
        time.sleep(1)                                 # be polite to Gutenberg / Wikisource
    text = "\n\n".join(parts)
    path.write_text(text, encoding="utf-8")
    return text


_WIKI_SUBS = [
    (re.compile(r"<ref[^>/]*/>|<ref[^>]*>.*?</ref>", re.S), ""),
    # {{另|A|B}}: A with a variant reading B - keep the main reading, never drop the character
    (re.compile(r"\{\{另\|([^|{}]*)\|[^{}]*\}\}"), r"\1"),
    (re.compile(r"<!--.*?-->", re.S), ""),
    (re.compile(r"\{\{[^{}]*\}\}"), ""),             # templates (applied twice for nesting)
    (re.compile(r"\{\{[^{}]*\}\}"), ""),
    (re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]"), r"\1"),
    (re.compile(r"'{2,}"), ""),
    (re.compile(r"<[^>]+>"), ""),
]


def clean_wikitext(text: str) -> str:
    for rx, repl in _WIKI_SUBS:
        text = rx.sub(repl, text)
    return text


def passages(src: Source, text: str) -> list[Passage]:
    if src.format == "wikitext":
        text = clean_wikitext(text)
    p = src.parser
    for rx, repl in p.get("replace") or []:          # source-specific markup -> headings
        text = re.sub(rx, repl, text)
    if src.format == "html":
        text = html_to_text(text)
    lines = clean_lines(text, start_after=p.get("start_after"), end_before=p.get("end_before"))
    lines = prepare(lines, drop_indented=p.get("drop_indented"), drop_lines=p.get("drop_lines"))
    return split(lines, levels_from_config(p.get("levels") or []),
                 strip_line_prefix=p.get("strip_line_prefix"))


def html_to_text(html: str) -> str:
    """Rendered Wikisource / Latin Library HTML -> lines; block tags become line breaks."""
    import html as htmllib
    # Parsoid puts JSON (with '>' inside) in data-* attributes: drop them before tags.
    html = re.sub(r"""\sdata-[\w-]+=(?:'[^']*'|"[^"]*")""", "", html)
    html = re.sub(r"(?is)<(script|style|sup)[^>]*>.*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</?(p|div|h[1-6]|li|tr|table|blockquote)[^>]*>", "\n\n", html)
    text = htmllib.unescape(re.sub(r"<[^>]+>", "", html))
    return re.sub(r"\n{3,}", "\n\n", text)


# ---- loading ------------------------------------------------------------------------
def load_work(cur, src: Source, parts: list[Passage]) -> dict:
    """Upsert the work and its passages. Refuses unverified sources (C-01)."""
    if src.status != "verified":
        raise CanonError(f"{src.id}: status {src.status!r} - only verified sources are loaded (C-01)")
    if not parts:
        raise CanonError(f"{src.id}: the parser produced no passage")
    cur.execute(
        """INSERT INTO yti.canon_works (work_id, source_id, work, work_vi, author, translator,
               pub_year, language, pillar, license, source_url, verified_on)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (work_id) DO UPDATE SET work = EXCLUDED.work, work_vi = EXCLUDED.work_vi,
               author = EXCLUDED.author, translator = EXCLUDED.translator,
               pub_year = EXCLUDED.pub_year, language = EXCLUDED.language,
               pillar = EXCLUDED.pillar, license = EXCLUDED.license,
               source_url = EXCLUDED.source_url, verified_on = EXCLUDED.verified_on,
               loaded_at = now()""",
        (src.id, src.id, src.work, src.work_vi, src.author, src.translator, src.pub_year,
         src.language, src.pillar, src.license, src.source_url, src.verified_on))
    ids = []
    for seq, p in enumerate(parts, start=1):
        pid = f"{src.id}.{p.ref}"
        ids.append(pid)
        align = src.align.format(**p.numbers, title=p.title or "") if src.align else None
        cur.execute(
            """INSERT INTO yti.canon_passages (passage_id, work_id, ref, ref_label, seq, text,
                   text_norm, search_text, license, align_group, content_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (passage_id) DO UPDATE SET ref_label = EXCLUDED.ref_label,
                   seq = EXCLUDED.seq, text = EXCLUDED.text, text_norm = EXCLUDED.text_norm,
                   search_text = EXCLUDED.search_text, license = EXCLUDED.license,
                   align_group = EXCLUDED.align_group, content_hash = EXCLUDED.content_hash,
                   embedding = CASE WHEN yti.canon_passages.content_hash = EXCLUDED.content_hash
                                    THEN yti.canon_passages.embedding END,
                   embedding_model = CASE WHEN yti.canon_passages.content_hash = EXCLUDED.content_hash
                                          THEN yti.canon_passages.embedding_model END""",
            (pid, src.id, p.ref, ref_label(src.ref_label, p), seq, p.text,
             normalize_for_match(p.text), search_text(p.text), src.license, align,
             content_hash(p.text)))
    cur.execute("DELETE FROM yti.canon_passages WHERE work_id = %s AND NOT (passage_id = ANY(%s))",
                (src.id, ids))
    return {"work_id": src.id, "passages": len(ids), "removed": cur.rowcount}


def pending_embeddings(cur, model: str, limit: int = 5000) -> list[dict]:
    cur.execute("""SELECT passage_id, text FROM yti.canon_passages
                    WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM %s
                    ORDER BY work_id, seq LIMIT %s""", (model, limit))
    return [dict(r) for r in cur.fetchall()]


def store_embeddings(cur, rows, vectors, model: str) -> int:
    from youtube_intel.rag.indexer import vector_literal
    for r, v in zip(rows, vectors):
        cur.execute("UPDATE yti.canon_passages SET embedding = %s::vector, embedding_model = %s "
                    "WHERE passage_id = %s", (vector_literal(v), model, r["passage_id"]))
    return len(rows)


__all__ = ["Source", "load_sources", "fetch", "passages", "load_work", "clean_wikitext",
           "pending_embeddings", "store_embeddings", "CanonError", "ChunkError"]
