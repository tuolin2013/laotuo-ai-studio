"""Modal 上部署的 VoxCPM2 配音后端（voxcpm2-tts-service / VoxCPM2Service，服务端源码 modal/voxcpm_service.py）。

与 H3 一样通过 Modal SDK 按「应用名 + 类名」调用，不需要 Web 地址、不暴露公网端点：
    TTS = modal.Cls.from_name(app, cls)()
    TTS.synthesize.remote.aio(text=..., control=..., reference_audio=bytes, prompt_text=..., ...)
    TTS.transcribe.remote.aio(audio_bytes)
服务端返回 48kHz 单声道 16-bit WAV 字节；鉴权使用本机 Modal 登录。
"""

from __future__ import annotations

import asyncio
import time

import app.settings as settings_mod
from app.providers.voice.base import (
    VoiceProvider,
    VoiceProviderError,
    VoiceRequest,
    VoiceWarmup,
)

DEFAULT_APP = "voxcpm2-tts-service"
DEFAULT_CLASS = "VoxCPM2Service"


class ModalVoxCPMProvider(VoiceProvider):
    provider_id = "modal_voxcpm2"
    display_name = "Modal · VoxCPM2"
    cold_start_hint = "首次调用需冷启动 GPU 并加载模型，约 2 分钟；之后每段几秒"

    def __init__(self, app_name: str = DEFAULT_APP, class_name: str = DEFAULT_CLASS,
                 timeout_seconds: float = 600, service_factory=None):
        super().__init__(timeout_seconds)
        self.app_name = app_name
        self.class_name = class_name
        # 测试注入：service_factory() -> 拥有 synthesize / transcribe .remote.aio 的对象
        self._service_factory = service_factory

    def _cls(self):
        import modal

        return modal.Cls.from_name(self.app_name, self.class_name)

    def _service(self):
        if self._service_factory is not None:
            return self._service_factory()
        return self._cls()()

    async def warmup(self) -> VoiceWarmup:
        """确认已部署且本机凭证有效（只做查找，不启动 GPU、不产生费用）。"""
        if self._service_factory is not None:
            return VoiceWarmup(True, "测试替身已就绪")
        if not settings_mod._modal_logged_in():
            return VoiceWarmup(False, "本机未登录 Modal：请在终端运行 modal token new")
        start = time.monotonic()
        try:
            await self._cls().hydrate.aio()
        except Exception as e:  # Modal 的 NotFoundError / AuthError 等
            return VoiceWarmup(False, f"找不到已部署的 {self.app_name}.{self.class_name}：{type(e).__name__} {e}",
                               time.monotonic() - start)
        return VoiceWarmup(True, f"已找到 {self.app_name}.{self.class_name}（{self.cold_start_hint}）",
                           time.monotonic() - start)

    async def _call(self, coro, what: str):
        try:
            return await asyncio.wait_for(coro, timeout=self.timeout_seconds)
        except TimeoutError as e:
            raise VoiceProviderError(f"VoxCPM2 {what}超过 {self.timeout_seconds:.0f} 秒未完成，已取消") from e
        except VoiceProviderError:
            raise
        except Exception as e:  # 服务端异常（参数错误、参考音频无法解码等）原样带回
            raise VoiceProviderError(f"VoxCPM2 {what}失败：{type(e).__name__}: {e}") from e

    async def synthesize(self, request: VoiceRequest) -> bytes:
        ref = request.reference_path.read_bytes() if request.reference_path else None
        data = await self._call(self._service().synthesize.remote.aio(
            text=request.text,
            control=request.control or None,
            reference_audio=ref,
            prompt_text=request.prompt_text or None,
            cfg_value=request.cfg_value,
            inference_timesteps=request.inference_timesteps,
            normalize=request.normalize,
            denoise=request.denoise,
            seed=request.seed,
        ), "合成")
        if not data:
            raise VoiceProviderError("VoxCPM2 返回了空音频")
        return data

    async def transcribe(self, audio: bytes) -> str:
        return str(await self._call(self._service().transcribe.remote.aio(audio), "识别") or "").strip()

    async def normalize_preview(self, texts: list[str]) -> list[str] | None:
        """调用服务端 preview_normalize（只用 CPU，不启动 GPU），与合成时的读法规则完全一致。"""
        if self._service_factory is not None:
            fn = getattr(self._service_factory(), "preview_normalize", None)
            return list(await fn.remote.aio(texts)) if fn is not None else None
        import modal

        fn = modal.Function.from_name(self.app_name, "preview_normalize")
        return list(await self._call(fn.remote.aio(texts), "读法预览"))
