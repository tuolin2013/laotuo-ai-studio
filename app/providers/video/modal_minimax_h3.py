"""Modal 上部署的 MiniMax H3 图生视频（原生音画一体）。

请求 / 响应字段为通用约定，需与实际部署的 Modal 端点对齐：
- 请求：POST JSON {prompt, first_frame_base64, aspect_ratio, duration_seconds, with_audio}
- 响应：video/* 二进制，或 JSON {video_url} / {video_base64}
"""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncIterator

import httpx

from app.providers.video.base import (
    VideoProgress,
    VideoProvider,
    VideoProviderError,
    VideoProviderNotConfigured,
    VideoRequest,
    WarmupResult,
    register_provider,
)


@register_provider
class ModalMiniMaxH3Provider(VideoProvider):
    provider_id = "modal_minimax_h3"
    display_name = "Modal · MiniMax H3（原生音画）"

    def __init__(self, url: str = "", warmup_url: str = "", token: str = "", timeout_seconds: float = 600):
        super().__init__(timeout_seconds)
        self.url = url
        self.warmup_url = warmup_url
        self.token = token

    def is_configured(self) -> bool:
        return bool(self.url)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def warmup(self) -> WarmupResult:
        """Modal Serverless 冷启动可能需要几十秒到数分钟，先发轻量请求唤醒容器。"""
        if not self.warmup_url:
            return WarmupResult(ready=True, message="未配置预热地址，跳过预热")
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.get(self.warmup_url, headers=self._headers())
            ok = resp.status_code < 400
            return WarmupResult(ok, f"预热返回 HTTP {resp.status_code}", time.monotonic() - start)
        except httpx.HTTPError as e:
            return WarmupResult(False, f"预热失败：{type(e).__name__}", time.monotonic() - start)

    async def generate(self, request: VideoRequest) -> AsyncIterator[VideoProgress]:
        if not self.is_configured():
            raise VideoProviderNotConfigured("未配置 MODAL_MINIMAX_H3_URL")
        yield VideoProgress(stage="warming_up", message="正在唤醒 Modal 容器（冷启动可能较慢）…")
        warm = await self.warmup()
        yield VideoProgress(stage="warming_up", message=f"{warm.message}（{warm.elapsed_seconds:.1f}s）")

        payload = {
            "prompt": request.prompt,
            "first_frame_base64": base64.b64encode(request.first_frame_path.read_bytes()).decode(),
            "aspect_ratio": request.aspect_ratio,
            "duration_seconds": request.duration_seconds,
            "with_audio": request.with_audio,
            **request.extra,
        }
        yield VideoProgress(stage="submitted", message=f"已提交 H3 任务，最长等待 {self.timeout_seconds:.0f} 秒")
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                resp = await client.post(self.url, json=payload, headers=self._headers())
                resp.raise_for_status()
            except httpx.HTTPError as e:
                raise VideoProviderError(f"Modal H3 请求失败：{type(e).__name__}") from e

            yield VideoProgress(stage="downloading", message="H3 已返回，正在保存视频…")
            if resp.headers.get("content-type", "").startswith("video/"):
                data = resp.content
            else:
                body = resp.json()
                if body.get("video_base64"):
                    data = base64.b64decode(body["video_base64"])
                elif body.get("video_url"):
                    data = (await client.get(body["video_url"])).content
                else:
                    raise VideoProviderError("Modal H3 响应中没有 video_base64 / video_url")

        request.output_dir.mkdir(parents=True, exist_ok=True)
        out = request.output_dir / f"{request.shot_id}_{int(time.time())}.mp4"
        out.write_bytes(data)
        yield VideoProgress(stage="done", message=f"视频已保存：{out.name}", video_path=out)
