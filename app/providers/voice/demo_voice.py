"""零 Key 演示配音后端：本地用纯 Python 合成「按字发声」的提示音，时长与真实口播相近。

只用于本地演示完整交互流程（试听、逐段生成、合成完整配音、导出字幕）；不是真实人声。
同一音色描述 / 同一参考音频得到同一音高，换音色时音高随之变化，便于在演示中听出「音色是否一致」。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import re
import wave
from array import array
from functools import lru_cache

from app.providers.voice.base import VoiceProvider, VoiceRequest, VoiceWarmup
from app.schemas.voice import display_text

SAMPLE_RATE = 48000
SYLLABLE_SECONDS = 0.2
GAP_SECONDS = 0.02
PAUSE_SECONDS = {"，": 0.18, ",": 0.18, "、": 0.12, "。": 0.32, "！": 0.32, "？": 0.32, "!": 0.32, "?": 0.32,
                 "；": 0.25, ";": 0.25, "…": 0.4, "：": 0.15, ":": 0.15}
_UNIT = re.compile(r"[A-Za-z]+|\d|[^\s\W]|[，,、。！？!?；;…：:]", re.UNICODE)
DEMO_TRANSCRIPT = "演示模式无法识别真实语音，请手动填写参考音频里说的话"


@lru_cache(maxsize=64)
def _syllable(freq: int, amplitude: int = 9000) -> bytes:
    """一个「音节」：基频 + 两个泛音，前后渐入渐出，避免爆音。"""
    n = int(SAMPLE_RATE * SYLLABLE_SECONDS)
    fade = int(SAMPLE_RATE * 0.03)
    out = array("h")
    for i in range(n):
        env = min(1.0, i / fade, (n - i) / fade)
        t = i / SAMPLE_RATE
        v = (math.sin(2 * math.pi * freq * t) + 0.35 * math.sin(4 * math.pi * freq * t)
             + 0.15 * math.sin(6 * math.pi * freq * t)) / 1.5
        out.append(int(amplitude * env * v))
    return out.tobytes()


def _silence(seconds: float) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * seconds)


def _base_pitch(req: VoiceRequest) -> int:
    """同一音色 → 同一基频：有参考音频按音频内容，声音设计按描述。"""
    key = req.reference_path.read_bytes()[:4096] if req.reference_path else req.control.encode("utf-8")
    return 150 + int(hashlib.sha256(key).hexdigest()[:4], 16) % 120


def render_demo_wav(req: VoiceRequest) -> bytes:
    base = _base_pitch(req)
    seed = req.seed or 0
    pcm = bytearray(_silence(0.15))
    for k, unit in enumerate(_UNIT.findall(display_text(req.text))):
        if unit in PAUSE_SECONDS:
            pcm += _silence(PAUSE_SECONDS[unit])
            continue
        # 音高在基频附近起伏，像说话的抑扬顿挫；外文单词按音节数估算长度
        freq = base + ((k * 7 + seed) % 5 - 2) * 12
        repeat = max(1, len(unit) // 3) if unit.isascii() and unit.isalpha() else 1
        for _ in range(repeat):
            pcm += _syllable(freq) + _silence(GAP_SECONDS)
    pcm += _silence(0.2)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(pcm))
    return buf.getvalue()


class DemoVoiceProvider(VoiceProvider):
    provider_id = "demo_voice"
    display_name = "本地演示（提示音合成，非真实人声）"
    cold_start_hint = "本地合成，无需等待"

    def __init__(self, step_delay: float = 0.35):
        super().__init__()
        self.step_delay = step_delay

    async def warmup(self) -> VoiceWarmup:
        return VoiceWarmup(True, "演示模式：本地合成提示音，不调用 Modal、不产生费用")

    async def synthesize(self, request: VoiceRequest) -> bytes:
        await asyncio.sleep(self.step_delay)
        return await asyncio.to_thread(render_demo_wav, request)

    async def transcribe(self, audio: bytes) -> str:
        await asyncio.sleep(self.step_delay)
        return DEMO_TRANSCRIPT
