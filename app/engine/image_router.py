"""生图路由：根据模型选择体系与 Key，根据实际绑定参考图数量选择接口。"""

from __future__ import annotations

from pathlib import Path

from app.providers.image import ImageFamily, ImageHttpRequest, build_gemini_request, build_openai_request
from app.settings import Settings


def family_for_model(model: str) -> ImageFamily:
    if model.startswith("gpt-image"):
        return "openai"
    if model.startswith("gemini"):
        return "gemini"
    raise ValueError(f"未知的生图模型：{model}")


def key_for_family(settings: Settings, family: ImageFamily) -> str:
    """两大生图体系使用各自独立的 Key，互不混用。"""
    key = settings.openai_image_key if family == "openai" else settings.gemini_image_key
    if not key:
        name = "RIGHTAPI_OPENAI_IMAGE_KEY" if family == "openai" else "RIGHTAPI_GEMINI_IMAGE_KEY"
        raise RuntimeError(f"未配置 {name}，请在 .env 中填写")
    return key


def route_image_request(
    settings: Settings, model: str, prompt: str, aspect_ratio: str, references: list[Path], n: int = 1
) -> ImageHttpRequest:
    if len(references) > 3:
        raise ValueError("单次生图最多 3 张参考图")
    if family_for_model(model) == "openai":
        return build_openai_request(settings.rightapi_base_url, model, prompt, aspect_ratio, references, n)
    return build_gemini_request(
        settings.rightapi_base_url, model, prompt, aspect_ratio, references, settings.gemini_async
    )
