"""生图路由：协议与渠道由模型目录显式决定，接口由实际绑定参考图数量决定。"""

from __future__ import annotations

from pathlib import Path

from app.providers.image import ImageHttpRequest, build_gemini_request, build_openai_request
from app.providers.image_models import Protocol, get_model, protocol_of
from app.settings import Settings


def family_for_model(model: str) -> Protocol:
    """兼容旧调用名：返回模型协议（openai / gemini），未登记的模型直接报错。"""
    return protocol_of(model)


def image_key(settings: Settings, model: str | None = None) -> str:
    """按模型所属渠道取 Key：高质量渠道 RIGHTAPI_IMAGE_KEY，普通渠道 RIGHTAPI_IMAGE_STD_KEY。"""
    if model is not None and get_model(model).channel == "std":
        if not settings.image_std_key:
            raise RuntimeError(f"模型 {model} 属于「image 普通」渠道，未配置 RIGHTAPI_IMAGE_STD_KEY，"
                               "请在「⚙️ API 密钥与接口配置」中填写")
        return settings.image_std_key
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
    """model 为目录 ID；请求里发送目录登记的真实模型名，地址用该渠道的 Base URL。"""
    if len(references) > 3:
        raise ValueError("单次生图最多 3 张参考图")
    m = get_model(model)
    tier = (resolution or settings.image_resolution).upper()
    base = settings.image_base_url(model)
    if m.protocol == "openai":
        return build_openai_request(base, m.request_model, prompt, aspect_ratio, references, tier,
                                    quality or settings.image_quality)
    return build_gemini_request(base, m.request_model, prompt, aspect_ratio, references, tier)
