"""
An AI-film episode script before gate 2: code checks, and a readable screenplay.

`check_film_script` is the part of the Checker that needs no model: structure, speakers,
voice-readable text, banned names, the Short's opening. The LLM originality check (F-04)
comes on top of it, not instead. `screenplay` prints the script the way a person reads
one, so the owner approves the story, not YAML.
"""

from __future__ import annotations

from studio.production.voice import speakable
from studio.schemas import FilmEpisode, StoryBible

KIND_VI = {"hook": "MỞ ĐẦU", "scene": "CẢNH", "cliffhanger": "KẾT TREO", "end_card": "MÀN KẾT"}


def check_film_script(bible: StoryBible, episode: FilmEpisode,
                      series_id: str | None = None, number: int | None = None) -> list[str]:
    """Every problem found; empty means the script may go to the owner."""
    problems = []
    if episode.series_id != bible.series_id:
        problems.append(f"series_id {episode.series_id!r} is not the bible's {bible.series_id!r}")
    if series_id and episode.series_id != series_id:
        problems.append(f"the job is series {series_id!r}, the script says {episode.series_id!r}")
    if number and episode.number != number:
        problems.append(f"the job is episode {number}, the script says {episode.number}")
    if not episode.shots or episode.shots[0].kind != "hook":
        problems.append("the episode must open on a hook shot")
    ids = [s.shot_id for s in episode.shots]
    problems += [f"shot_id {i!r} is used twice" for i in sorted({i for i in ids if ids.count(i) > 1})]
    known = {c.id for c in bible.characters}
    for s in episode.shots:
        for c in s.characters:
            if c not in known:
                problems.append(f"{s.shot_id}: character {c!r} is not in the bible")
        for ln in s.lines:
            if ln.speaker != "narrator" and ln.speaker not in known:
                problems.append(f"{s.shot_id}: speaker {ln.speaker!r} is not in the bible")
            try:
                speakable(ln.text)
            except ValueError:
                problems.append(f"{s.shot_id}: spell numbers out for the voice: {ln.text!r}")
    text = " ".join([episode.title] + [ln.text for s in episode.shots for ln in s.lines]
                    + [s.on_screen_text or "" for s in episode.shots]).lower()
    problems += [f"uses a banned name: {n!r}" for n in bible.banned_names if n.lower() in text]
    short = [s for s in episode.shots if s.in_short]
    if not short:
        problems.append("no shot is marked in_short: the Short would be empty")
    elif short[0].kind != "hook":
        problems.append(f"the Short must open on a hook shot, not {short[0].shot_id!r}")
    return problems


def screenplay(bible: StoryBible, episode: FilmEpisode) -> str:
    """The script as a person reads it: scene heading, what we see, who says what."""
    names = {c.id: c.name for c in bible.characters} | {"narrator": "Người dẫn"}
    out = [f"{bible.title.upper()} - TẬP {episode.number}: {episode.title}",
           f"Tóm tắt bộ phim: {bible.logline}", ""]
    for i, s in enumerate(episode.shots, start=1):
        tag = " [vào Short]" if s.in_short else ""
        out.append(f"{i}. {KIND_VI.get(s.kind, s.kind)} - {s.place}{tag}")
        out.append(f"   Hình: {s.image_prompt}")
        if s.on_screen_text:
            out.append(f"   Chữ trên màn hình: {s.on_screen_text}")
        for ln in s.lines:
            out.append(f"   {names.get(ln.speaker, ln.speaker)}: {ln.text}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
