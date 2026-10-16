"""可扩展视频生成接口。新增后端只需继承 VideoProvider 并调用 register_provider。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.schemas.common import AspectRatio

DEFAULT_VIDEO_TIMEOUT_SECONDS = 600.0


class VideoProviderError(RuntimeError):
    pass


class VideoProviderNotConfigured(VideoProviderError):
    """缺少地址或 Key。"""


class ManualActionRequired(VideoProviderError):
    """该 Provider 需要人工在外部工具（如 ComfyUI）中完成生成。"""


@dataclass(frozen=True)
class VideoRequest:
    shot_id: str
    prompt: str
    first_frame_path: Path
    aspect_ratio: AspectRatio
    duration_seconds: float
    output_dir: Path
    # H3 原生生成声音；silent 模式时可关闭
    with_audio: bool = True
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class VideoProgress:
    """生成过程中的进度事件，供 Gradio 异步生成器实时 yield 到日志。"""

    stage: Literal["warming_up", "submitted", "running", "downloading", "done", "failed"]
    message: str
    video_path: Path | None = None


@dataclass(frozen=True)
class WarmupResult:
    ready: bool
    message: str
    elapsed_seconds: float = 0.0


class VideoProvider(ABC):
    provider_id: str
    display_name: str
    supports_auto_generation: bool = True

    def __init__(self, timeout_seconds: float = DEFAULT_VIDEO_TIMEOUT_SECONDS):
        self.timeout_seconds = timeout_seconds

    @abstractmethod
    def is_configured(self) -> bool:
        """地址 / Key 是否齐全。"""

    async def warmup(self) -> WarmupResult:
        """Serverless 冷启动预热。默认无需预热。"""
        return WarmupResult(ready=True, message="无需预热")

    @abstractmethod
    def generate(self, request: VideoRequest) -> AsyncIterator[VideoProgress]:
        """异步生成视频，过程中持续产出进度事件，最后一条为 done 或 failed。"""


_REGISTRY: dict[str, type[VideoProvider]] = {}


def register_provider(cls: type[VideoProvider]) -> type[VideoProvider]:
    _REGISTRY[cls.provider_id] = cls
    return cls


def available_providers() -> dict[str, type[VideoProvider]]:
    return dict(_REGISTRY)
