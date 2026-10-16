"""ComfyUI MiniMax All-in-One 人工桥接：提供复制提示词与首帧下载，视频由用户在 ComfyUI 生成后回传。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.providers.video.base import (
    ManualActionRequired,
    VideoProgress,
    VideoProvider,
    VideoRequest,
    register_provider,
)


@register_provider
class ManualComfyUIBridgeProvider(VideoProvider):
    provider_id = "manual_comfyui"
    display_name = "ComfyUI 手动桥接（复制提示词 + 回传视频）"
    supports_auto_generation = False

    def __init__(self, comfyui_url: str = "", timeout_seconds: float = 600):
        super().__init__(timeout_seconds)
        self.comfyui_url = comfyui_url

    def is_configured(self) -> bool:
        # 地址只用于界面跳转，未配置也能复制粘贴使用
        return True

    def instructions(self, request: VideoRequest) -> str:
        target = self.comfyui_url or "你的 ComfyUI MiniMax All-in-One 页面"
        return (
            f"1. 打开 {target}\n"
            f"2. 上传锁定首帧：{request.first_frame_path.name}（比例 {request.aspect_ratio}）\n"
            f"3. 粘贴 H3 提示词，时长设为约 {request.duration_seconds:g} 秒\n"
            "4. 生成完成后，把视频文件拖回本分镜的「回传视频」区域完成绑定"
        )

    async def generate(self, request: VideoRequest) -> AsyncIterator[VideoProgress]:
        yield VideoProgress(stage="submitted", message=self.instructions(request))
        raise ManualActionRequired("请在 ComfyUI 中生成视频后回传绑定")
