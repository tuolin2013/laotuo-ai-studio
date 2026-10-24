"""本机合成的配乐与音效（numpy），不使用任何外部音频素材或接口，没有版权问题。

配乐按 BPM 与主题调式生成：钩子段只有铺底和弹拨（留白），主视觉登场（起势点）才进鼓点、贝斯、琶音，
收尾两小节撤掉鼓点只留和弦长音；底鼓触发侧链压缩，让铺底随节拍「呼吸」。
音效全部与画面事件对齐：转场呼啸声的峰值落在剪辑点上，起势音效恰好在起势点结束并接一记重击，
卖点逐条落定时的提示音按音阶逐级升高；全片只有一次「起势 → 重击」。
"""

from __future__ import annotations

import math
import wave
from collections.abc import Iterable
from pathlib import Path

import numpy as np

SR = 48000
MAJOR = (0, 2, 4, 5, 7, 9, 11)
PENTATONIC = (0, 2, 4, 7, 9)


def midi_hz(n: float) -> float:
    return 440.0 * 2 ** ((n - 69) / 12)


def _t(n: int) -> np.ndarray:
    return np.arange(n, dtype=np.float32) / SR


def _env(n: int, attack: float, decay: float) -> np.ndarray:
    t = _t(n)
    a = np.minimum(1.0, t / max(attack, 1e-4))
    return (a * np.exp(-np.maximum(0.0, t - attack) / max(decay, 1e-4))).astype(np.float32)


def _band(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """FFT 带通（平滑边沿），用于噪声类音色。"""
    n = len(x)
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1 / SR)
    w = np.clip((f - lo * 0.7) / (lo * 0.3 + 1e-6), 0, 1) * np.clip((hi * 1.3 - f) / (hi * 0.3 + 1e-6), 0, 1)
    return np.fft.irfft(spec * w, n).astype(np.float32)


class Bus:
    """立体声总线：按秒放置单声道音色，等功率声像。"""

    def __init__(self, seconds: float):
        self.n = math.ceil(seconds * SR) + SR // 10
        self.l = np.zeros(self.n, np.float32)
        self.r = np.zeros(self.n, np.float32)

    def add(self, sig: np.ndarray, at: float, gain: float = 1.0, pan: float = 0.0) -> None:
        i = round(at * SR)
        if i >= self.n or len(sig) == 0:
            return
        if i < 0:
            sig, i = sig[-i:], 0
        sig = sig[: self.n - i] * gain
        a = (max(-1.0, min(1.0, pan)) + 1) * math.pi / 4
        self.l[i:i + len(sig)] += sig * math.cos(a)
        self.r[i:i + len(sig)] += sig * math.sin(a)

    def add_stereo(self, left: np.ndarray, right: np.ndarray, at: float = 0.0, gain: float = 1.0) -> None:
        i = round(at * SR)
        m = min(len(left), self.n - i)
        if m > 0:
            self.l[i:i + m] += left[:m] * gain
            self.r[i:i + m] += right[:m] * gain


# ---------------------------------------------------------------- 乐器


def kick(rng) -> np.ndarray:
    n = int(0.45 * SR)
    t = _t(n)
    f = 46 + 110 * np.exp(-t / 0.028)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.26)
    click = rng.normal(0, 1, n).astype(np.float32) * np.exp(-t / 0.002) * 0.25
    return (body + click).astype(np.float32)


def clap(rng) -> np.ndarray:
    n = int(0.3 * SR)
    t = _t(n)
    noise = _band(rng.normal(0, 1, n).astype(np.float32), 900, 3500)
    env = np.exp(-t / 0.11)
    for k in (0.0, 0.011, 0.022):  # 三次微拍，像真实拍手
        env = env + 0.6 * np.exp(-np.maximum(0, t - k) / 0.004) * (t >= k)
    return (noise * env * 0.5).astype(np.float32)


def hat(rng) -> np.ndarray:
    n = int(0.08 * SR)
    return (_band(rng.normal(0, 1, n).astype(np.float32), 7000, 16000) * np.exp(-_t(n) / 0.022)).astype(np.float32)


def pluck(freq: float, decay: float = 0.28) -> np.ndarray:
    n = int((decay * 4 + 0.02) * SR)
    t = _t(n)
    w = 2 * np.pi * freq * t
    sig = np.sin(w) + 0.35 * np.sin(2 * w) * np.exp(-t / (decay * 0.4)) + 0.12 * np.sin(3 * w) * np.exp(-t / 0.05)
    return (sig * _env(n, 0.003, decay)).astype(np.float32)


def bass_note(freq: float, length: float) -> np.ndarray:
    n = int(length * SR)
    t = _t(n)
    w = 2 * np.pi * freq * t
    sig = np.sin(w) + 0.25 * np.sin(2 * w) + 0.08 * np.sin(3 * w)
    env = np.minimum(1, t / 0.006) * np.exp(-t / (length * 0.9)) * np.clip((length - t) / 0.02, 0, 1)
    return (sig * env).astype(np.float32)


def pad_chord(freqs: Iterable[float], length: float, rng) -> tuple[np.ndarray, np.ndarray]:
    """柔和铺底：每个和弦音两支微失谐的「锯齿」（6 个谐波叠加，天然无混叠），左右声道各一支。"""
    n = int(length * SR)
    t = _t(n)
    left = np.zeros(n, np.float32)
    right = np.zeros(n, np.float32)
    for f in freqs:
        for side, cents in ((left, -7), (right, 7)):
            ff = f * 2 ** (cents / 1200)
            ph = rng.uniform(0, 2 * np.pi)
            for h in range(1, 7):
                side += (np.sin(2 * np.pi * ff * h * t + ph * h) * (1 / h) * math.exp(-h / 2.2)).astype(np.float32)
    attack, release = min(0.45, length * 0.3), min(0.6, length * 0.4)
    env = np.minimum(1, t / attack) * np.clip((length - t) / release, 0, 1)
    return left * env, right * env


# ---------------------------------------------------------------- 音效


def whoosh(length: float, rng) -> np.ndarray:
    """呼啸：噪声由低频带过渡到高频带，峰值在 80% 处（对准剪辑点），之后迅速收尾。"""
    n = max(16, int(length * SR))
    t = np.linspace(0, 1, n, dtype=np.float32)
    noise = rng.normal(0, 1, n).astype(np.float32)
    lo, hi = _band(noise, 250, 1400), _band(noise, 1400, 7000)
    sweep = np.clip(t / 0.8, 0, 1)
    sig = lo * (1 - sweep) + hi * sweep * 0.8
    env = np.where(t < 0.8, (t / 0.8) ** 2.2, np.exp(-(t - 0.8) / 0.06))
    return (sig * env).astype(np.float32)


def riser(length: float, root: float, rng) -> np.ndarray:
    """起势：噪声带上扫 + 一个八度的音高爬升，越来越响，恰好在结尾截止。"""
    n = max(16, int(length * SR))
    t = np.linspace(0, 1, n, dtype=np.float32)
    noise = rng.normal(0, 1, n).astype(np.float32)
    bands = [_band(noise, a, b) for a, b in ((400, 1500), (1500, 4000), (4000, 10000))]
    k = t * 2
    sig = bands[0] * np.clip(1 - k, 0, 1) + bands[1] * (1 - np.abs(k - 1)).clip(0, 1) + bands[2] * np.clip(k - 1, 0, 1)
    f = root * 2 ** t
    tone = np.sin(2 * np.pi * np.cumsum(f) / SR) * 0.5
    env = t ** 2.4 * np.clip((1 - t) / 0.004, 0, 1)
    return ((sig * 0.7 + tone) * env).astype(np.float32)


def impact(rng) -> np.ndarray:
    n = int(1.6 * SR)
    t = _t(n)
    f = 38 + 30 * np.exp(-t / 0.08)
    boom = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.55)
    crack = _band(rng.normal(0, 1, n).astype(np.float32), 200, 5000) * np.exp(-t / 0.07) * 0.5
    return (boom + crack).astype(np.float32)


def thump() -> np.ndarray:
    n = int(0.35 * SR)
    t = _t(n)
    f = 52 + 60 * np.exp(-t / 0.03)
    return (np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.14)).astype(np.float32)


def tick(freq: float) -> np.ndarray:
    n = int(0.09 * SR)
    t = _t(n)
    return (np.sin(2 * np.pi * freq * t) * np.exp(-t / 0.022)).astype(np.float32)


def pop(freq: float) -> np.ndarray:
    n = int(0.16 * SR)
    t = _t(n)
    f = freq * (0.55 + 0.45 * np.minimum(1, t / 0.05))
    return (np.sin(2 * np.pi * np.cumsum(f) / SR) * _env(n, 0.004, 0.06)).astype(np.float32)


def shimmer(freqs: Iterable[float]) -> np.ndarray:
    n = int(1.4 * SR)
    t = _t(n)
    sig = sum(np.sin(2 * np.pi * f * t) * (1 + 0.25 * np.sin(2 * np.pi * 5.5 * t)) for f in freqs)
    return (sig * _env(n, 0.02, 0.45)).astype(np.float32)


# ---------------------------------------------------------------- 编曲


def _scale(mode: str) -> tuple[int, ...]:
    return PENTATONIC if mode == "pentatonic" else MAJOR


def _progression(mode: str) -> list[tuple[int, tuple[int, ...]]]:
    """(和弦根音相对主音的半音数, 和弦音程)。大调 I–V–vi–IV；五声调式用开放的挂二和弦。"""
    if mode == "pentatonic":
        return [(0, (0, 7, 14, 16)), (-3, (0, 7, 12, 14)), (5, (0, 7, 14, 16)), (7, (0, 5, 7, 14))]
    return [(0, (0, 4, 7, 12)), (7, (0, 4, 7, 12)), (9, (0, 3, 7, 12)), (5, (0, 4, 7, 12))]


def compose(seconds: float, bpm: int, root: int, mode: str, *, drop: float | None, outro: float | None,
            music: bool, events: list, sfx: bool, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    """返回 (左, 右) 声道。events 为 renderer.SoundEvent 列表。"""
    rng = np.random.default_rng(seed)
    bus = Bus(seconds)
    beat = 60.0 / bpm
    bar = beat * 4
    drop = drop if drop is not None else 0.0
    end_drums = max(drop, seconds - 2 * bar)
    prog = _progression(mode)
    scale = _scale(mode)
    if music:
        k_sig, c_sig, h_sig = kick(rng), clap(rng), hat(rng)
        n_bars = math.ceil(seconds / bar) + 1
        duck = np.ones(bus.n, np.float32)
        for b in range(n_bars):
            t_bar = b * bar
            if t_bar >= seconds:
                break
            rel, chord = prog[b % len(prog)]
            base = root - 12 + rel
            # 铺底（全程；起势前更轻）
            length = min(bar + 0.6, seconds - t_bar + 0.8)
            pl, pr = pad_chord([midi_hz(base + i) for i in chord[:3]], length, rng)
            bus.add_stereo(pl, pr, t_bar, 0.055 if t_bar < drop else 0.075)
            for k in range(8):
                t = t_bar + k * beat / 2
                if t >= seconds - 0.05:
                    break
                after = t >= drop - 1e-3
                drums = after and t < end_drums - 1e-3
                if k % 2 == 0 and drums:  # 四拍底鼓
                    bus.add(k_sig, t, 0.5)
                    i0 = int(t * SR)
                    span = np.arange(min(int(0.32 * SR), bus.n - i0), dtype=np.float32) / SR
                    duck[i0:i0 + len(span)] = np.minimum(duck[i0:i0 + len(span)], 1 - 0.55 * np.exp(-span / 0.12))
                if k in (2, 6) and drums:
                    bus.add(c_sig, t, 0.16)
                if k % 2 == 1 and drums:
                    bus.add(h_sig, t, 0.06, pan=0.25)
                # 琶音弹拨：起势前只在每拍第一个八分音符上，起势后每个八分音符
                if after or k % 2 == 0:
                    tones = [chord[j % len(chord)] for j in (0, 1, 2, 3, 2, 1, 3, 2)]
                    note = base + 24 + tones[k]
                    bus.add(pluck(midi_hz(note), 0.22 if after else 0.35), t, 0.075 if after else 0.06,
                            pan=-0.3 + 0.6 * (k / 7))
                if drums:  # 贝斯八分音符：根音 / 高八度交替
                    bus.add(bass_note(midi_hz(base - 12 + (12 if k % 4 == 3 else 0)), beat / 2 * 0.92), t, 0.2)
        # 侧链：底鼓压低铺底与贝斯（整体乘，听感接近）
        bus.l *= duck
        bus.r *= duck
        # 结尾：主和弦长音
        if outro is not None:
            rel, chord = prog[0]
            pl, pr = pad_chord([midi_hz(root + i) for i in chord], max(1.0, seconds - end_drums + 0.6), rng)
            bus.add_stereo(pl, pr, end_drums, 0.05)
            bus.add(shimmer([midi_hz(root + 24 + i) for i in (0, 7, 12)]), end_drums, 0.03)
    if sfx:
        for ev in events:
            if ev.kind == "whoosh":
                sig = whoosh(ev.length, rng)
            elif ev.kind == "riser":
                sig = riser(ev.length, midi_hz(root + 12), rng)
            elif ev.kind == "impact":
                sig = impact(rng)
            elif ev.kind == "thump":
                sig = thump()
            elif ev.kind == "tick":
                deg = scale[ev.step % len(scale)] + 12 * (ev.step // len(scale))
                sig = tick(midi_hz(root + 36 + deg))
            elif ev.kind == "pop":
                sig = pop(midi_hz(root + 31))
            elif ev.kind == "shimmer":
                sig = shimmer([midi_hz(root + 36 + i) for i in (0, 4 if mode == "major" else 2, 7)])
            else:
                continue
            bus.add(sig, ev.time, ev.gain, ev.pan)
    # 尾部淡出与限幅
    n_out = int(seconds * SR)
    left, right = bus.l[:n_out], bus.r[:n_out]
    fade = int(min(0.6, seconds * 0.1) * SR)
    if fade:
        ramp = np.linspace(1, 0, fade, dtype=np.float32) ** 2
        left[-fade:] *= ramp
        right[-fade:] *= ramp
    peak = max(float(np.abs(left).max(initial=0)), float(np.abs(right).max(initial=0)), 1e-6)
    gain = 0.7 / peak
    return np.tanh(left * gain * 1.1) / 1.1, np.tanh(right * gain * 1.1) / 1.1


def write_wav(path: Path, left: np.ndarray, right: np.ndarray) -> Path:
    data = np.empty(len(left) * 2, np.int16)
    data[0::2] = np.clip(left * 32767, -32768, 32767).astype(np.int16)
    data[1::2] = np.clip(right * 32767, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())
    return path
