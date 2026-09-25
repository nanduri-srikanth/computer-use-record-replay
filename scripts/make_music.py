"""Composes the walkthrough's background track: an original, upbeat electro-pop loop, synthesized from scratch.

No samples and no third-party audio, so there is nothing to license. 118 BPM, C major, I-V-vi-IV.
Structure: intro (pad and hats), groove (full kit, bass, arpeggio), a lift with an octave-up arpeggio,
and a fade on the last bars. Deterministic: the same length always renders the same file.

    uv run --with numpy python scripts/make_music.py --seconds 245 --out docs/walkthrough/build/music.wav
"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np

SR = 44100
BPM = 118
BEAT = 60 / BPM
BAR = 4 * BEAT
RNG = np.random.default_rng(118)

# I - V - vi - IV in C major, as MIDI notes (root, chord tones)
PROGRESSION = [(48, [60, 64, 67, 72]), (43, [59, 62, 67, 71]), (45, [60, 64, 69, 72]), (41, [60, 65, 69, 72])]


def hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def env(n: int, attack: float, decay: float) -> np.ndarray:
    t = np.arange(n) / SR
    a = np.clip(t / max(attack, 1e-4), 0, 1)
    return a * np.exp(-t / decay)


def lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    a = np.exp(-2 * np.pi * cutoff / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):  # one-pole filter; only used on short notes
        acc = (1 - a) * v + a * acc
        y[i] = acc
    return y


def add(buf: np.ndarray, sig: np.ndarray, at: float, pan: float = 0.0, gain: float = 1.0) -> None:
    i = int(at * SR)
    if i >= buf.shape[0]:
        return
    sig = sig[: buf.shape[0] - i] * gain
    buf[i:i + len(sig), 0] += sig * np.sqrt(0.5 * (1 - pan))
    buf[i:i + len(sig), 1] += sig * np.sqrt(0.5 * (1 + pan))


# ---- instruments

def kick() -> np.ndarray:
    n = int(0.32 * SR)
    t = np.arange(n) / SR
    freq = 48 + 110 * np.exp(-t / 0.035)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    return np.sin(phase) * env(n, 0.001, 0.13) + 0.3 * np.sin(phase * 2) * env(n, 0.001, 0.02)


def clap() -> np.ndarray:
    n = int(0.22 * SR)
    noise = RNG.standard_normal(n)
    band = lowpass(noise - lowpass(noise, 1200), 5000)  # band-pass: body of a clap, no fizz
    bursts = sum(env(n, 0.001, 0.012) * (np.arange(n) >= int(k * 0.011 * SR)) for k in range(3))
    return 0.5 * band * (bursts * 0.35 + env(n, 0.001, 0.09))


def hat(open_: bool = False) -> np.ndarray:
    n = int((0.12 if open_ else 0.04) * SR)
    noise = RNG.standard_normal(n)
    return 0.22 * lowpass(noise - lowpass(noise, 6000), 11000) * env(n, 0.0005, 0.05 if open_ else 0.012)


def bass(midi: int, length: float) -> np.ndarray:
    n = int(length * SR)
    t = np.arange(n) / SR
    f = hz(midi)
    saw = sum(np.sin(2 * np.pi * f * k * t) / k for k in range(1, 7))
    return lowpass(0.5 * saw * env(n, 0.004, 0.18), 900)


def pluck(midi: int, length: float = 0.28) -> np.ndarray:
    n = int(length * SR)
    t = np.arange(n) / SR
    f = hz(midi)
    tone = np.sin(2 * np.pi * f * t) + 0.35 * np.sin(4 * np.pi * f * t) + 0.12 * np.sin(6 * np.pi * f * t)
    return 0.28 * tone * env(n, 0.002, 0.09)


def pad(chord: list[int], length: float) -> np.ndarray:
    n = int(length * SR)
    t = np.arange(n) / SR
    sig = np.zeros(n)
    for m in chord:
        for det in (-0.07, 0.07):
            f = hz(m + det)
            sig += sum(np.sin(2 * np.pi * f * k * t) / k for k in range(1, 4))
    shape = np.minimum(1, t / 0.6) * np.minimum(1, (length - t) / 0.4)
    return 0.035 * sig * np.clip(shape, 0, 1)


def compose(seconds: float) -> np.ndarray:
    bars = int(np.ceil(seconds / BAR)) + 1
    buf = np.zeros((int(bars * BAR * SR) + SR, 2))
    k, c, ho, hc = kick(), clap(), hat(True), hat(False)
    bass_cache: dict[tuple[int, float], np.ndarray] = {}
    pluck_cache: dict[int, np.ndarray] = {}
    for bar in range(bars):
        root, chord = PROGRESSION[bar % 4]
        t0 = bar * BAR
        intro = bar < 4
        lift = (bar // 16) % 2 == 1  # every other 16-bar section lifts the arpeggio an octave
        add(buf, pad(chord, BAR), t0, gain=1.0 if intro else 0.8)
        for beat in range(4):
            tb = t0 + beat * BEAT
            add(buf, hc, tb + BEAT / 2, pan=0.3, gain=0.8 if intro else 1.0)  # offbeat hats
            if intro:
                continue
            add(buf, k, tb, gain=0.9)
            if beat in (1, 3):
                add(buf, c, tb, pan=-0.1, gain=0.45)
            add(buf, hc, tb, pan=0.3, gain=0.35)
            if beat == 3 and bar % 4 == 3:
                add(buf, ho, tb + BEAT / 2, pan=0.35, gain=0.6)
        if intro:
            continue
        for eighth in range(8):  # pulsing bass: root, octave on the offbeats
            m = root + (12 if eighth % 2 else 0)
            key = (m, BEAT / 2)
            if key not in bass_cache:
                bass_cache[key] = bass(m, BEAT / 2 * 0.95)
            add(buf, bass_cache[key], t0 + eighth * BEAT / 2, gain=0.55)
        pattern = [0, 1, 2, 3, 2, 1, 2, 3] * 2  # 16th-note arpeggio over the chord
        for s, idx in enumerate(pattern):
            m = chord[idx] + (12 if lift else 0)
            if m not in pluck_cache:
                pluck_cache[m] = pluck(m)
            add(buf, pluck_cache[m], t0 + s * BEAT / 4, pan=-0.35 if s % 2 else 0.35, gain=0.5 if s % 4 else 0.7)
    out = buf[: int(seconds * SR)]
    fade = int(3 * SR)
    out[-fade:] *= np.linspace(1, 0, fade)[:, None]
    out[: int(1.5 * SR)] *= np.linspace(0, 1, int(1.5 * SR))[:, None]
    out = np.tanh(out * 1.4)  # gentle saturation glues the mix
    return out / np.max(np.abs(out)) * 0.89


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=245)
    ap.add_argument("--out", default="docs/walkthrough/build/music.wav")
    args = ap.parse_args()
    audio = compose(args.seconds)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(args.out, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((audio * 32767).astype("<i2").tobytes())
    print(f"{args.out}: {args.seconds:.0f}s, {BPM} BPM")


if __name__ == "__main__":
    main()
