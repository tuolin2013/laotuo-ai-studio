"""fal.ai 视频后端（预留，尚未实现）。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.providers.video.base import (
    VideoProgress,
    VideoProvider,
    VideoProviderError,
    VideoRequest,
    register_provider,
)


@register_provider
class FalAIVideoProvider(VideoProvider):
    provider_id = "fal_ai"
    display_name = "fal.ai（预留）"

    def __init__(self, api_key: str = "", model: str = "", timeout_seconds: float = 600):
        super().__init__(timeout_seconds)
        self.api_key = api_key
        self.model = model

    def is_configured(self) -> bool:
        return bool(self.api_key and self.model)

    async def generate(self, request: VideoRequest) -> AsyncIterator[VideoProgress]:
        raise VideoProviderError("fal.ai 视频接口尚未实现，当前为预留位")
        yield  # pragma: no cover - 使函数成为异步生成器
