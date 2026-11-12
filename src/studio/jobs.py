"""
Jobs and the owner's three gates (spec §7.1, D6; owner request 2026-11-12).

Nothing is made before the owner says yes:

    proposals (a LIST) --gate 1 topic--> writing --script ready + code checks--> gate 2 script
        --approved--> producing --render + QC--> gate 3 final --approved--> ready_to_publish

* gate 1: the owner sees every proposed video and picks; the rest are rejected.
* gate 2: the owner reads the script. "changes" sends it back to writing with the
  reason; the writer reads it (`job-show`) and resubmits.
* gate 3: the owner watches the render. "changes" re-renders (or, `back="script"`,
  rewrites). Publishing is still the owner's click (P7 uploads private only).

`decide()` and `ALLOWED` are pure (unit-tested); the rest is SQL with row locks.
The approved script is pinned by its SHA-256: a script edited after approval
cannot be rendered (`require_renderable`) until it passes gate 2 again.
LangGraph (P5, O-01/O-02) will drive these same states; the CLI is the manual
pilot of it (M-06/F-05).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from studio.schemas import Pillar

Format = Literal["short", "long", "film_episode"]
Decision = Literal["approved", "changes_requested", "rejected"]

GATES = {"awaiting_topic_approval": "topic",
         "awaiting_script_approval": "script",
         "awaiting_final_approval": "final"}

# forward moves that are not owner decisions (owner decisions go through decide())
ALLOWED = {
    "planned": {"researching"},
    "researching": {"awaiting_topic_approval"},
    "writing": {"checking"},
    "checking": {"awaiting_script_approval", "writing"},
    "producing": {"qc"},
    "qc": {"awaiting_final_approval", "producing"},
    "ready_to_publish": {"published"},
    "published": {"analyzed"},
}
FINAL = {"rejected", "failed", "analyzed"}


class JobError(Exception):
    """A move the rules do not allow; the message says what to do instead."""


class Proposal(BaseModel):
    """One proposed video, as the owner sees it at gate 1."""
    pillar: Pillar
    format: Format
    title: str = Field(min_length=3, max_length=200)
    idea: str = Field(min_length=10, description="what the video is, in 1-3 sentences")
    why: str = Field(min_length=10, description="why now: trend or evidence, in words")
    evidence: list[str] = Field(default_factory=list, description="video URLs behind `why`")
    series_id: str | None = None
    episode: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _film_needs_series(self):
        if self.format == "film_episode":
            if self.pillar != "ai_film" or not self.series_id or not self.episode:
                raise ValueError("a film_episode is ai_film and names series_id and episode")
        return self


def decide(state: str, decision: Decision, back: str | None = None) -> str:
    """Where an owner decision at a gate sends the job."""
    gate = GATES.get(state)
    if gate is None:
        raise JobError(f"nothing to approve: the job is in {state!r}, not waiting for you")
    if decision == "rejected":
        return "rejected"
    if decision == "approved":
        return {"topic": "writing", "script": "producing", "final": "ready_to_publish"}[gate]
    if back not in (None, "script"):
        raise JobError(f"back must be 'script' or omitted, not {back!r}")
    if gate == "final":
        return "writing" if back == "script" else "producing"
    return {"topic": "researching", "script": "writing"}[gate]


def check_move(state: str, to: str) -> None:
    if to in ("failed", "rejected") and state not in FINAL:
        return
    if to not in ALLOWED.get(state, set()):
        raise JobError(f"a job in {state!r} cannot move to {to!r}")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def film_paths(series_id: str, episode: int, root: Path = Path("content/films")) -> tuple[Path, Path]:
    """bible and episode script of a series: one folder per series."""
    folder = root / series_id
    return folder / "bible.yaml", folder / f"ep{episode:02d}.yaml"


# ---- database -----------------------------------------------------------------------------
_COLS = ("job_id, pillar, format, title, proposal, series_id, episode, state, "
         "script_path, script_sha256, outputs, created_at, updated_at")


def _event(cur, job_id: int, frm: str | None, to: str, actor: str, note: str | None = None):
    cur.execute("INSERT INTO yti.job_events (job_id, from_state, to_state, actor, note) "
                "VALUES (%s, %s, %s, %s, %s)", (job_id, frm, to, actor, note))


def propose(cur, proposals: list[Proposal], actor: str) -> list[int]:
    """Gate 1 input: each proposal becomes a job waiting for the owner."""
    ids = []
    for p in proposals:
        cur.execute(
            "INSERT INTO yti.jobs (pillar, format, title, proposal, series_id, episode, state) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'awaiting_topic_approval') RETURNING job_id",
            (p.pillar, p.format, p.title, json.dumps(p.model_dump(), ensure_ascii=False),
             p.series_id, p.episode))
        job_id = cur.fetchone()["job_id"]
        _event(cur, job_id, None, "researching", actor, "proposed")
        _event(cur, job_id, "researching", "awaiting_topic_approval", actor)
        ids.append(job_id)
    return ids


def get(cur, job_id: int, *, lock: bool = False) -> dict:
    cur.execute(f"SELECT {_COLS} FROM yti.jobs WHERE job_id = %s" + (" FOR UPDATE" if lock else ""),
                (job_id,))
    row = cur.fetchone()
    if row is None:
        raise JobError(f"no job {job_id}")
    return dict(row)


def list_jobs(cur, states: list[str] | None = None) -> list[dict]:
    if states:
        cur.execute(f"SELECT {_COLS} FROM yti.jobs WHERE state = ANY(%s) ORDER BY job_id", (states,))
    else:
        cur.execute(f"SELECT {_COLS} FROM yti.jobs ORDER BY job_id")
    return [dict(r) for r in cur.fetchall()]


def history(cur, job_id: int) -> tuple[list[dict], list[dict]]:
    cur.execute("SELECT at, from_state, to_state, actor, note FROM yti.job_events "
                "WHERE job_id = %s ORDER BY event_id", (job_id,))
    events = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT gate, decision, reason, reviewer, human_effort_minutes, decided_at "
                "FROM yti.approvals WHERE job_id = %s ORDER BY approval_id", (job_id,))
    return events, [dict(r) for r in cur.fetchall()]


def open_feedback(approvals: list[dict]) -> str | None:
    """The owner's latest 'changes' reason, if it is the last decision: what to fix now."""
    if approvals and approvals[-1]["decision"] == "changes_requested":
        return approvals[-1]["reason"]
    return None


def move(cur, job_id: int, to: str, actor: str, note: str | None = None, **fields) -> dict:
    """A non-owner step (submit script, render done, ...). Checked against ALLOWED."""
    job = get(cur, job_id, lock=True)
    check_move(job["state"], to)
    sets, vals = ["state = %s", "updated_at = now()"], [to]
    for k, v in fields.items():
        if k not in ("script_path", "script_sha256", "outputs"):
            raise ValueError(f"cannot set {k}")
        sets.append(f"{k} = %s")
        vals.append(json.dumps(v, ensure_ascii=False) if k == "outputs" else v)
    cur.execute(f"UPDATE yti.jobs SET {', '.join(sets)} WHERE job_id = %s", (*vals, job_id))
    _event(cur, job_id, job["state"], to, actor, note)
    return {**job, "state": to, **fields}


def record_decision(cur, job_id: int, decision: Decision, *, reviewer: str,
                    reason: str | None = None, minutes: float | None = None,
                    back: str | None = None) -> str:
    """The owner's yes / changes / no at the gate the job is waiting on."""
    job = get(cur, job_id, lock=True)
    to = decide(job["state"], decision, back)
    gate = GATES[job["state"]]
    if decision != "approved" and not (reason or "").strip():
        raise JobError("say why (--reason): the writer reads it to fix the job")
    if gate == "script" and decision == "approved":
        current = sha256_file(Path(job["script_path"]))
        if current != job["script_sha256"]:
            raise JobError("the script changed after it was submitted: run job-script again")
    cur.execute(
        "INSERT INTO yti.approvals (job_id, gate, decision, reason, reviewer, "
        "human_effort_minutes, script_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (job_id, gate, decision, reason, reviewer, minutes,
         job["script_sha256"] if gate == "script" else None))
    cur.execute("UPDATE yti.jobs SET state = %s, updated_at = now() WHERE job_id = %s", (to, job_id))
    _event(cur, job_id, job["state"], to, reviewer, f"{gate}: {decision}" + (f" - {reason}" if reason else ""))
    return to


def require_renderable(job: dict) -> None:
    """Only an approved, unchanged script is rendered (gate 2 cannot be skipped)."""
    if job["state"] != "producing":
        hint = {"awaiting_script_approval": "the script is waiting for your approval",
                "writing": "the script is not submitted yet",
                "awaiting_topic_approval": "the topic is not approved yet",
                "awaiting_final_approval": "already rendered: approve it or ask for changes"}
        raise JobError(f"job {job['job_id']} is {job['state']!r}: "
                       f"{hint.get(job['state'], 'nothing to render')}")
    if not job["script_path"] or sha256_file(Path(job["script_path"])) != job["script_sha256"]:
        raise JobError("the script changed after you approved it: submit it again (job-script) "
                       "and approve the new version")
