"""生图路由：协议由模型目录显式决定，接口由实际绑定参考图数量决定。"""

from __future__ import annotations

from pathlib import Path

from app.providers.image import ImageHttpRequest, build_gemini_request, build_openai_request
from app.providers.image_models import Protocol, protocol_of
from app.settings import Settings


def family_for_model(model: str) -> Protocol:
    """兼容旧调用名：返回模型协议（openai / gemini），未登记的模型直接报错。"""
    return protocol_of(model)


def image_key(settings: Settings) -> str:
    if not settings.image_key:
        raise RuntimeError("未配置 RIGHTAPI_IMAGE_KEY，请在「⚙️ API 密钥与接口配置」中填写")
    return settings.image_key


def route_image_request(
    settings: Settings,
    model: str,
    prompt: str,
    aspect_ratio: str,
    references: list[Path],
    resolution: str | None = None,
    quality: str | None = None,
) -> ImageHttpRequest:
    if len(references) > 3:
        raise ValueError("单次生图最多 3 张参考图")
    tier = (resolution or settings.image_resolution).upper()
    base = settings.rightapi_base_url
    if protocol_of(model) == "openai":
        return build_openai_request(base, model, prompt, aspect_ratio, references, tier,
                                    quality or settings.image_quality)
    return build_gemini_request(base, model, prompt, aspect_ratio, references, tier)
