"""RightAPI 生图：OpenAI 兼容体系与 Gemini 兼容体系。

本模块只负责「构造请求」与「发送请求」，路由规则见 app/engine/image_router.py。
路由依据是槽位实际绑定的参考图数量：
- OpenAI：0 张 -> /v1/images/generations；>=1 张 -> /v1/images/edits
- Gemini：统一 /v1beta/models/{model}:generateContent（不使用 :streamGenerateContent）
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

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


class ImageAPIError(RuntimeError):
    pass


class RightAPIImageClient:
    """发送已构造好的生图请求，返回图片字节列表。"""

    def __init__(
        self,
        timeout: float = 300,
        task_url_template: str = "",
        poll_interval: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.timeout = timeout
        self.task_url_template = task_url_template
        self.poll_interval = poll_interval
        self._transport = transport

    async def send(self, req: ImageHttpRequest, api_key: str) -> list[bytes]:
        headers = {"Authorization": f"Bearer {api_key}"}
        if req.family == "gemini":
            headers["x-goog-api-key"] = api_key
        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
            try:
                if req.files:
                    resp = await client.post(req.url, data=req.data, files=req.files, headers=headers)
                else:
                    resp = await client.post(req.url, json=req.json, headers=headers)
            except httpx.HTTPError as e:
                raise ImageAPIError(f"生图请求失败：{type(e).__name__}") from e
            if resp.status_code >= 400:
                raise ImageAPIError(f"生图请求失败：HTTP {resp.status_code} {resp.text[:200]}")
            body = resp.json()
            if req.family == "gemini" and body.get("task_id") and not body.get("candidates"):
                body = await self._poll(client, body["task_id"], headers)
            images = extract_openai_images(body) if req.family == "openai" else extract_gemini_images(body)
            out: list[bytes] = []
            for item in images:
                if isinstance(item, bytes):
                    out.append(item)
                else:
                    out.append((await client.get(item)).content)
        if not out:
            raise ImageAPIError("接口未返回图片")
        return out

    async def _poll(self, client: httpx.AsyncClient, task_id: str, headers: dict) -> dict[str, Any]:
        """Gemini "async": true 模式：轮询 task_id 直到返回结果或超时。"""
        if not self.task_url_template:
            raise ImageAPIError("已返回 task_id，但未配置 RIGHTAPI_GEMINI_TASK_URL_TEMPLATE，无法轮询")
        url = self.task_url_template.replace("{task_id}", task_id)
        deadline = asyncio.get_running_loop().time() + self.timeout
        while asyncio.get_running_loop().time() < deadline:
            resp = await client.get(url, headers=headers)
            if resp.status_code < 400:
                body = resp.json()
                status = str(body.get("status", "")).lower()
                result = body.get("result") or body.get("response") or body
                if result.get("candidates"):
                    return result
                if status in {"failed", "error", "cancelled"}:
                    raise ImageAPIError(f"异步生图任务失败：{body.get('error') or status}")
            await asyncio.sleep(self.poll_interval)
        raise ImageAPIError("异步生图任务轮询超时")
