"""Modal 上部署的 MiniMax H3（minimax-h3-diffusers-service / H3BatchRunner）原生音画一体视频后端。

通过 Modal SDK 按「应用名 + 类名」直接调用，不需要 Web 地址、不暴露公网端点：
    Runner = modal.Cls.from_name(app, cls)
    Runner(workflow="ref2va").render_single_shot.remote(prompt, images, frames, seed, steps, aspect_ratio)

调用约定（与服务端 modal/h3_diffusers_service.py 对齐）：
- 有锁定首帧时走 ref2va：images[0] = 锁定首帧（<Picture 1>），其后为该分镜绑定的参考图（<Picture 2>…，最多 9 张）。
- 帧数按 24fps 换算；服务端会把帧数调整到 17n+5 且在 124~345（约 5.2~14.4 秒）之间。
- 鉴权使用本机 Modal 登录（~/.modal.toml 或 MODAL_TOKEN_ID / MODAL_TOKEN_SECRET 环境变量）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path

from app.providers.video.base import (
    VideoProgress,
    VideoProvider,
    VideoProviderError,
    VideoProviderNotConfigured,
    VideoRequest,
    WarmupResult,
    register_provider,
)

FPS = 24
MIN_FRAMES, MAX_FRAMES = 124, 345
MAX_REF_IMAGES = 9
DEFAULT_APP = "minimax-h3-diffusers-service"
DEFAULT_CLASS = "H3BatchRunner"
HEARTBEAT_SECONDS = 20


def modal_logged_in() -> bool:
    """本机是否已登录 Modal（不读取、不打印任何 token 内容）。"""
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return True
    return (Path.home() / ".modal.toml").exists()


def h3_frames(duration_seconds: float) -> int:
    """镜头时长 → H3 接受的帧数（17n+5，124~345）。"""
    import math

    n = math.ceil((max(round(duration_seconds * FPS), MIN_FRAMES) - 5) / 17)
    return min(17 * n + 5, MAX_FRAMES)


def build_ref2va_prompt(prompt: str, ref_roles: list[str]) -> str:
    """在 H3 提示词前声明每张参考图的角色：<Picture 1> 为锁定首帧，其后为商品等参考图。"""
    lines = ["<Picture 1> 是本镜头锁定的首帧画面：视频从这一帧开始，构图、商品包装形状、颜色、文字与 logo 以它为准。"]
    for i, role in enumerate(ref_roles, start=2):
        lines.append(f"<Picture {i}> 是商品参考图（{role}），商品外观必须与它一致。")
    return "\n".join(lines) + "\n" + prompt


def stable_seed(text: str) -> int:
    """同一剧本使用同一种子，全片画风更统一。"""
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


@register_provider
class ModalMiniMaxH3Provider(VideoProvider):
    provider_id = "modal_minimax_h3"
    display_name = "Modal · MiniMax H3（原生音画）"

    def __init__(
        self,
        app_name: str = DEFAULT_APP,
        class_name: str = DEFAULT_CLASS,
        steps: int = 30,
        timeout_seconds: float = 2400,
        runner_factory=None,
    ):
        super().__init__(timeout_seconds)
        self.app_name = app_name
        self.class_name = class_name
        self.steps = steps
        # 测试注入：runner_factory(workflow) -> 拥有 render_single_shot.remote.aio 的对象
        self._runner_factory = runner_factory

    def is_configured(self) -> bool:
        return bool(self.app_name and self.class_name) and (self._runner_factory is not None or modal_logged_in())

    def _cls(self):
        import modal

        return modal.Cls.from_name(self.app_name, self.class_name)

    def _runner(self, workflow: str):
        if self._runner_factory is not None:
            return self._runner_factory(workflow)
        return self._cls()(workflow=workflow)

    async def warmup(self) -> WarmupResult:
        """确认已部署且本机凭证有效（只做查找，不启动 GPU、不产生费用）。"""
        if self._runner_factory is not None:
            return WarmupResult(ready=True, message="测试替身已就绪")
        if not modal_logged_in():
            return WarmupResult(ready=False, message="本机未登录 Modal：请在终端运行 modal token new")
        start = time.monotonic()
        try:
            await self._cls().hydrate.aio()
        except Exception as e:  # Modal 的 NotFoundError / AuthError 等
            return WarmupResult(False, f"找不到已部署的 {self.app_name}.{self.class_name}：{type(e).__name__} {e}",
                                time.monotonic() - start)
        return WarmupResult(
            True,
            f"已找到 {self.app_name}.{self.class_name}（GPU 按需启动：首次渲染需冷启动并加载权重，约 1~3 分钟）",
            time.monotonic() - start,
        )

    async def generate(self, request: VideoRequest) -> AsyncIterator[VideoProgress]:
        if not self.is_configured():
            raise VideoProviderNotConfigured("本机未登录 Modal，无法调用 MiniMax H3")
        refs: list[Path] = list(request.extra.get("reference_paths") or [])
        images = [request.first_frame_path.read_bytes(), *[p.read_bytes() for p in refs]][:MAX_REF_IMAGES]
        roles = list(request.extra.get("reference_roles") or [p.stem for p in refs])[: len(images) - 1]
        prompt = build_ref2va_prompt(request.prompt, roles)
        frames = h3_frames(request.duration_seconds)
        seed = int(request.extra.get("seed") or stable_seed(request.shot_id))
        actual = frames / FPS
        note = f"（H3 最短 {MIN_FRAMES / FPS:.1f} 秒，分镜 {request.duration_seconds:g} 秒已调整）" \
            if actual > request.duration_seconds + 0.5 else ""
        yield VideoProgress(
            stage="submitted",
            message=(f"提交 {self.app_name}.{self.class_name}(workflow=ref2va)：{len(images)} 张图"
                     f"（首帧 + {len(images) - 1} 张商品参考）| {request.aspect_ratio} | {frames} 帧 ≈ {actual:.1f} 秒"
                     f"{note} | {self.steps} 步 | seed={seed}"),
        )
        runner = self._runner("ref2va")
        task = asyncio.ensure_future(runner.render_single_shot.remote.aio(
            prompt, images, frames, seed, self.steps, request.aspect_ratio
        ))
        start = time.monotonic()
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECONDS)
                if done:
                    break
                elapsed = time.monotonic() - start
                if elapsed > self.timeout_seconds:
                    task.cancel()
                    raise VideoProviderError(f"H3 渲染超过 {self.timeout_seconds:.0f} 秒未完成，已取消")
                yield VideoProgress(stage="running",
                                    message=f"H3 渲染中…已等待 {elapsed:.0f} 秒（冷启动 + 推理通常 5~8 分钟）")
            data = task.result()
        except VideoProviderError:
            raise
        except Exception as e:  # 服务端异常（参数错误、OOM 等）原样带回
            raise VideoProviderError(f"Modal H3 调用失败：{type(e).__name__}: {e}") from e
        finally:
            if not task.done():
                task.cancel()
        if not data:
            raise VideoProviderError("Modal H3 返回了空视频")
        yield VideoProgress(stage="downloading", message=f"H3 已返回 {len(data) / 1e6:.1f} MB，正在保存…")
        request.output_dir.mkdir(parents=True, exist_ok=True)
        out = request.output_dir / f"{request.shot_id}_h3_{int(time.time())}.mp4"
        out.write_bytes(data)
        yield VideoProgress(stage="done", message=f"视频已保存：{out.name}（用时 {time.monotonic() - start:.0f} 秒）",
                            video_path=out)
