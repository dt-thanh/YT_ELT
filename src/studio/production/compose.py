"""
Render one AI-film episode (F-06, M-03): script -> long cut 16:9 + vertical Short 9:16.

    voices  : one WAV per line (cached by text + voice)     -> real durations
    timeline: shots and lines on one clock (frame-aligned)
    images  : one square image per shot (cached)
    clips   : one clip per shot - crop to the format, slow zoom/pan, fade in/out
    variant : clips joined + voice track + burned subtitles + two-pass loudnorm (-14 LUFS)
    Short   : the same steps over the shots marked in_short, in 1080x1920
    QC      : duration, size, audio and loudness read back from the files

Every step writes under `work/` keyed by a hash of its inputs and is skipped when the
file exists, so a re-run after editing one shot only redoes that shot (spec §10:
every step idempotent). `compile_series` joins rendered episodes into the long cut.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from studio.production import ffmpeg, timeline as tl
from studio.production.images import image_prompt, placeholder_image
from studio.production.voice import Tts, mix_track, wav_seconds, write_wav
from studio.schemas import FilmEpisode, StoryBible

logger = logging.getLogger(__name__)

FADE = 0.3


@dataclass(frozen=True)
class RenderSpec:
    long: tuple[int, int] = (1920, 1080)     # spec §10.9
    short: tuple[int, int] = (1080, 1920)
    fps: int = 25
    upscale: float = 1.5                     # zoompan on a larger frame: no jitter
    crf: int = 20
    loudness: float = -14.0                  # LUFS, spec §10.8
    loudness_tolerance: float = 2.0
    fonts_dir: str = "/usr/share/fonts/truetype/dejavu"


@dataclass
class VariantResult:
    path: str
    srt: str
    seconds: float
    shots: list[str]
    qc: dict = field(default_factory=dict)


@dataclass
class EpisodeResult:
    series_id: str
    number: int
    title: str
    voice: str
    long: VariantResult
    short: VariantResult | None
    short_errors: list[str]
    timeline: list[dict]

    @property
    def qc_ok(self) -> bool:
        parts = [self.long] + ([self.short] if self.short else [])
        return all(v.qc.get("ok") for v in parts)


def _key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()[:12]


# ---- voices ------------------------------------------------------------------------------
def _voice_of(bible: StoryBible, speaker: str) -> tuple[float, float]:
    try:
        c = bible.character(speaker)
        return c.pitch, c.rate
    except KeyError:
        if speaker == "narrator":
            return 1.0, 1.0
        raise ValueError(f"speaker {speaker!r} is not a character of {bible.series_id}") from None


def synth_lines(episode: FilmEpisode, bible: StoryBible, tts: Tts,
                work: Path) -> tuple[dict[tuple[str, int], float], dict[tuple[str, int], Path]]:
    seconds, paths = {}, {}
    for shot in episode.shots:
        for i, line in enumerate(shot.lines):
            pitch, rate = _voice_of(bible, line.speaker)
            out = work / "voice" / f"{_key(tts.name, line.text, pitch, rate)}.wav"
            if not out.exists():
                raw = out.with_suffix(".raw.wav")
                write_wav(raw, tts.synth(line.text), tts.rate)
                if pitch == 1.0 and rate == 1.0:
                    raw.rename(out)
                else:
                    # pitch up by p also speeds up by p; atempo puts the speed back (x rate)
                    ffmpeg.run(["-i", str(raw), "-af",
                                f"asetrate={tts.rate * pitch:.0f},aresample={tts.rate},"
                                f"atempo={rate / pitch:.4f}", "-ac", "1", str(out)])
                    raw.unlink()
            seconds[(shot.shot_id, i)] = wav_seconds(out)
            paths[(shot.shot_id, i)] = out
    return seconds, paths


# ---- pictures ----------------------------------------------------------------------------
def shot_images(episode: FilmEpisode, bible: StoryBible, work: Path) -> dict[str, Path]:
    out = {}
    for n, shot in enumerate(episode.shots, start=1):
        p = work / "images" / f"{shot.shot_id}-{_key(image_prompt(shot, bible), shot.place, shot.kind, n)}.png"
        if not p.exists():
            placeholder_image(shot, bible, n, p)
        out[shot.shot_id] = p
    return out


_MOTION = {
    "zoom_in": ("1+0.12*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    "zoom_out": ("1.12-0.12*on/{n}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
    "pan_left": ("1.12", "(iw-iw/zoom)*(1-on/{n})", "ih/2-(ih/zoom/2)"),
    "pan_right": ("1.12", "(iw-iw/zoom)*on/{n}", "ih/2-(ih/zoom/2)"),
    "static": ("1", "0", "0"),
}


def clip_filter(motion: str, seconds: float, size: tuple[int, int], spec: RenderSpec) -> tuple[str, int]:
    """FFmpeg filter for one shot: crop the square to the format, enlarge, slow move, fade."""
    w, h = size
    frames = max(1, round(seconds * spec.fps))
    crop = "crop=iw:iw*9/16" if w >= h else "crop=ih*9/16:ih"
    sw, sh = int(w * spec.upscale) // 2 * 2, int(h * spec.upscale) // 2 * 2
    z, x, y = (e.format(n=max(1, frames - 1)) for e in _MOTION[motion])
    fade_out = max(0.0, seconds - FADE)
    vf = (f"{crop},scale={sw}:{sh},"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={spec.fps},"
          f"fade=t=in:st=0:d={FADE},fade=t=out:st={fade_out:.3f}:d={FADE},format=yuv420p")
    return vf, frames


def _render_variant(name: str, timeline: tl.Timeline, images: dict[str, Path],
                    voices: dict[tuple[str, int], Path], voice_rate: int, size: tuple[int, int],
                    spec: RenderSpec, work: Path, out_mp4: Path) -> VariantResult:
    vdir = work / "clips" / f"{size[0]}x{size[1]}"
    clips = []
    for span in timeline.shots:
        img = images[span.shot.shot_id]
        clip = vdir / f"{span.shot.shot_id}-{_key(img.name, span.duration, span.shot.motion, size, spec.fps, spec.upscale, spec.crf)}.mp4"
        if not clip.exists():
            vf, frames = clip_filter(span.shot.motion, span.duration, size, spec)
            vdir.mkdir(parents=True, exist_ok=True)
            ffmpeg.run(["-i", str(img), "-vf", vf, "-frames:v", str(frames), "-r", str(spec.fps),
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(spec.crf), str(clip)])
        clips.append(clip)

    vw = work / name
    vw.mkdir(parents=True, exist_ok=True)
    (vw / "clips.txt").write_text("".join("file '" + str(c).replace("'", r"'\''") + "'\n"
                                          for c in clips), encoding="utf-8")
    ffmpeg.run(["-f", "concat", "-safe", "0", "-i", "clips.txt", "-c", "copy", "video.mp4"], cwd=vw)

    placements = [(ln.start, voices[(ln.shot_id, ln.index)]) for ln in timeline.lines]
    write_wav(vw / "voice.wav", mix_track(placements, timeline.duration, voice_rate), voice_rate)
    (vw / "subs.ass").write_text(tl.to_ass(timeline, width=size[0], height=size[1]), encoding="utf-8")
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    srt = out_mp4.with_suffix(".srt")
    srt.write_text(tl.to_srt(timeline), encoding="utf-8")

    norm = loudnorm_filter(vw / "voice.wav", spec)
    ffmpeg.run(["-i", "video.mp4", "-i", "voice.wav", "-filter_complex",
                f"[0:v]ass=subs.ass:fontsdir={spec.fonts_dir}[v];"
                f"[1:a]aresample=48000,{norm},aresample=48000[a]",
                "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", str(spec.crf), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                "-ar", "48000", "-movflags", "+faststart", str(out_mp4)], cwd=vw)
    result = VariantResult(str(out_mp4), str(srt), timeline.duration,
                           [s.shot.shot_id for s in timeline.shots])
    result.qc = quality_check(out_mp4, expected_seconds=timeline.duration, size=size, spec=spec)
    return result


def loudnorm_filter(wav: Path, spec: RenderSpec) -> str:
    """Two-pass EBU R128 normalisation: measure the voice track, then correct it linearly.
    One pass cannot reach the target on short clips (it needs look-ahead it does not have)."""
    target = f"I={spec.loudness}:TP=-1.5:LRA=11"
    err = ffmpeg.run(["-i", str(wav), "-af", f"loudnorm={target}:print_format=json",
                      "-f", "null", "-"])
    m = json.loads(err[err.rindex("{"):err.rindex("}") + 1])
    return (f"loudnorm={target}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
            f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
            f"offset={m['target_offset']}:linear=true")


def quality_check(path: Path, *, expected_seconds: float, size: tuple[int, int],
                  spec: RenderSpec) -> dict:
    """Technical QC (spec §7.6 'Kỹ thuật', §10.12): read back what the file really is."""
    info = ffmpeg.probe(path)
    lufs = ffmpeg.loudness_lufs(path) if info.has_audio else None
    checks = {
        "duration": abs(info.duration - expected_seconds) <= 0.3,
        "size": (info.width, info.height) == size,
        "audio": info.has_audio,
        "loudness": lufs is not None and abs(lufs - spec.loudness) <= spec.loudness_tolerance,
    }
    return {"ok": all(checks.values()), "checks": checks, "seconds": round(info.duration, 2),
            "width": info.width, "height": info.height, "lufs": lufs}


# ---- episode -----------------------------------------------------------------------------
def render_episode(episode: FilmEpisode, bible: StoryBible, tts: Tts, *, work: Path, out: Path,
                   spec: RenderSpec = RenderSpec(), make_short: bool = True) -> EpisodeResult:
    if episode.series_id != bible.series_id:
        raise ValueError(f"episode belongs to {episode.series_id!r}, bible is {bible.series_id!r}")
    work, out = Path(work).resolve(), Path(out).resolve()   # ffmpeg runs in work/ subfolders
    seconds, voices = synth_lines(episode, bible, tts, work)
    images = shot_images(episode, bible, work)
    stem = f"{episode.series_id}_ep{episode.number:02d}"

    long_tl = tl.build_timeline(episode.shots, seconds, fps=spec.fps)
    logger.info("%s: %d shots, %.1f s", stem, len(long_tl.shots), long_tl.duration)
    long = _render_variant("long", long_tl, images, voices, tts.rate, spec.long, spec, work,
                           out / f"{stem}_long.mp4")

    short, errors = None, []
    if make_short:
        picked, errors = tl.short_shots(episode, long_tl)
        if not errors:
            short_tl = tl.build_timeline(picked, seconds, fps=spec.fps)
            short = _render_variant("short", short_tl, images, voices, tts.rate, spec.short,
                                    spec, work, out / f"{stem}_short.mp4")
            if short.seconds > tl.SHORT_MAX:
                short.qc["ok"] = False

    result = EpisodeResult(episode.series_id, episode.number, episode.title, tts.name, long,
                           short, errors,
                           [{"shot_id": s.shot.shot_id, "kind": s.shot.kind, "start": s.start,
                             "end": s.end, "in_short": s.shot.in_short} for s in long_tl.shots])
    (out / f"{stem}_report.json").write_text(
        json.dumps(asdict(result) | {"qc_ok": result.qc_ok}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return result


def compile_series(parts: list[tuple[str, Path]], out_mp4: Path) -> str:
    """Join rendered episodes (same encoding) into the long cut; returns its chapters,
    also written next to it as <name>_chapters.txt for the video description."""
    out_mp4 = Path(out_mp4).resolve()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    listing = out_mp4.with_suffix(".txt")
    listing.write_text("".join("file '" + str(Path(p).resolve()).replace("'", r"'\''") + "'\n"
                               for _, p in parts), encoding="utf-8")
    ffmpeg.run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                "-movflags", "+faststart", str(out_mp4)])
    listing.unlink()
    text = tl.chapters([(title, ffmpeg.probe(p).duration) for title, p in parts])
    out_mp4.with_name(out_mp4.stem + "_chapters.txt").write_text(text + "\n", encoding="utf-8")
    return text


def load_yaml_model(path: Path | str, model):
    import yaml
    return model.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

