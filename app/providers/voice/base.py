"""可扩展配音（TTS）后端接口。新增后端只需继承 VoiceProvider，并在 EngineRuntime.voice_provider() 中返回。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

DEFAULT_VOICE_TIMEOUT_SECONDS = 600.0


class VoiceProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class VoiceRequest:
    """一次合成请求。reference_path 为空时是声音设计（每次随机一个声音，由 control 描述音色）。"""

    text: str
    # 风格 / 音色指令，服务端拼成 "(control)text"；高保真克隆（prompt_text 非空）时不生效
    control: str = ""
    # 参考音频（16kHz 单声道 wav）：克隆音色
    reference_path: Path | None = None
    # 参考音频的逐字文本：提供时走高保真克隆
    prompt_text: str = ""
    cfg_value: float = 2.0
    inference_timesteps: int = 10
    normalize: bool = False
    denoise: bool = False
    seed: int | None = None

    @property
    def mode_label(self) -> str:
        if self.reference_path is None:
            return "声音设计"
        return "高保真克隆" if self.prompt_text else "可控克隆"


@dataclass(frozen=True)
class VoiceWarmup:
    ready: bool
    message: str
    elapsed_seconds: float = 0.0


class VoiceProvider(ABC):
    provider_id: str
    display_name: str
    # 冷启动提示：心跳日志里告诉用户大概要等多久
    cold_start_hint: str = ""

    def __init__(self, timeout_seconds: float = DEFAULT_VOICE_TIMEOUT_SECONDS):
        self.timeout_seconds = timeout_seconds

    async def warmup(self) -> VoiceWarmup:
        """确认服务可用（不产生费用）。默认无需检查。"""
        return VoiceWarmup(ready=True, message="无需检查")

    async def normalize_preview(self, texts: list[str]) -> list[str] | None:
        """预览「数字 / 单位转读法」后的文字；后端不支持时返回 None。"""
        return None

    @abstractmethod
    async def synthesize(self, request: VoiceRequest) -> bytes:
        """合成一段语音，返回 WAV 字节。"""

    @abstractmethod
    async def transcribe(self, audio: bytes) -> str:
        """识别音频里说的话（用作高保真克隆的参考文本）。"""
