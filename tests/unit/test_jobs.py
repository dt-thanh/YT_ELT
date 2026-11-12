"""The owner's three gates (D6; owner request 2026-11-12): pure rules, and the code
checks a film script passes before the owner reads it."""
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from studio import jobs
from studio.film_script import check_film_script, screenplay
from studio.schemas import FilmEpisode, Line, Shot, StoryBible

ROOT = Path(__file__).resolve().parents[2]


def test_each_gate_sends_the_job_forward_back_or_out():
    d = jobs.decide
    assert d("awaiting_topic_approval", "approved") == "writing"
    assert d("awaiting_topic_approval", "changes_requested") == "researching"
    assert d("awaiting_script_approval", "approved") == "producing"
    assert d("awaiting_script_approval", "changes_requested") == "writing"
    assert d("awaiting_final_approval", "approved") == "ready_to_publish"
    assert d("awaiting_final_approval", "changes_requested") == "producing"
    assert d("awaiting_final_approval", "changes_requested", back="script") == "writing"
    assert all(d(g, "rejected") == "rejected" for g in jobs.GATES)


def test_nothing_skips_a_gate():
    with pytest.raises(jobs.JobError, match="not waiting for you"):
        jobs.decide("writing", "approved")
    for frm, to in [("writing", "producing"), ("awaiting_topic_approval", "writing"),
                    ("awaiting_script_approval", "producing"), ("checking", "producing"),
                    ("qc", "ready_to_publish"), ("awaiting_final_approval", "published")]:
        with pytest.raises(jobs.JobError):
            jobs.check_move(frm, to)
    jobs.check_move("checking", "awaiting_script_approval")
    jobs.check_move("producing", "failed")
    with pytest.raises(jobs.JobError):
        jobs.check_move("rejected", "failed")


def test_a_film_proposal_names_its_series_and_episode():
    base = dict(pillar="ai_film", format="film_episode", title="Còn Bảy Ngày tập 2",
                idea="An bị giam, phải thuyết phục lính canh tin mình.",
                why="Mô-típ xuyên không có hệ thống đếm ngược.")
    p = jobs.Proposal(**base, series_id="con_bay_ngay", episode=2)
    assert jobs.film_paths(p.series_id, p.episode)[1] == Path("content/films/con_bay_ngay/ep02.yaml")
    with pytest.raises(ValidationError, match="series_id and episode"):
        jobs.Proposal(**base)
    with pytest.raises(ValidationError):
        jobs.Proposal(**{**base, "pillar": "stoic"}, series_id="x", episode=1)


def _pilot():
    load = lambda name: yaml.safe_load((ROOT / "content/films/con_bay_ngay" / name).read_text("utf-8"))
    return StoryBible.model_validate(load("bible.yaml")), FilmEpisode.model_validate(load("ep01.yaml"))


def test_the_pilot_script_passes_the_code_checks_and_reads_like_a_screenplay():
    bible, ep = _pilot()
    assert check_film_script(bible, ep, "con_bay_ngay", 1) == []
    text = screenplay(bible, ep)
    assert text.startswith("CÒN BẢY NGÀY - TẬP 1: Đồng xu trong đêm cúp điện")
    assert "1. MỞ ĐẦU - Phố cổ Thăng Long, chạng vạng [vào Short]" in text
    assert "Đỗ Tuấn: Đứng yên!" in text and "Người dẫn: Còn bảy ngày." in text


def test_the_code_checks_catch_a_broken_script():
    bible, ep = _pilot()
    bible = bible.model_copy(update={"banned_names": ["Tiêu Viêm"]})
    bad = Shot(shot_id="s01_hook", kind="scene", place="x", image_prompt="y", characters=["lan"],
               lines=[Line(speaker="lan", text="Năm 1257, Tiêu Viêm tới.")], in_short=True)
    ep = ep.model_copy(update={"number": 3, "shots": [bad] + ep.shots})
    problems = "\n".join(check_film_script(bible, ep, "con_bay_ngay", 1))
    for expected in ("episode 1, the script says 3", "open on a hook", "used twice",
                     "character 'lan'", "speaker 'lan'", "spell numbers out", "banned name",
                     "Short must open on a hook"):
        assert expected in problems
