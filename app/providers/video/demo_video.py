"""零 Key 演示视频后端：用 ffmpeg 把锁定首帧做成带缓慢推近与合成环境音的 mp4。

只用于本地演示完整交互流程；没有 ffmpeg 时直接退化为把首帧复制为预览图并提示。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from app.providers.video.base import (
    VideoProgress,
    VideoProvider,
    VideoProviderError,
    VideoRequest,
    WarmupResult,
    register_provider,
)

_SIZES = {"9:16": (576, 1024), "16:9": (1024, 576), "1:1": (768, 768), "3:4": (768, 1024), "4:3": (1024, 768)}


@register_provider
class DemoVideoProvider(VideoProvider):
    provider_id = "demo"
    display_name = "本地演示（ffmpeg 合成，非真实 H3）"

    def __init__(self, timeout_seconds: float = 120, step_delay: float = 0.4):
        super().__init__(timeout_seconds)
        self.step_delay = step_delay

    def is_configured(self) -> bool:
        return True

    @staticmethod
    def ffmpeg_path() -> str | None:
        from app.engine.video_cloner import ffmpeg_exe

        return ffmpeg_exe()

    async def warmup(self) -> WarmupResult:
        await asyncio.sleep(self.step_delay)
        return WarmupResult(ready=True, message="演示容器已就绪（模拟冷启动）", elapsed_seconds=self.step_delay)

    async def generate(self, request: VideoRequest) -> AsyncIterator[VideoProgress]:
        yield VideoProgress(stage="warming_up", message="演示模式：模拟唤醒 H3 容器…")
        await asyncio.sleep(self.step_delay)
        yield VideoProgress(stage="submitted", message="演示模式：已提交任务（本地 ffmpeg 合成，不消耗额度）")

        ffmpeg = self.ffmpeg_path()
        if not ffmpeg:
            raise VideoProviderError("演示视频需要 ffmpeg，当前系统未找到；"
                                     "请安装依赖 imageio-ffmpeg 或登录 Modal 使用真实 H3")
        w, h = _SIZES.get(request.aspect_ratio, (768, 768))
        dur = max(1.0, min(float(request.duration_seconds), 15.0))
        fps = 24
        frames = int(dur * fps)
        request.output_dir.mkdir(parents=True, exist_ok=True)
        out = request.output_dir / f"{request.shot_id}_demo_{int(time.time() * 1000)}.mp4"
        vf = (
            f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
            f"zoompan=z='min(zoom+0.0012,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={w}x{h}:fps={fps},format=yuv420p"
        )
        args = [ffmpeg, "-y", "-loglevel", "error", "-loop", "1", "-i", str(request.first_frame_path)]
        if request.with_audio:
            # 粉红噪声 + 低频正弦，模拟原生环境音底噪
            args += ["-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.04:duration={dur}",
                     "-f", "lavfi", "-i", f"sine=frequency=220:duration={dur}"]
            args += ["-filter_complex", f"[0:v]{vf}[v];[1:a][2:a]amix=inputs=2:weights='1 0.15',"
                     f"afade=t=in:d=0.4,afade=t=out:st={max(dur - 0.5, 0)}:d=0.5[a]",
                     "-map", "[v]", "-map", "[a]", "-c:a", "aac", "-b:a", "96k"]
        else:
            args += ["-vf", vf]
        args += ["-t", f"{dur}", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart", str(out)]

        yield VideoProgress(stage="running", message=f"演示模式：正在合成 {dur:g} 秒 {request.aspect_ratio} 视频…")
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_seconds)
        except TimeoutError as e:
            proc.kill()
            raise VideoProviderError("演示视频合成超时") from e
        if proc.returncode != 0 or not out.exists():
            raise VideoProviderError(f"ffmpeg 合成失败：{err.decode(errors='ignore')[-300:]}")
        yield VideoProgress(stage="done", message=f"演示视频已生成：{out.name}", video_path=out)
