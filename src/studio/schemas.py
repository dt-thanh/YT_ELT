"""
Agent inputs and outputs (spec §7, §11). Every agent speaks JSON validated by these
Pydantic models; code checks them, nothing parses free text.

Only the Research output exists so far (P1). Script, ScenePlan, MediaBundle,
CheckReport and Learning arrive with P3-P6.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Pillar = Literal["stoic", "east", "ai_film"]          # channel pillars (spec D9)
Language = Literal["en", "zh", "vi"]


class ResearchRequest(BaseModel):
    """Spec §7.2 input: pillar, time window (default 14 days), how many briefs."""
    pillar: Pillar
    window_days: int = Field(default=14, ge=1, le=60)
    n_briefs: int = Field(default=3, ge=1, le=10)


class Evidence(BaseModel):
    """One tracked video behind a brief: raw API numbers and when they were read.
    Filled by code from tool results, never by the model. No titles (R3)."""
    video_id: str
    channel_id: str | None = None
    language: Language | None = None
    url: str
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    delta_views: int | None = None                  # per-video growth, "tính độc lập"
    views_per_hour: float | None = None             # per-video growth, "tính độc lập"
    observed_at: datetime | None = None
    s1: float | None = None                         # only with derived metrics
    s1_label: str | None = None


class TopicBrief(BaseModel):
    """Spec §7.2 output. With derived_metrics_enabled = false the three advanced
    fields stay empty: languages_up, vi_saturation, viewer_questions."""
    topic_id: str
    working_title: str = Field(max_length=120)
    pillar: Pillar
    evidence: list[Evidence] = Field(min_length=1)
    languages_up: list[Language] = []
    vi_saturation: Literal["none", "low", "high"] | None = None
    angles_taken: list[str] = []
    proposed_angle: str = Field(max_length=400)
    viewer_questions: list[str] = []
    passage_ids: list[str] = []                     # canon passages (P2); empty until then
    confidence: Literal["low", "medium", "high"]
    derived_metrics: bool
    created_at: datetime


# ---- what the model is allowed to write (structured output) --------------------
class BriefDraft(BaseModel):
    working_title: str = Field(max_length=120)
    video_ids: list[str] = Field(min_length=1, max_length=8)
    angles_taken: list[str] = Field(default=[], max_length=5)
    proposed_angle: str = Field(max_length=400)
    confidence: Literal["low", "medium", "high"]


class BriefDrafts(BaseModel):
    briefs: list[BriefDraft] = Field(max_length=10)


# ---- canon (spec §9, P2): what search_canon / get_passage return ------------------------
class CanonHit(BaseModel):
    passage_id: str
    work_vi: str
    author: str
    translator: str | None
    language: Literal["en", "zh", "la", "fr", "vi"]
    license: Literal["public_domain", "own"]
    ref_label: str
    snippet: str
    rank: int
    score: float
    similarity: float | None
    matched_by: str


class AlignedPassage(BaseModel):
    passage_id: str
    language: str
    translator: str | None
    ref_label: str
    text: str


class CanonPassage(BaseModel):
    """The exact text a script may quote (P3: citations are checked against it)."""
    passage_id: str
    work: str
    work_vi: str
    author: str
    translator: str | None
    pub_year: int
    language: Literal["en", "zh", "la", "fr", "vi"]
    license: Literal["public_domain", "own"]
    source_url: str
    ref: str
    ref_label: str
    text: str
    aligned: list[AlignedPassage] = []


# ---- AI film (spec §5 D9/D10, backlog F-02, F-06) ---------------------------------------
# A series is produced EPISODE by episode; the long cut is the episodes joined with
# chapters, and each episode yields one vertical Short made of the shots marked in_short.
# Written by people for the pilot; by the Writer agent from F-03 on.
Motion = Literal["zoom_in", "zoom_out", "pan_left", "pan_right", "static"]
SLUG = r"^[a-z0-9_]+$"


class Character(BaseModel):
    id: str = Field(pattern=SLUG)
    name: str
    look: str                                   # what an image model must keep the same
    pitch: float = Field(default=1.0, ge=0.7, le=1.4)   # pilot voice: one model, shifted
    rate: float = Field(default=1.0, ge=0.7, le=1.4)


class StoryBible(BaseModel):
    series_id: str = Field(pattern=SLUG)
    title: str
    logline: str
    world: str
    style_prefix: str                           # prepended to every image prompt
    negative: str = ""
    characters: list[Character] = Field(min_length=1)
    # names this story must never use: titles/characters of tracked films (originality, F-04)
    banned_names: list[str] = []

    def character(self, cid: str) -> Character:
        for c in self.characters:
            if c.id == cid:
                return c
        raise KeyError(cid)


class Line(BaseModel):
    speaker: str = Field(pattern=SLUG)          # a character id, or "narrator"
    text: str = Field(min_length=1)             # spoken words only: no digits (TTS reads words)


class Shot(BaseModel):
    shot_id: str = Field(pattern=SLUG)
    kind: Literal["hook", "scene", "cliffhanger", "end_card"] = "scene"
    place: str
    image_prompt: str                           # scene description; style_prefix is added
    characters: list[str] = []
    motion: Motion = "zoom_in"
    lines: list[Line] = []
    hold_seconds: float = Field(default=0.0, ge=0, le=10)   # shots with no lines
    on_screen_text: str | None = None
    in_short: bool = False


class FilmEpisode(BaseModel):
    series_id: str = Field(pattern=SLUG)
    number: int = Field(ge=1)
    title: str
    shots: list[Shot] = Field(min_length=1)
