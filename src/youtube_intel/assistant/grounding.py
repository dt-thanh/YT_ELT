"""
Grounding validator: every fact in an answer must come from this turn's tool results.

Pure functions, no I/O. Checks:
  * Every number (Vietnamese or English formatting, with K/M/B, nghìn/triệu/tỷ,
    or %) matches a tool value, allowing rounding (<= 0.5 % relative error, or
    equal when the tool value is rounded to 0-2 decimals, or to thousands /
    millions / billions).
  * Small integers (<= SMALL_INT_MAX) only count when they equal a rank, a
    row_count, or an integer field exactly - so "5 videos" cannot be grounded
    by an unrelated decimal that happens to round to 5.
  * Entity binding (G-01): in a sentence that names a video or channel (by id or
    by the title a tool returned), a number must come from that entity's own rows
    - the nearest named entity before it, else the nearest after it. Videos win
    over channels. Relations between two entities (pct_of_leader, gap_to_leader)
    may come from any entity named in the sentence. A rank claim ("đứng thứ 2",
    "dẫn đầu", "ranked 1") must equal that entity's rank.
  * Dates and clock times appear in tool results (UTC or Asia/Ho_Chi_Minh).
  * Every video id - cited or written in the text - was returned by a tool.
  * No URLs (links are built by code).
  * When a tool returned nothing, failed, or a video's growth is not computable
    (validity_reason != 'ok'), data_gaps must say so.

Ambiguous separators are handled by keeping every reading: "1.234" is 1234
(Vietnamese thousands) or 1.234 (English decimal); a number is grounded if any
reading matches.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

SMALL_INT_MAX = 10
REL_TOLERANCE = Decimal("0.005")
VN_OFFSET = timedelta(hours=7)

_MULTIPLIERS = {
    "k": 10**3, "nghìn": 10**3, "ngàn": 10**3, "thousand": 10**3,
    "m": 10**6, "mn": 10**6, "triệu": 10**6, "million": 10**6,
    "b": 10**9, "bn": 10**9, "tỷ": 10**9, "tỉ": 10**9, "billion": 10**9,
}

_URL = re.compile(r"https?://|www\.", re.I)
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?(?!\d)")
_DMY_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})/(\d{4})(?![\d/])")
_DM_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
_TIME = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::\d{2})?(?![\d:])")
_CHANNEL_ID = re.compile(r"(?<![\w-])UC[A-Za-z0-9_-]{22}(?![\w-])")
_VIDEO_ID = re.compile(r"(?<![\w-])[A-Za-z0-9_-]{11}(?![\w-])")
# A number, optionally followed by a scale word/letter or a percent sign.
# Not preceded by a letter/digit (so "v3", "gpt-4o" are skipped) and not
# followed by a letter (so "4o" is skipped) unless that letter is a scale suffix.
_NUMBER = re.compile(
    r"(?<![\w.,])(?P<num>\d[\d.,]*\d|\d)"
    r"(?:(?P<attached>[kKmMbB]|mn|bn)(?!\w)"
    r"|\s*(?P<word>nghìn|ngàn|triệu|tỷ|tỉ|thousand|million|billion)(?!\w)"
    r"|\s*(?P<pct>%|phần trăm|percent))?"
    r"(?![\w])"
)
_SENTENCE_END = re.compile(r"[.!?;](?=\s|$)|\n")

_RANK_WORDS = {"nhất": 1, "nhì": 2, "hai": 2, "ba": 3, "tư": 4, "bốn": 4, "năm": 5,
               "sáu": 6, "bảy": 7, "tám": 8, "chín": 9, "mười": 10,
               "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
_RANK_N = r"(?P<n>\d+|nhất|nhì|hai|ba|tư|bốn|năm|sáu|bảy|tám|chín|mười|first|second|third|fourth|fifth)"
_RANK_CLAIMS = (
    re.compile(r"(?<!\w)(?:đứng|xếp(?:\s+hạng)?|hạng|vị\s+trí)\s+(?:thứ\s+|số\s+)?"
               + _RANK_N + r"(?!\w)", re.I),
    re.compile(r"(?<!\w)rank(?:ed|s)?\s+(?:no\.\s*|number\s+|#)?" + _RANK_N + r"(?!\w)", re.I),
    re.compile(r"(?<!\w)(?P<n>\d+(?:st|nd|rd|th)|first|second|third|fourth|fifth)\s+place(?!\w)", re.I),
)
# "A dẫn đầu" claims rank 1; "video dẫn đầu" / "kênh dẫn đầu" only names the leader.
_LEADER_CLAIM = re.compile(
    r"(?<!video\s)(?<!kênh\s)(?<!\w)(?:dẫn\s+đầu|đứng\s+đầu)(?!\w)"
    r"|(?<!\w)(?:leads|is\s+leading|is\s+the\s+leader)(?!\w)", re.I)
# Fields that describe a relation between two entities, so they may be quoted
# next to either one ("A cao hơn B 290 view/giờ").
_RELATIONAL_FIELDS = {"pct_of_leader", "gap_to_leader"}


@dataclass(frozen=True)
class GroundingReport:
    ok: bool
    errors: tuple[str, ...]
    numbers_checked: int


# =============================================================================
# Parsing the answer
# =============================================================================

def number_readings(token: str) -> set[Decimal]:
    """All plausible values of a number token, without scale or percent."""
    dots, commas = token.count("."), token.count(",")
    if not dots and not commas:
        return {Decimal(token)}
    readings: set[Decimal] = set()

    def add(text: str) -> None:
        try:
            readings.add(Decimal(text))
        except InvalidOperation:
            pass

    if dots and commas:
        # The separator that comes last is the decimal mark.
        if token.rfind(".") > token.rfind(","):
            add(token.replace(",", ""))                       # 1,234,567.89
        else:
            add(token.replace(".", "").replace(",", "."))     # 1.234.567,89
        return readings

    sep = "." if dots else ","
    parts = token.split(sep)
    groups_ok = all(len(p) == 3 for p in parts[1:]) and 1 <= len(parts[0]) <= 3
    if len(parts) > 2:                                        # 1.234.567 / 1,234,567
        if groups_ok:
            add("".join(parts))
        return readings
    if groups_ok:                                             # 1.234 / 1,234: ambiguous
        add("".join(parts))
    add(f"{parts[0]}.{parts[1]}")                             # decimal reading
    return readings


@dataclass(frozen=True)
class NumberMention:
    raw: str
    readings: frozenset[Decimal]
    is_percent: bool
    scaled: bool                 # had K/M/B or nghìn/triệu/tỷ
    start: int = -1              # offset in the text


def extract_numbers(text: str) -> list[NumberMention]:
    out = []
    for m in _NUMBER.finditer(text):
        base = number_readings(m.group("num"))
        if not base:
            continue
        suffix = (m.group("attached") or m.group("word") or "").lower()
        mult = _MULTIPLIERS.get(suffix, 1)
        out.append(NumberMention(raw=m.group(0).strip(),
                                 readings=frozenset(b * mult for b in base),
                                 is_percent=bool(m.group("pct")), scaled=mult != 1,
                                 start=m.start()))
    return out


# =============================================================================
# Facts available from the tool results
# =============================================================================

EntityKey = tuple[str, str]          # ("video", video_id) or ("channel", channel_id)


@dataclass
class EntityFacts:
    """Values from the rows that describe one video or one channel."""
    label: str
    numbers: list[Decimal] = field(default_factory=list)
    exact_ints: set[int] = field(default_factory=set)
    ranks: set[int] = field(default_factory=set)
    relational: list[Decimal] = field(default_factory=list)


@dataclass
class Facts:
    numbers: list[Decimal]
    exact_ints: set[int]          # ranks, row counts, integer fields
    video_ids: set[str]
    channel_ids: set[str]
    dates: set[str]               # YYYY-MM-DD, UTC and Vietnam time
    times: set[str]               # HH:MM, UTC and Vietnam time
    needs_gap: list[str]          # reasons data_gaps must be filled
    entities: dict[EntityKey, EntityFacts] = field(default_factory=dict)
    names: dict[str, EntityKey] = field(default_factory=dict)    # title -> entity
    # Values that belong to no video/channel row: row counts, coverage rows, extras.
    unowned_numbers: list[Decimal] = field(default_factory=list)
    unowned_ints: set[int] = field(default_factory=set)


def _iter_numbers(value, key=None):
    """(field name, value) for every quotable number inside value."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _iter_numbers(v, k)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_numbers(v, key)
    elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        if not (key and str(key).endswith("_id")):
            yield key, Decimal(str(value))


def _add_number(numbers: list, ints: set, d: Decimal) -> None:
    numbers.append(d)
    if d == d.to_integral_value():
        ints.add(int(d))


def _row_entities(row: dict) -> list[EntityKey]:
    keys = []
    if isinstance(row.get("video_id"), str):
        keys.append(("video", row["video_id"]))
    if isinstance(row.get("channel_id"), str):
        keys.append(("channel", row["channel_id"]))
    return keys


def _collect_entities(rows, facts: Facts) -> None:
    for row in rows:
        keys = _row_entities(row)
        if not keys:
            for _, d in _iter_numbers(row):
                _add_number(facts.unowned_numbers, facts.unowned_ints, d)
            continue
        if row.get("video_title") and isinstance(row.get("video_id"), str):
            facts.names.setdefault(str(row["video_title"]), ("video", row["video_id"]))
        if row.get("channel_title") and isinstance(row.get("channel_id"), str):
            facts.names.setdefault(str(row["channel_title"]), ("channel", row["channel_id"]))
        for key in keys:
            title = row.get("video_title" if key[0] == "video" else "channel_title")
            ef = facts.entities.setdefault(key, EntityFacts(label=f"{key[0]} {title or key[1]!r}"))
            for name, d in _iter_numbers(row):
                _add_number(ef.numbers, ef.exact_ints, d)
                if name == "rank" and d == d.to_integral_value():
                    ef.ranks.add(int(d))
                if name in _RELATIONAL_FIELDS:
                    ef.relational.append(d)


def _walk(value, key, facts: Facts) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _walk(v, k, facts)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _walk(v, key, facts)
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, (int, float, Decimal)):
        if key and str(key).endswith("_id"):
            return                            # ids are not quotable quantities
        d = Decimal(str(value))
        facts.numbers.append(d)
        if isinstance(value, int) or d == d.to_integral_value():
            facts.exact_ints.add(int(d))
    elif isinstance(value, str):
        if key == "video_id":
            facts.video_ids.add(value)
        if key == "channel_id":
            facts.channel_ids.add(value)
        ts = _parse_ts(value)
        if ts:
            for t in (ts, ts + VN_OFFSET):
                facts.dates.add(t.strftime("%Y-%m-%d"))
                facts.times.add(t.strftime("%H:%M"))


def _parse_ts(text: str) -> datetime | None:
    if not re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def collect_facts(results) -> Facts:
    """results: ToolResult objects (or their to_llm() dicts) from this turn."""
    facts = Facts([], set(), set(), set(), set(), set(), [])
    for r in results:
        d = r.to_llm() if hasattr(r, "to_llm") else r
        name = d.get("tool", "?")
        if "error" in d:
            facts.needs_gap.append(f"{name} failed ({d['error']})")
            continue
        facts.exact_ints.add(int(d.get("row_count", 0)))
        _add_number(facts.unowned_numbers, facts.unowned_ints, Decimal(int(d.get("row_count", 0))))
        if d.get("row_count", 0) == 0:
            facts.needs_gap.append(f"{name} returned no rows")
        for row in d.get("rows", []):
            reason = row.get("validity_reason")
            if reason not in (None, "ok"):
                facts.needs_gap.append(f"{row.get('video_id')}: {reason}")
        _walk({k: v for k, v in d.items() if k not in ("tool", "source")}, None, facts)
        _collect_entities(d.get("rows", []), facts)
        for _, n in _iter_numbers(d.get("extra", {})):
            _add_number(facts.unowned_numbers, facts.unowned_ints, n)
    return facts


# =============================================================================
# Matching
# =============================================================================

def value_matches(n: Decimal, v: Decimal) -> bool:
    """n (from the answer) is v (from a tool), allowing honest rounding."""
    if v == 0:
        return n == 0
    if abs(n - v) <= abs(v) * REL_TOLERANCE:
        return True
    for d in (0, 1, 2):
        if v.quantize(Decimal(1).scaleb(-d)) == n:
            return True
    for k in (3, 6, 9):                       # "1,2 triệu" for 1,234,567
        scale = Decimal(10) ** k
        for d in (0, 1, 2):
            if (v / scale).quantize(Decimal(1).scaleb(-d)) * scale == n:
                return True
    return False


def _grounded_in(m: NumberMention, numbers, exact_ints) -> bool:
    for n in m.readings:
        is_small_int = (not m.is_percent and not m.scaled
                        and n == n.to_integral_value() and abs(n) <= SMALL_INT_MAX)
        if is_small_int:
            if int(n) in exact_ints:
                return True
            continue
        for v in numbers:
            if value_matches(n, v) or (m.is_percent and value_matches(n, v * 100)):
                return True
    return False


# =============================================================================
# Which entity a sentence is about
# =============================================================================

@dataclass(frozen=True)
class _Mention:
    start: int
    key: EntityKey


class _Sentences:
    """Entity mentions grouped by sentence, for binding numbers and ranks."""

    def __init__(self, text: str, mentions: list[_Mention]):
        self.ends = [m.end() for m in _SENTENCE_END.finditer(text)]
        self.mentions = sorted(mentions, key=lambda x: x.start)

    def _sentence(self, pos: int) -> int:
        return bisect.bisect_right(self.ends, pos)

    def in_sentence(self, pos: int) -> list[_Mention]:
        s = self._sentence(pos)
        return [m for m in self.mentions if self._sentence(m.start) == s]

    def bound(self, pos: int) -> EntityKey | None:
        """The nearest video named before pos in its sentence, else after it;
        channels only when the sentence names no video."""
        here = self.in_sentence(pos)
        for kind in ("video", "channel"):
            cands = [m for m in here if m.key[0] == kind]
            before = [m for m in cands if m.start < pos]
            if before:
                return before[-1].key
            after = [m for m in cands if m.start > pos]
            if after:
                return after[0].key
        return None


def _blank(match_text: str) -> str:
    """Same-length blank, so offsets stay valid and the digits are not re-read."""
    return " " * len(match_text)


def _find_names(text: str, facts: Facts, mentions: list[_Mention]) -> str:
    """Record and blank every video/channel title a tool returned, longest first."""
    taken: list[tuple[int, int]] = []
    for name in sorted(facts.names, key=len, reverse=True):
        flags = re.I if len(name) >= 4 else 0     # 'A' must not match the article 'a'
        for m in re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, flags):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            mentions.append(_Mention(m.start(), facts.names[name]))
    for s, e in taken:
        text = text[:s] + _blank(text[s:e]) + text[e:]
    return text


def _rank_claims(text: str) -> list[tuple[int, int, str]]:
    """(offset, claimed rank, raw phrase) for every rank claim in the text."""
    out = []
    for rx in _RANK_CLAIMS:
        for m in rx.finditer(text):
            n = m.group("n").lower()
            digits = re.match(r"\d+", n)
            out.append((m.start(), int(digits.group(0)) if digits else _RANK_WORDS[n], m.group(0)))
    out += [(m.start(), 1, m.group(0)) for m in _LEADER_CLAIM.finditer(text)]
    return out


# =============================================================================
# Entry point
# =============================================================================

def ground(answer: str, cited_video_ids, data_gaps, results, *,
           status: str = "answered") -> GroundingReport:
    facts = collect_facts(results)
    errors: list[str] = []
    text = answer or ""

    if _URL.search(text):
        errors.append("answer contains a URL; links are added by the system")

    for vid in cited_video_ids or []:
        if vid not in facts.video_ids:
            errors.append(f"cited video_id {vid!r} was not returned by any tool")

    # Titles, ids, dates and times are checked, recorded as entity mentions where
    # they name one, then blanked (same length) so their digits are not read as numbers.
    entity_mentions: list[_Mention] = []
    text = _find_names(text, facts, entity_mentions)

    def _check_channel(m: re.Match) -> str:
        if m.group(0) not in facts.channel_ids:
            errors.append(f"channel_id {m.group(0)!r} was not returned by any tool")
        elif ("channel", m.group(0)) in facts.entities:
            entity_mentions.append(_Mention(m.start(), ("channel", m.group(0))))
        return _blank(m.group(0))
    text = _CHANNEL_ID.sub(_check_channel, text)

    def _check_video(m: re.Match) -> str:
        token = m.group(0)
        looks_like_id = (any(c.isdigit() for c in token) or "_" in token or "-" in token
                         or any(c.isupper() for c in token[1:]))
        if not looks_like_id:
            return token                            # an ordinary 11-letter word
        if token not in facts.video_ids:
            errors.append(f"video_id {token!r} in the answer was not returned by any tool")
        elif ("video", token) in facts.entities:
            entity_mentions.append(_Mention(m.start(), ("video", token)))
        return _blank(token)
    text = _VIDEO_ID.sub(_check_video, text)

    def _check_date(y: int, mo: int, d: int, raw: str) -> str:
        if f"{y:04d}-{mo:02d}-{d:02d}" not in facts.dates:
            errors.append(f"date {raw!r} does not appear in the tool results")
        return _blank(raw)
    text = _ISO_DATE.sub(lambda m: _check_date(int(m[1]), int(m[2]), int(m[3]), m[0]), text)
    text = _DMY_DATE.sub(lambda m: _check_date(int(m[3]), int(m[2]), int(m[1]), m[0]), text)

    def _check_dm(m: re.Match) -> str:
        d, mo = int(m[1]), int(m[2])
        if not any(x.endswith(f"-{mo:02d}-{d:02d}") for x in facts.dates):
            errors.append(f"date {m[0]!r} does not appear in the tool results")
        return _blank(m[0])
    text = _DM_DATE.sub(_check_dm, text)

    def _check_time(m: re.Match) -> str:
        if f"{int(m[1]):02d}:{m[2]}" not in facts.times:
            errors.append(f"time {m[0]!r} does not appear in the tool results")
        return _blank(m[0])
    text = _TIME.sub(_check_time, text)

    sentences = _Sentences(text, entity_mentions)

    mentions = extract_numbers(text)
    for m in mentions:
        key = sentences.bound(m.start)
        if key is None:
            if not _grounded_in(m, facts.numbers, facts.exact_ints):
                errors.append(f"number {m.raw!r} does not match any tool result")
            continue
        ef = facts.entities[key]
        relational = [v for x in sentences.in_sentence(m.start)
                      for v in facts.entities[x.key].relational]
        if (_grounded_in(m, ef.numbers, ef.exact_ints)
                or _grounded_in(m, facts.unowned_numbers, facts.unowned_ints)
                or _grounded_in(m, relational, set())):
            continue
        if _grounded_in(m, facts.numbers, facts.exact_ints):
            errors.append(f"number {m.raw!r} is written about {ef.label} but comes "
                          "from another video or channel")
        else:
            errors.append(f"number {m.raw!r} does not match any tool result")

    all_ranks = {r for ef in facts.entities.values() for r in ef.ranks}
    for pos, rank, raw in _rank_claims(text):
        key = sentences.bound(pos)
        ranks = facts.entities[key].ranks if key else all_ranks
        if rank not in ranks:
            who = facts.entities[key].label if key else "a video or channel"
            have = f"its rank is {sorted(ranks)}" if ranks else "no tool returned a rank"
            errors.append(f"{raw!r} claims rank {rank} for {who}, but {have}")

    if status != "declined" and facts.needs_gap and not [g for g in (data_gaps or []) if g.strip()]:
        errors.append("data_gaps is empty but some data is missing: "
                      + "; ".join(facts.needs_gap[:5]))

    return GroundingReport(ok=not errors, errors=tuple(errors), numbers_checked=len(mentions))
