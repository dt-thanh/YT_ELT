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

import re
from dataclasses import dataclass
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
                                 is_percent=bool(m.group("pct")), scaled=mult != 1))
    return out


# =============================================================================
# Facts available from the tool results
# =============================================================================

@dataclass
class Facts:
    numbers: list[Decimal]
    exact_ints: set[int]          # ranks, row counts, integer fields
    video_ids: set[str]
    channel_ids: set[str]
    dates: set[str]               # YYYY-MM-DD, UTC and Vietnam time
    times: set[str]               # HH:MM, UTC and Vietnam time
    needs_gap: list[str]          # reasons data_gaps must be filled


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
        if d.get("row_count", 0) == 0:
            facts.needs_gap.append(f"{name} returned no rows")
        for row in d.get("rows", []):
            reason = row.get("validity_reason")
            if reason not in (None, "ok"):
                facts.needs_gap.append(f"{row.get('video_id')}: {reason}")
        _walk({k: v for k, v in d.items() if k not in ("tool", "source")}, None, facts)
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


def _number_grounded(m: NumberMention, facts: Facts) -> bool:
    for n in m.readings:
        is_small_int = (not m.is_percent and not m.scaled
                        and n == n.to_integral_value() and abs(n) <= SMALL_INT_MAX)
        if is_small_int:
            if int(n) in facts.exact_ints:
                return True
            continue
        for v in facts.numbers:
            if value_matches(n, v) or (m.is_percent and value_matches(n, v * 100)):
                return True
    return False


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

    # Video and channel ids: check, then blank them out so their digits are not
    # read as numbers.
    for vid in cited_video_ids or []:
        if vid not in facts.video_ids:
            errors.append(f"cited video_id {vid!r} was not returned by any tool")

    def _check_channel(m: re.Match) -> str:
        if m.group(0) not in facts.channel_ids:
            errors.append(f"channel_id {m.group(0)!r} was not returned by any tool")
        return " "
    text = _CHANNEL_ID.sub(_check_channel, text)

    def _check_video(m: re.Match) -> str:
        token = m.group(0)
        looks_like_id = (any(c.isdigit() for c in token) or "_" in token or "-" in token
                         or any(c.isupper() for c in token[1:]))
        if not looks_like_id:
            return token                            # an ordinary 11-letter word
        if token not in facts.video_ids:
            errors.append(f"video_id {token!r} in the answer was not returned by any tool")
        return " "
    text = _VIDEO_ID.sub(_check_video, text)

    # Dates and times, then blank them out.
    def _check_date(y: int, mo: int, d: int, raw: str) -> str:
        if f"{y:04d}-{mo:02d}-{d:02d}" not in facts.dates:
            errors.append(f"date {raw!r} does not appear in the tool results")
        return " "
    text = _ISO_DATE.sub(lambda m: _check_date(int(m[1]), int(m[2]), int(m[3]), m[0]), text)
    text = _DMY_DATE.sub(lambda m: _check_date(int(m[3]), int(m[2]), int(m[1]), m[0]), text)

    def _check_dm(m: re.Match) -> str:
        d, mo = int(m[1]), int(m[2])
        if not any(x.endswith(f"-{mo:02d}-{d:02d}") for x in facts.dates):
            errors.append(f"date {m[0]!r} does not appear in the tool results")
        return " "
    text = _DM_DATE.sub(_check_dm, text)

    def _check_time(m: re.Match) -> str:
        if f"{int(m[1]):02d}:{m[2]}" not in facts.times:
            errors.append(f"time {m[0]!r} does not appear in the tool results")
        return " "
    text = _TIME.sub(_check_time, text)

    mentions = extract_numbers(text)
    for m in mentions:
        if not _number_grounded(m, facts):
            errors.append(f"number {m.raw!r} does not match any tool result")

    if status != "declined" and facts.needs_gap and not [g for g in (data_gaps or []) if g.strip()]:
        errors.append("data_gaps is empty but some data is missing: "
                      + "; ".join(facts.needs_gap[:5]))

    return GroundingReport(ok=not errors, errors=tuple(errors), numbers_checked=len(mentions))
