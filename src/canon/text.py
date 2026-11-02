"""
Text helpers shared by indexing AND querying - one tokeniser for both, so a query
can never be tokenised differently from the index (the RegScan bug, spec §9).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from youtube_intel.rag.text import normalize as strip_accents

_CJK = r"㐀-䶿一-鿿豈-﫿"
_RUNS = re.compile(rf"[{_CJK}]+|[^\W\d_{_CJK}]+|\d+")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "«": '"', "»": '"',
                         "「": '"', "」": '"', "『": '"', "』": '"', "–": "-", "—": "-"})

# Function words of the canon's languages and of Vietnamese queries, accents stripped.
# The RAG eval (2026-10-31) showed what they do to an OR query: "and", "nhat"... match
# everything and the "nothing relevant" gate never fires.
STOPWORDS = frozenset("""
the a an and or of to in on for with is are was were be been it its that this these those
as at by from his he her she him you your we our they their not but all so no nor what which
who whom then than there when where how into upon unto thee thou thy shall will would may
le la les de des du un une et en au aux que qui ne pas il elle ils ce se sa son ses sur par
est non ad cum quod ut sed quae quam nec si aut atque ab ex enim esse
va cua la co nhung cho voi trong mot cac nay do khi de thi ma da duoc khong cung nhu ve
""".split())


def normalize_for_match(text: str) -> str:
    """For exact citation checks: NFKC, case-folded, quotes unified, punctuation and
    spacing ignored. Two renderings of the same passage compare equal."""
    t = unicodedata.normalize("NFKC", text or "").translate(_QUOTES).casefold()
    t = re.sub(rf"[^\w{_CJK}]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def search_tokens(text: str) -> list[str]:
    """Words without accents (stop words dropped); Chinese as character unigrams and
    bigrams, because Chinese has no spaces between words."""
    out: list[str] = []
    for run in _RUNS.findall(unicodedata.normalize("NFKC", text or "")):
        if re.match(rf"[{_CJK}]", run):
            out += list(run) + [run[i:i + 2] for i in range(len(run) - 1)]
        else:
            w = strip_accents(run)
            if len(w) >= 2 and w not in STOPWORDS:
                out.append(w)
    return out


def search_text(text: str) -> str:
    return " ".join(search_tokens(text))


def tsquery(text: str, max_terms: int = 24) -> str | None:
    """OR query over the same tokens; None when nothing is left to search."""
    terms = list(dict.fromkeys(search_tokens(text)))[:max_terms]
    return " | ".join(t.replace("'", "") for t in terms) or None


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
