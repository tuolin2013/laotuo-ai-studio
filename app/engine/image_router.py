"""生图路由：一把 RIGHTAPI_IMAGE_KEY，按所选模型名选择调用格式，按实际绑定参考图数量选择接口。"""

from __future__ import annotations

from pathlib import Path

from app.providers.image import ImageFamily, ImageHttpRequest, build_gemini_request, build_openai_request
from app.settings import Settings


def family_for_model(model: str) -> ImageFamily:
    m = model.lower()
    if m.startswith("gpt-image"):
        return "openai"
    if m.startswith(("nano-banana", "gemini")):
        return "gemini"
    raise ValueError(f"未知的生图模型：{model}")


def image_key(settings: Settings) -> str:
    """GPT-Image 与 Nano-Banana 共用 Image 渠道 Key。"""
    if not settings.image_key:
        raise RuntimeError("未配置 RIGHTAPI_IMAGE_KEY，请在「⚙️ API 密钥与接口配置」中填写")
    return settings.image_key


def route_image_request(
    settings: Settings,
    model: str,
    prompt: str,
    aspect_ratio: str,
    references: list[Path],
    n: int = 1,
    resolution: str | None = None,
) -> ImageHttpRequest:
    if len(references) > 3:
        raise ValueError("单次生图最多 3 张参考图")
    res = resolution or settings.image_resolution
    base = settings.rightapi_base_url
    if family_for_model(model) == "openai":
        return build_openai_request(base, model, prompt, aspect_ratio, references, n, res)
    return build_gemini_request(
        base, model, prompt, aspect_ratio, references, settings.gemini_async, res, settings.gemini_openai_fallback
    )
