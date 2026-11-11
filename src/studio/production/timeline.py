"""
Timeline of an AI-film episode (F-06). Pure functions, no I/O.

The script is the source of truth for every cut: shot and line timings come from the
REAL audio durations (TTS), not from estimates (spec §10.2), and the Short is the shots
the Writer marked `in_short` - code cuts them, nothing has to "watch" the video.

    lines' audio seconds --build_timeline--> spans (start/end of every shot and line)
                         --short_shots-----> which shots make the vertical Short
                         --to_ass / to_srt-> subtitles on the same clock
    episode durations    --chapters------> YouTube chapters of the long cut
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from studio.schemas import FilmEpisode, Shot

LEAD = 0.4          # silence before a shot's first line
GAP = 0.25          # between two lines of the same shot
TAIL = 0.6          # after the last line, before the cut
MIN_SHOT = 2.0      # a shot is never shorter than this
SHORT_MAX = 90.0    # spec §5 (D9): a film Short is at most 90 seconds


@dataclass(frozen=True)
class LineSpan:
    shot_id: str
    index: int
    speaker: str
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class ShotSpan:
    shot: Shot
    start: float
    end: float
    lines: tuple[LineSpan, ...] = ()

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


@dataclass(frozen=True)
class Timeline:
    shots: tuple[ShotSpan, ...] = field(default_factory=tuple)

    @property
    def duration(self) -> float:
        return self.shots[-1].end if self.shots else 0.0

    @property
    def lines(self) -> list[LineSpan]:
        return [ln for s in self.shots for ln in s.lines]


def build_timeline(shots: list[Shot], line_seconds: dict[tuple[str, int], float], *,
                   fps: int | None = None) -> Timeline:
    """line_seconds[(shot_id, line_index)] = length of that line's audio.

    With `fps`, every shot lasts a whole number of frames, so a video built clip by clip
    stays on the same clock as the audio and subtitles (no drift across shots)."""
    t, spans = 0.0, []
    for shot in shots:
        start, cursor, lines = t, t + LEAD, []
        for i, line in enumerate(shot.lines):
            secs = line_seconds[(shot.shot_id, i)]
            lines.append(LineSpan(shot.shot_id, i, line.speaker, line.text,
                                  round(cursor, 3), round(cursor + secs, 3)))
            cursor += secs + GAP
        if shot.lines:
            end = cursor - GAP + TAIL
        else:
            end = start + shot.hold_seconds
        end = max(end, start + MIN_SHOT)
        if fps:
            end = start + math.ceil(round((end - start) * fps, 6)) / fps
        spans.append(ShotSpan(shot, round(start, 3), round(end, 3), tuple(lines)))
        t = end
    return Timeline(tuple(spans))


def short_shots(episode: FilmEpisode, timeline: Timeline,
                max_seconds: float = SHORT_MAX) -> tuple[list[Shot], list[str]]:
    """The shots of the vertical Short, and every reason it cannot be made.

    A Short must open on the hook (it has no episode around it to set things up)
    and fit YouTube's length; the cut keeps script order."""
    picked = [s for s in episode.shots if s.in_short]
    errors = []
    if not picked:
        errors.append("no shot is marked in_short")
    elif picked[0].kind != "hook":
        errors.append(f"the Short must open on a hook shot, not {picked[0].shot_id!r}")
    seconds = sum(s.duration for s in timeline.shots if s.shot.in_short)
    if seconds > max_seconds:
        errors.append(f"the Short would last {seconds:.1f} s, more than {max_seconds:.0f} s")
    return picked, errors


# ---- subtitles ---------------------------------------------------------------------------
SUB_CHARS_WIDE = 84     # about two lines on a 16:9 frame
SUB_CHARS_TALL = 52     # about two lines on a 9:16 frame


def split_caption(text: str, max_chars: int) -> list[str]:
    """Break a long line into captions of at most `max_chars` (two screen lines). Whole
    sentences are kept together when they fit; a sentence that is itself too long is cut
    at commas, then between words. Never splits a word."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return [text]
    sentences = [text]
    for sep in (". ", "? ", "! "):
        sentences = [q for piece in sentences for q in _split_keep(piece, sep)]
    out, cur = [], ""
    for sen in sentences:
        if len(sen) > max_chars:
            out += [cur] if cur else []
            cur = ""
            clauses = [sen]
            for sep in (", ", "; ", ": "):
                clauses = [q for piece in clauses for q in _split_keep(piece, sep)]
            atoms = [w for c in clauses
                     for w in ([c] if len(c) <= max_chars else _greedy(c.split(), max_chars))]
            out += _greedy(atoms, max_chars)
        elif cur and len(cur) + 1 + len(sen) > max_chars:
            out.append(cur)
            cur = sen
        else:
            cur = f"{cur} {sen}".strip()
    return out + [cur] if cur else out


def _split_keep(text: str, sep: str) -> list[str]:
    bits = text.split(sep)
    return [b + sep.rstrip() for b in bits[:-1]] + [bits[-1]]


def _greedy(atoms: list[str], max_chars: int) -> list[str]:
    out, cur = [], ""
    for a in atoms:
        if cur and len(cur) + 1 + len(a) > max_chars:
            out.append(cur)
            cur = a
        else:
            cur = f"{cur} {a}".strip()
    return out + [cur] if cur else out


def captions(timeline: Timeline, max_chars: int) -> list[tuple[float, float, str, str]]:
    """(start, end, speaker, text) on screen: each spoken line split into readable captions,
    each shown for a share of the line's time proportional to its length."""
    out = []
    for ln in timeline.lines:
        parts = split_caption(ln.text, max_chars)
        total = sum(len(p) for p in parts)
        t = ln.start
        for p in parts:
            end = t + (ln.end - ln.start) * len(p) / total
            out.append((round(t, 3), round(end, 3), ln.speaker, p))
            t = end
    return out


def _ts_srt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ts_ass(t: float) -> str:
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def to_srt(timeline: Timeline, max_chars: int = SUB_CHARS_WIDE) -> str:
    blocks = [f"{i}\n{_ts_srt(a)} --> {_ts_srt(b)}\n{text}\n"
              for i, (a, b, _, text) in enumerate(captions(timeline, max_chars), start=1)]
    return "\n".join(blocks)


def _ass_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def to_ass(timeline: Timeline, *, width: int, height: int, font: str = "DejaVu Sans") -> str:
    """Dialogue at the bottom (raised on vertical video, above the Shorts buttons);
    the narrator in italics; on-screen text (place, year) at the top of its shot."""
    vertical = height > width
    size = round(height * (0.034 if vertical else 0.05))
    margin_v = round(height * (0.2 if vertical else 0.06))
    margin_h = round(width * 0.08)
    head = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\nPlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Line,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H64000000,"
        f"1,0,0,0,100,100,0,0,1,{max(2, size // 16)},1,2,{margin_h},{margin_h},{margin_v},1\n"
        f"Style: Narrator,{font},{size},&H00B8EEFF,&H00B8EEFF,&H00101010,&H64000000,"
        f"0,1,0,0,100,100,0,0,1,{max(2, size // 16)},1,2,{margin_h},{margin_h},{margin_v},1\n"
        f"Style: Place,{font},{round(size * 0.8)},&H00FFFFFF,&H00FFFFFF,&H00101010,&H64000000,"
        f"1,0,0,0,100,100,2,0,1,{max(2, size // 16)},1,8,{margin_h},{margin_h},"
        f"{round(height * (0.12 if vertical else 0.06))},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for span in timeline.shots:
        if span.shot.on_screen_text:
            events.append(f"Dialogue: 1,{_ts_ass(span.start + 0.2)},{_ts_ass(span.end - 0.2)},"
                          f"Place,,0,0,0,,{_ass_text(span.shot.on_screen_text)}")
    for a, b, speaker, text in captions(timeline, SUB_CHARS_TALL if vertical else SUB_CHARS_WIDE):
        style = "Narrator" if speaker == "narrator" else "Line"
        events.append(f"Dialogue: 0,{_ts_ass(a)},{_ts_ass(b)},{style},"
                      f"{speaker},0,0,0,,{_ass_text(text)}")
    return head + "\n".join(events) + "\n"


# ---- the long cut ------------------------------------------------------------------------
def _ts_chapter(t: float) -> str:
    s = int(t)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def chapters(episodes: list[tuple[str, float]]) -> str:
    """YouTube chapters for the long cut: the first must be 00:00 and each at least 10 s."""
    lines, t = [], 0.0
    for title, seconds in episodes:
        lines.append(f"{_ts_chapter(t)} {title}")
        t += seconds
    return "\n".join(lines)
