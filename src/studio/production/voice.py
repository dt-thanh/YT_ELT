"""
Voices (spec §10.1). A `Tts` turns one line into mono PCM samples; the caller writes it.

* MmsTts: Meta's MMS-TTS Vietnamese model, run locally on CPU, free. PILOT ONLY: the
  model is licensed CC-BY-NC 4.0 (non-commercial), so it must not voice a monetised
  video. The real voice is chosen by the blind listening test (M-01: FPT.AI, ElevenLabs).
  One speaker: characters are told apart by a pitch/rate shift (Character.pitch/rate).
* FakeTts: tests. A quiet tone whose length grows with the text (audible, so loudness
  QC has something to measure), deterministic, instant.
"""

from __future__ import annotations

import re
import unicodedata
import wave
from pathlib import Path
from typing import Protocol

import numpy as np

_KEEP = re.compile(r"[^\w\s]", re.UNICODE)


def speakable(text: str) -> str:
    """What the TTS model reads: lowercase words. MMS drops punctuation anyway; digits it
    cannot read at all, so scripts spell numbers out (the schema says so) - fail loudly."""
    if any(ch.isdigit() for ch in text):
        raise ValueError(f"spell numbers out for the voice: {text!r}")
    text = unicodedata.normalize("NFC", text).lower()
    return " ".join(_KEEP.sub(" ", text).split())


class Tts(Protocol):
    name: str
    rate: int

    def synth(self, text: str) -> np.ndarray: ...


class MmsTts:
    name = "facebook/mms-tts-vie"

    rate = 16000                                     # MMS-TTS output rate

    def __init__(self, model: str = "facebook/mms-tts-vie"):
        self._name, self._model = model, None        # loaded on first use: cached lines skip it

    def _load(self):
        import torch
        from transformers import AutoTokenizer, VitsModel
        self._torch = torch
        self._tok = AutoTokenizer.from_pretrained(self._name)
        self._model = VitsModel.from_pretrained(self._name)
        if int(self._model.config.sampling_rate) != self.rate:
            raise ValueError(f"{self._name}: {self._model.config.sampling_rate} Hz, expected {self.rate}")

    def synth(self, text: str) -> np.ndarray:
        if self._model is None:
            self._load()
        self._torch.manual_seed(2026)                # VITS samples noise: same line, same audio
        with self._torch.no_grad():
            out = self._model(**self._tok(speakable(text), return_tensors="pt")).waveform[0]
        return out.numpy().astype(np.float32)


class FakeTts:
    name = "fake"
    rate = 16000

    def __init__(self, chars_per_second: float = 15.0):
        self.cps = chars_per_second

    def synth(self, text: str) -> np.ndarray:
        seconds = max(0.5, len(speakable(text)) / self.cps)
        t = np.arange(int(seconds * self.rate), dtype=np.float32) / self.rate
        return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def write_wav(path: Path, samples: np.ndarray, rate: int) -> float:
    """16-bit mono WAV; returns its length in seconds."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return len(pcm) / rate


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32767, rate


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def mix_track(placements: list[tuple[float, Path]], seconds: float, rate: int) -> np.ndarray:
    """One mono track of `seconds`: each WAV placed at its start time (lines never overlap)."""
    track = np.zeros(int(round(seconds * rate)) + 1, dtype=np.float32)
    for start, path in placements:
        samples, r = read_wav(path)
        if r != rate:
            raise ValueError(f"{path}: {r} Hz, expected {rate} Hz")
        i = int(round(start * rate))
        n = min(len(samples), len(track) - i)
        track[i:i + n] += samples[:n]
    return track
