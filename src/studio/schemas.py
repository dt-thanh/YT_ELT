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

Pillar = Literal["stoic", "east", "fable"]
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
