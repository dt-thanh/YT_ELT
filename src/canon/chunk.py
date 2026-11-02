"""
Cut a source text into passages along its NATURAL units (spec §9, C-02): book.section
for Marcus Aurelius, chapter for the Dao De Jing, one fable per passage, ...

A source declares its heading levels as regexes (config/canon_sources.yaml):

    levels:
      - {name: book, pattern: '^THE (\\w+) BOOK$', value: ordinal}
      - {name: item, pattern: '^([IVXLC]+)\\. ', value: roman, inline: true}

Lines are scanned once. A match on a level sets its number and resets the levels
below; a match on the LAST level starts a new passage (with `inline: true` the rest
of that line is the passage's first words). Text between headings belongs to the
open passage. The ref is the level numbers joined with dots ("4.3").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ORDINALS = {w: i for i, w in enumerate(
    "FIRST SECOND THIRD FOURTH FIFTH SIXTH SEVENTH EIGHTH NINTH TENTH ELEVENTH TWELFTH "
    "THIRTEENTH FOURTEENTH FIFTEENTH SIXTEENTH SEVENTEENTH EIGHTEENTH NINETEENTH TWENTIETH"
    .split(), start=1)}
ORDINALS.update({w: i for i, w in enumerate(
    "ONE TWO THREE FOUR FIVE SIX SEVEN EIGHT NINE TEN ELEVEN TWELVE THIRTEEN FOURTEEN "
    "FIFTEEN SIXTEEN SEVENTEEN EIGHTEEN NINETEEN TWENTY".split(), start=1)})
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
_HAN = {"〇": 0, "零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6,
        "七": 7, "八": 8, "九": 9}
_FOOTNOTE = re.compile(r"\[\d+\]|\{\d+\}|\[[A-Z]\]")


class ChunkError(ValueError):
    pass


def roman(s: str) -> int:
    s = s.upper().strip()
    if not s or any(c not in _ROMAN for c in s):
        raise ChunkError(f"not a roman numeral: {s!r}")
    total = 0
    for i, c in enumerate(s):
        v = _ROMAN[c]
        total += -v if i + 1 < len(s) and _ROMAN[s[i + 1]] > v else v
    return total


def han_number(s: str) -> int:
    """一 .. 九十九 and 百 (enough for chapter numbers)."""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if len(s) > 1 and "十" not in s and "百" not in s and all(c in _HAN for c in s):
        return int("".join(str(_HAN[c]) for c in s))     # digit style: 二一 = 21, 一〇 = 10
    total, cur = 0, 0
    for ch in s:
        if ch in _HAN:
            cur = _HAN[ch]
        elif ch == "十":
            total += (cur or 1) * 10
            cur = 0
        elif ch == "百":
            total += (cur or 1) * 100
            cur = 0
        else:
            raise ChunkError(f"not a Chinese numeral: {s!r}")
    return total + cur


def to_number(raw: str, kind: str) -> int:
    if kind == "int":
        return int(raw)
    if kind == "roman":
        return roman(raw)
    if kind == "ordinal":
        try:
            return ORDINALS[raw.upper().strip()]
        except KeyError:
            raise ChunkError(f"not an ordinal word: {raw!r}") from None
    if kind == "han":
        return han_number(raw)
    raise ChunkError(f"unknown value kind {kind!r}")


@dataclass(frozen=True)
class Level:
    name: str
    pattern: re.Pattern
    value: str = "int"          # int | roman | ordinal | han | sequence
    inline: bool = False        # the rest of the heading line starts the passage text
    title_group: int | None = None   # capture group holding a title (fables)
    after_blank_lines: int = 0  # only a heading when at least N blank lines precede it
    implicit_first: bool = False     # last level: text right after a parent heading is #1
    range_group: int | None = None   # group with the end of a merged unit ('5, 6.' -> 5-6)


@dataclass
class Passage:
    numbers: dict[str, int | str]          # the last level may be a range: "5-6"
    text: str
    title: str | None = None

    @property
    def ref(self) -> str:
        return ".".join(str(v) for v in self.numbers.values())


@dataclass
class _Open:
    numbers: dict[str, int]
    lines: list[str] = field(default_factory=list)
    title: str | None = None
    implicit: bool = False               # opened by implicit_first, not by a heading


def levels_from_config(raw: list[dict]) -> list[Level]:
    out = []
    for lv in raw:
        out.append(Level(lv["name"], re.compile(lv["pattern"]), lv.get("value", "int"),
                         bool(lv.get("inline", False)), lv.get("title_group"),
                         int(lv.get("after_blank_lines", 0)), bool(lv.get("implicit_first")),
                         lv.get("range_group")))
    if not out:
        raise ChunkError("a parser needs at least one level")
    return out


def clean_lines(text: str, *, start_after: str | None = None,
                end_before: str | None = None) -> list[str]:
    """Gutenberg header/footer off, then the source's own start/end markers."""
    lines = text.replace("\r\n", "\n").split("\n")
    s = next((i + 1 for i, ln in enumerate(lines) if "*** START OF" in ln), 0)
    e = next((i for i, ln in enumerate(lines) if "*** END OF" in ln), len(lines))
    lines = lines[s:e]
    if start_after:
        idx = next((i for i, ln in enumerate(lines) if re.search(start_after, ln)), None)
        if idx is None:
            raise ChunkError(f"start marker {start_after!r} not found")
        lines = lines[idx + 1:]                       # the marker line itself is not text
    if end_before:
        idx = next((i for i, ln in enumerate(lines) if re.search(end_before, ln)), None)
        lines = lines[:idx] if idx is not None else lines
    return lines


_WIDE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


def _glue(parts: list[str]) -> str:
    """Join wrapped lines: a space between words, nothing between Chinese characters."""
    out = ""
    for p in parts:
        if out and not (_WIDE.match(out[-1]) or _WIDE.match(p[0])):
            out += " "
        out += p
    return out


def _join(lines: list[str]) -> str:
    """Hard-wrapped lines -> paragraphs; paragraphs separated by one newline."""
    paras, cur = [], []
    for ln in lines:
        if ln.strip():
            cur.append(ln.strip())
        elif cur:
            paras.append(_glue(cur))
            cur = []
    if cur:
        paras.append(_glue(cur))
    text = "\n".join(paras)
    text = _FOOTNOTE.sub("", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def prepare(lines: list[str], *, drop_indented: int | None = None,
            drop_lines: str | None = None, strip_line_prefix: str | None = None) -> list[str]:
    """Per-source cleanup before splitting: indented footnote blocks, commentary lines,
    verse numbers at the start of a line."""
    out = []
    drop_rx = re.compile(drop_lines) if drop_lines else None
    strip_rx = re.compile(strip_line_prefix) if strip_line_prefix else None
    for ln in lines:
        if drop_indented and ln.strip() and len(ln) - len(ln.lstrip(" ")) >= drop_indented:
            continue
        if drop_rx and drop_rx.search(ln):
            continue
        out.append(strip_rx.sub("", ln.strip()) if strip_rx and ln.strip() else ln)
    return out


def split(lines: list[str], levels: list[Level], *,
          strip_line_prefix: str | None = None) -> list[Passage]:
    counters: dict[str, int] = {}
    out: list[Passage] = []
    current: _Open | None = None
    last = levels[-1]
    strip_rx = re.compile(strip_line_prefix) if strip_line_prefix else None
    blanks = 99                                   # the text start counts as a break

    def close():
        if current is not None:
            text = _join(current.lines)
            if text:
                out.append(Passage(dict(current.numbers), text, current.title))

    for ln in lines:
        stripped = ln.strip()
        if not stripped:
            blanks += 1
            if current is not None:
                current.lines.append(ln)
            continue
        hit = None
        for depth, lv in enumerate(levels):
            m = lv.pattern.search(stripped)
            if m and blanks >= lv.after_blank_lines:
                hit = (depth, lv, m)
                break
        blanks = 0
        if hit is None:
            if current is not None:
                current.lines.append(strip_rx.sub("", stripped) if strip_rx else ln)
            continue
        depth, lv, m = hit
        empty_open = (lv is last and current is not None
                      and not "".join(current.lines).strip())
        if lv.value == "sequence" and empty_open:
            pass                                  # a blank line after the heading: same unit
        elif lv.value == "sequence":
            counters[lv.name] = counters.get(lv.name, 0) + 1
        else:
            counters[lv.name] = to_number(m.group(1), lv.value)
        for lower in levels[depth + 1:]:              # a new book restarts its sections
            counters.pop(lower.name, None)
        if lv is last and current is not None and current.implicit and \
                lv.value != "sequence" and \
                current.numbers == {x.name: counters.get(x.name) for x in levels}:
            # an explicit '1.' right where implicit_first already opened section 1
            if lv.inline:
                rest = stripped[m.end():]
                current.lines.append(strip_rx.sub("", rest) if strip_rx else rest)
            continue
        if lv is last:
            close()
            missing = [x.name for x in levels if x.name not in counters]
            if missing:
                raise ChunkError(f"passage before any {missing} heading: {stripped[:60]!r}")
            title = m.group(lv.title_group).strip() if lv.title_group else None
            numbers = {x.name: counters[x.name] for x in levels}
            if lv.range_group and m.group(lv.range_group):
                numbers[lv.name] = f"{counters[lv.name]}-{to_number(m.group(lv.range_group), lv.value)}"
            current = _Open(numbers, [], title)
            if lv.inline:
                rest = stripped[m.end():]
                current.lines.append(strip_rx.sub("", rest) if strip_rx else rest)
        else:
            close()
            current = None
            if last.implicit_first and depth == len(levels) - 2:
                counters[last.name] = 1           # e.g. Long's Meditations: section 1 has no number
                current = _Open({x.name: counters[x.name] for x in levels}, [], implicit=True)
    close()
    _mark_silent_gaps(out, last)
    refs = [p.ref for p in out]
    dupes = sorted({r for r in refs if refs.count(r) > 1})
    if dupes:
        raise ChunkError(f"duplicate refs {dupes[:5]} - the heading patterns are ambiguous")
    return out


def _first(v) -> int:
    return int(str(v).split("-")[0])


def _last(v) -> int:
    return int(str(v).split("-")[-1])


def _mark_silent_gaps(out: list[Passage], last: Level) -> None:
    """Numbered units that skip (4 then 6) mean the source lost a marker: the earlier
    passage holds both, so its label becomes a range ("4-5") instead of a wrong '4'."""
    if last.value == "sequence":
        return
    for a, b in zip(out, out[1:]):
        parent_a = [v for k, v in a.numbers.items() if k != last.name]
        parent_b = [v for k, v in b.numbers.items() if k != last.name]
        if parent_a == parent_b and _first(b.numbers[last.name]) > _last(a.numbers[last.name]) + 1:
            a.numbers[last.name] = f"{_first(a.numbers[last.name])}-{_first(b.numbers[last.name]) - 1}"


def ref_label(template: str, p: Passage) -> str:
    """'Suy tưởng, quyển {book}, đoạn {item}' -> 'Suy tưởng, quyển 4, đoạn 3'."""
    try:
        return template.format(**p.numbers, title=p.title or "")
    except KeyError as e:
        raise ChunkError(f"ref_label template uses unknown level {e}") from None
