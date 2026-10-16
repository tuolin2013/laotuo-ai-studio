"""RightAPI 生图：OpenAI 兼容体系与 Gemini 兼容体系。

本模块只负责「构造请求」与「发送请求」，路由规则见 app/engine/image_router.py。
路由依据是槽位实际绑定的参考图数量：
- OpenAI：0 张 -> /v1/images/generations；>=1 张 -> /v1/images/edits
- Gemini：统一 /v1beta/models/{model}:generateContent（不使用 :streamGenerateContent）
"""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ImageFamily = Literal["openai", "gemini"]

# 比例 -> OpenAI size 参数
OPENAI_SIZES = {
    "1:1": "1024x1024",
    "3:4": "1024x1536",
    "2:3": "1024x1536",
    "9:16": "1024x1536",
    "4:3": "1536x1024",
    "3:2": "1536x1024",
    "16:9": "1536x1024",
}


@dataclass(frozen=True)
class ImageHttpRequest:
    """已构造好的 HTTP 请求描述，便于测试与日志（不含 Key）。"""

    family: ImageFamily
    method: str
    url: str
    json: dict[str, Any] | None = None
    data: dict[str, Any] | None = None
    files: list[tuple[str, tuple[str, bytes, str]]] = field(default_factory=list)


def _mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "image/png"


def build_openai_request(
    base_url: str, model: str, prompt: str, aspect_ratio: str, references: list[Path], n: int = 1
) -> ImageHttpRequest:
    size = OPENAI_SIZES.get(aspect_ratio, "1024x1024")
    if not references:
        return ImageHttpRequest(
            family="openai",
            method="POST",
            url=f"{base_url}/v1/images/generations",
            json={"model": model, "prompt": prompt, "size": size, "n": n},
        )
    files = [("image[]", (p.name, p.read_bytes(), _mime(p))) for p in references]
    return ImageHttpRequest(
        family="openai",
        method="POST",
        url=f"{base_url}/v1/images/edits",
        data={"model": model, "prompt": prompt, "size": size, "n": str(n)},
        files=files,
    )


def build_gemini_request(
    base_url: str,
    model: str,
    prompt: str,
    aspect_ratio: str,
    references: list[Path],
    use_async: bool = False,
) -> ImageHttpRequest:
    parts: list[dict[str, Any]] = [
        {"inline_data": {"mime_type": _mime(p), "data": base64.b64encode(p.read_bytes()).decode()}}
        for p in references
    ]
    parts.append({"text": prompt})
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"],
            "imageConfig": {"aspectRatio": aspect_ratio},
        },
    }
    if use_async:
        body["async"] = True
    return ImageHttpRequest(
        family="gemini",
        method="POST",
        url=f"{base_url}/v1beta/models/{model}:generateContent",
        json=body,
    )


def extract_openai_images(body: dict[str, Any]) -> list[bytes | str]:
    """返回图片字节（b64_json）或 URL 字符串。"""
    out: list[bytes | str] = []
    for item in body.get("data") or []:
        if item.get("b64_json"):
            out.append(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            out.append(item["url"])
    return out


def extract_gemini_images(body: dict[str, Any]) -> list[bytes]:
    out: list[bytes] = []
    for cand in body.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = part.get("inline_data") or part.get("inlineData")
            if inline and inline.get("data"):
                out.append(base64.b64decode(inline["data"]))
    return out
