"""RightAPI 生图（Image 渠道，一把 RIGHTAPI_IMAGE_KEY 同时服务两种调用格式）。

按所选模型自动分流，路由依据是槽位「实际绑定」的参考图数量：
- GPT-Image（gpt-image-*）：0 张 -> /v1/images/generations；>=1 张 -> /v1/images/edits
- Nano-Banana / Gemini：优先原生 /v1beta/models/{model}:generateContent（不使用 :streamGenerateContent），
  0 张只传文本，>=1 张传 base64 inline_data + 文本；同步返回图片或 "async": true 返回 task_id 轮询。
  若原生端点返回 404/405（渠道只开放 OpenAI 兼容端点），自动回退 /v1/images/generations 或 /v1/images/edits。
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

# 比例 -> GPT-Image size 参数
OPENAI_SIZES = {
    "1:1": "1024x1024",
    "3:4": "1024x1536",
    "2:3": "1024x1536",
    "9:16": "1024x1536",
    "4:3": "1536x1024",
    "3:2": "1536x1024",
    "16:9": "1536x1024",
}
# 1K/2K/4K -> GPT-Image quality
OPENAI_QUALITY = {"1K": "medium", "2K": "high", "4K": "high"}


@dataclass(frozen=True)
class ImageHttpRequest:
    """已构造好的 HTTP 请求描述，便于测试与日志（不含 Key）。"""

    family: ImageFamily
    method: str
    url: str
    json: dict[str, Any] | None = None
    data: dict[str, Any] | None = None
    files: list[tuple[str, tuple[str, bytes, str]]] = field(default_factory=list)
    # Nano-Banana 原生端点不可用时的 OpenAI 兼容回退请求
    fallback: ImageHttpRequest | None = None


def _mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "image/png"


def build_openai_request(
    base_url: str,
    model: str,
    prompt: str,
    aspect_ratio: str,
    references: list[Path],
    n: int = 1,
    resolution: str | None = None,
) -> ImageHttpRequest:
    size = OPENAI_SIZES.get(aspect_ratio, "1024x1024")
    extra = {"quality": OPENAI_QUALITY[resolution]} if resolution in OPENAI_QUALITY else {}
    if not references:
        return ImageHttpRequest(
            family="openai",
            method="POST",
            url=f"{base_url}/v1/images/generations",
            json={"model": model, "prompt": prompt, "size": size, "n": n, **extra},
        )
    files = [("image[]", (p.name, p.read_bytes(), _mime(p))) for p in references]
    return ImageHttpRequest(
        family="openai",
        method="POST",
        url=f"{base_url}/v1/images/edits",
        data={"model": model, "prompt": prompt, "size": size, "n": str(n), **extra},
        files=files,
    )


def build_gemini_request(
    base_url: str,
    model: str,
    prompt: str,
    aspect_ratio: str,
    references: list[Path],
    use_async: bool = False,
    resolution: str = "2K",
    openai_fallback: bool = True,
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
            "imageConfig": {"aspectRatio": aspect_ratio, "imageSize": resolution},
        },
    }
    if use_async:
        body["async"] = True
    fallback = None
    if openai_fallback:
        fallback = build_openai_request(base_url, model, prompt, aspect_ratio, references, 1, resolution)
    return ImageHttpRequest(
        family="gemini",
        method="POST",
        url=f"{base_url}/v1beta/models/{model}:generateContent",
        json=body,
        fallback=fallback,
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

    @staticmethod
    def _headers(api_key: str) -> dict[str, str]:
        # 同一把 Key：Bearer 用于 OpenAI 兼容端点，x-goog-api-key 用于 Gemini 原生端点，两者都带上最稳妥
        return {"Authorization": f"Bearer {api_key}", "x-goog-api-key": api_key}

    async def send(self, req: ImageHttpRequest, api_key: str) -> list[bytes]:
        if not api_key:
            raise ImageAPIError("未配置 RIGHTAPI_IMAGE_KEY")
        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
            resp = await self._post(client, req, api_key)
            if resp.status_code in (404, 405) and req.fallback is not None:
                # 渠道未开放 Gemini 原生端点：改用 OpenAI Images 兼容端点
                req = req.fallback
                resp = await self._post(client, req, api_key)
            if resp.status_code >= 400:
                raise ImageAPIError(f"生图请求失败：HTTP {resp.status_code} {resp.text[:200]}")
            try:
                body = resp.json()
            except ValueError as e:
                raise ImageAPIError("生图接口返回的不是 JSON") from e
            if req.family == "gemini" and body.get("task_id") and not body.get("candidates"):
                body = await self._poll(client, str(body["task_id"]), api_key)
            images = extract_openai_images(body) if req.family == "openai" else extract_gemini_images(body)
            out: list[bytes] = []
            for item in images:
                if isinstance(item, bytes):
                    out.append(item)
                    continue
                # 返回的是图片 URL：下载后由调用方保存到项目目录
                dl = await client.get(item)
                if dl.status_code >= 400:
                    raise ImageAPIError(f"下载生成图片失败：HTTP {dl.status_code}")
                out.append(dl.content)
        if not out:
            raise ImageAPIError("接口未返回图片")
        return out

    async def _post(self, client: httpx.AsyncClient, req: ImageHttpRequest, api_key: str) -> httpx.Response:
        try:
            if req.files:
                return await client.post(req.url, data=req.data, files=req.files, headers=self._headers(api_key))
            return await client.post(req.url, json=req.json, headers=self._headers(api_key))
        except httpx.HTTPError as e:
            raise ImageAPIError(f"生图请求失败：{type(e).__name__}") from e

    async def _poll(self, client: httpx.AsyncClient, task_id: str, api_key: str) -> dict[str, Any]:
        """Gemini "async": true 模式：轮询 task_id 直到返回结果或超时。"""
        if not self.task_url_template:
            raise ImageAPIError("已返回 task_id，但未配置 RIGHTAPI_GEMINI_TASK_URL_TEMPLATE，无法轮询")
        url = self.task_url_template.replace("{task_id}", task_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        while loop.time() < deadline:
            resp = await client.get(url, headers=self._headers(api_key))
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
