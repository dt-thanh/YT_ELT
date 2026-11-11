"""AI films (spec D9/D10, F-06): the script drives every cut. Timeline, Short selection,
subtitles and chapters are pure functions; one small real render runs when ffmpeg exists."""
from pathlib import Path

import pytest
import yaml

from studio.production import timeline as tl
from studio.production.images import image_prompt
from studio.production.voice import speakable
from studio.schemas import FilmEpisode, Line, Shot, StoryBible

ROOT = Path(__file__).resolve().parents[2]
SERIES = ROOT / "content" / "films" / "con_bay_ngay"

BIBLE = StoryBible(series_id="t", title="T", logline="l", world="w", style_prefix="truyện tranh",
                   negative="chữ viết",
                   characters=[{"id": "an", "name": "An", "look": "sơ mi trắng"},
                               {"id": "tuan", "name": "Tuấn", "look": "giáp da"}])


def shot(sid, *, kind="scene", lines=(), in_short=False, hold=0.0, chars=()):
    return Shot(shot_id=sid, kind=kind, place="chợ", image_prompt="cảnh", characters=list(chars),
                lines=[Line(speaker=s, text=t) for s, t in lines], in_short=in_short,
                hold_seconds=hold)


def ep(*shots):
    return FilmEpisode(series_id="t", number=1, title="x", shots=list(shots))


def test_shots_and_lines_follow_the_real_audio_lengths():
    s1 = shot("a", lines=[("narrator", "một"), ("an", "hai")])
    s2 = shot("b", hold=3)
    t = tl.build_timeline([s1, s2], {("a", 0): 2.0, ("a", 1): 1.5})
    a, b = t.shots
    assert [(ln.start, ln.end) for ln in a.lines] == [(0.4, 2.4), (2.65, 4.15)]
    assert a.end == pytest.approx(4.15 + tl.TAIL) and b.start == a.end
    assert b.duration == pytest.approx(3.0) and t.duration == pytest.approx(a.end + 3)


def test_shots_are_whole_frames_and_never_too_short():
    t = tl.build_timeline([shot("a", lines=[("an", "x")]), shot("b")], {("a", 0): 1.234}, fps=25)
    a, b = t.shots
    assert round(a.duration * 25, 6) == int(round(a.duration * 25))      # whole frames
    assert b.duration == pytest.approx(tl.MIN_SHOT)


def test_the_short_opens_on_the_hook_and_fits_the_limit():
    hook = shot("h", kind="hook", lines=[("narrator", "x")], in_short=True)
    mid = shot("m", lines=[("an", "y")])
    end = shot("e", kind="cliffhanger", lines=[("an", "z")], in_short=True)
    secs = {("h", 0): 5.0, ("m", 0): 5.0, ("e", 0): 5.0}
    e = ep(hook, mid, end)
    picked, errors = tl.short_shots(e, tl.build_timeline(e.shots, secs))
    assert [s.shot_id for s in picked] == ["h", "e"] and errors == []

    e2 = ep(mid.model_copy(update={"in_short": True}), end)
    _, errors = tl.short_shots(e2, tl.build_timeline(e2.shots, secs))
    assert "must open on a hook" in errors[0]
    _, errors = tl.short_shots(e, tl.build_timeline(e.shots, secs), max_seconds=10)
    assert "more than 10 s" in errors[0]
    _, errors = tl.short_shots(ep(mid), tl.build_timeline([mid], secs))
    assert errors == ["no shot is marked in_short"]


def test_subtitles_share_the_clock_and_mark_the_narrator():
    s = shot("a", lines=[("narrator", "Còn bảy ngày."), ("an", "Ai {đó}?")])
    s = s.model_copy(update={"on_screen_text": "Thăng Long, 1257"})
    t = tl.build_timeline([s], {("a", 0): 1.0, ("a", 1): 61.0})
    srt = tl.to_srt(t)
    assert "00:00:00,400 --> 00:00:01,400\nCòn bảy ngày." in srt
    assert "00:00:01,650 --> 00:01:02,650\nAi {đó}?" in srt
    ass = tl.to_ass(t, width=1080, height=1920)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert ",Narrator,narrator," in ass and ",Line,an," in ass and "Ai (đó)?" in ass
    assert ",Place,,0,0,0,,Thăng Long, 1257" in ass


def test_chapters_of_the_long_cut():
    assert tl.chapters([("Tập 1", 125.4), ("Tập 2", 3500), ("Tập 3", 10)]) == \
        "00:00 Tập 1\n02:05 Tập 2\n1:00:25 Tập 3"


def test_the_voice_reads_words_and_refuses_digits():
    assert speakable("Nguyên Phong thông bảo? Đồng xu...") == "nguyên phong thông bảo đồng xu"
    with pytest.raises(ValueError, match="spell numbers out"):
        speakable("năm 1257")


def test_image_prompt_keeps_style_and_only_the_shots_characters():
    p = image_prompt(shot("a", chars=["an"]), BIBLE)
    assert p.startswith("truyện tranh") and "An: sơ mi trắng" in p and "Tuấn" not in p
    assert p.endswith("Tránh: chữ viết")


# ---- the pilot script itself -------------------------------------------------------------
def test_pilot_episode_is_a_valid_original_script():
    bible = StoryBible.model_validate(yaml.safe_load((SERIES / "bible.yaml").read_text("utf-8")))
    e = FilmEpisode.model_validate(yaml.safe_load((SERIES / "ep01.yaml").read_text("utf-8")))
    speakers = {c.id for c in bible.characters} | {"narrator"}
    assert e.series_id == bible.series_id and e.shots[0].kind == "hook"
    for s in e.shots:
        assert set(s.characters) <= speakers
        for ln in s.lines:
            assert ln.speaker in speakers
            speakable(ln.text)                                  # no digits for the voice
    text = " ".join(ln.text for s in e.shots for ln in s.lines).lower()
    assert not [n for n in bible.banned_names if n.lower() in text]
    assert [s.shot_id for s in e.shots if s.in_short][0] == "s01_hook"


# ---- one real render, tiny -----------------------------------------------------------------
def test_render_an_episode_its_short_and_the_long_cut(tmp_path):
    from studio.production import ffmpeg
    if ffmpeg.ffmpeg_bin() is None:
        pytest.skip("ffmpeg not installed (pip install -e .[media])")
    pytest.importorskip("PIL")
    from studio.production.compose import RenderSpec, compile_series, render_episode
    from studio.production.voice import FakeTts

    e = ep(shot("h", kind="hook", lines=[("narrator", "mở đầu")], in_short=True, chars=["an"]),
           shot("m", lines=[("an", "ở giữa")], chars=["an"]),
           shot("c", kind="cliffhanger", lines=[("tuan", "kết treo")], in_short=True))
    spec = RenderSpec(long=(320, 180), short=(180, 320), fps=10, upscale=1.0, crf=30)
    res = render_episode(e, BIBLE, FakeTts(), work=tmp_path / "w", out=tmp_path / "o", spec=spec)

    assert res.qc_ok, (res.long.qc, res.short and res.short.qc)
    assert (res.long.qc["width"], res.long.qc["height"]) == (320, 180)
    assert (res.short.qc["width"], res.short.qc["height"]) == (180, 320)
    assert res.short.shots == ["h", "c"] and res.short.seconds < res.long.seconds
    assert Path(res.long.srt).read_text("utf-8").count("-->") == 3

    chapters = compile_series([("Tập 1", Path(res.long.path)), ("Tập 2", Path(res.long.path))],
                              tmp_path / "series.mp4")
    first, second = chapters.splitlines()
    assert first == "00:00 Tập 1" and second.endswith("Tập 2")
    assert ffmpeg.probe(tmp_path / "series.mp4").duration == pytest.approx(2 * res.long.seconds, abs=0.5)


def test_long_lines_become_readable_captions_on_the_same_clock():
    text = ("Nhiệm vụ. Giúp Thăng Long đứng vững trước giặc phương Bắc. Thời gian còn lại, "
            "bảy ngày. Nếu thất bại, bạn sẽ không bao giờ trở về.")
    parts = tl.split_caption(text, tl.SUB_CHARS_TALL)
    assert all(len(p) <= tl.SUB_CHARS_TALL for p in parts) and " ".join(parts) == text
    assert parts[1] == "Giúp Thăng Long đứng vững trước giặc phương Bắc."     # sentence kept whole
    t = tl.build_timeline([shot("a", lines=[("narrator", text)])], {("a", 0): 12.0})
    caps = tl.captions(t, tl.SUB_CHARS_TALL)
    assert caps[0][0] == 0.4 and caps[-1][1] == pytest.approx(12.4)            # spans the line
    assert all(a[1] == pytest.approx(b[0]) for a, b in zip(caps, caps[1:]))    # back to back
    assert tl.split_caption("một " * 30, 20) == ["một một một một một"] * 6
