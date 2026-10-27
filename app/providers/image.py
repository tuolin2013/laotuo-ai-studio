"""RightAPI 画图接口（严格按官方文档 https://docs.rightapi.ai/docs/rc_draw/ 实现）。

两种协议，模型决定协议，不能混用（用错会 404 / 400）。模型与渠道见 app/providers/image_models.py：

- OpenAI 生成（GPT Image 系列）：
  0 张参考图 POST /v1/images/generations；≥1 张 POST /v1/images/edits
- Gemini 生成（Nano Banana 系列）：
  POST /v1beta/models/{model}:generateContent（文生图与参考图编辑同一接口；禁止 :streamGenerateContent）

统一异步：请求体带 "async": true → 返回 task_id → 轮询 GET /v1/tasks/{task_id}。
完成时不再带 status：OpenAI 返回 data[].url；Gemini 返回 candidates[].content.parts[].text（文本即图片 URL）。
结果只有图片 URL（不返回 base64），由本模块下载为字节。
"""

from __future__ import annotations

import asyncio
import base64
import io
import math
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from PIL import Image

from app.providers.image_models import CATALOG

ImageFamily = Literal["openai", "gemini"]
ProgressCallback = Callable[[str], Awaitable[None]]

# GPT Image 尺寸约束（官方文档「尺寸规则」）
GPT_MAX_EDGE = 3840
GPT_MIN_PIXELS = 655_360
GPT_MAX_PIXELS = 8_294_400
GPT_EXPERIMENTAL_PIXELS = 2560 * 1440
# 各档位的长边目标；正方形 1K 用 1024
TIER_LONG_EDGE = {"1K": 1536, "2K": 2048, "4K": 3840}
GPT_QUALITIES = ("auto", "low", "medium", "high", "xhigh", "max")
# xhigh / max 只能用于 2.5 模型
GPT_25_ONLY_QUALITIES = ("xhigh", "max")
TERMINAL_FAILED = {"failed", "expired", "canceled", "cancelled"}
# 参考图压缩阈值：长边超过 2048 或文件超过 4MB 时压缩，明显加快生成
REF_MAX_EDGE = 2048
REF_MAX_BYTES = 4 * 1024 * 1024


def _ratio(aspect_ratio: str) -> tuple[int, int]:
    try:
        w, h = (int(x) for x in aspect_ratio.split(":"))
    except ValueError as e:
        raise ValueError(f"无效的画面比例：{aspect_ratio}") from e
    if w <= 0 or h <= 0 or max(w, h) / min(w, h) > 3:
        raise ValueError(f"画面比例需在 3:1 以内：{aspect_ratio}")
    return w, h


def _floor16(x: float) -> int:
    return max(16, int(x) // 16 * 16)


def gpt_image_size(aspect_ratio: str, tier: str) -> str:
    """把比例 + 1K/2K/4K 档位换算成 GPT Image 合法像素尺寸（16 的倍数、长边 ≤3840、总像素在范围内）。"""
    rw, rh = _ratio(aspect_ratio)
    long_edge = TIER_LONG_EDGE.get(tier, 2048)
    if rw == rh and tier == "1K":
        long_edge = 1024
    k = long_edge / max(rw, rh)
    # 总像素上限
    k = min(k, math.sqrt(GPT_MAX_PIXELS / (rw * rh)))
    w, h = _floor16(rw * k), _floor16(rh * k)
    while w * h < GPT_MIN_PIXELS:
        w, h = w + 16 * rw // math.gcd(rw, rh), h + 16 * rh // math.gcd(rw, rh)
    assert max(w, h) <= GPT_MAX_EDGE and w * h <= GPT_MAX_PIXELS
    return f"{w}x{h}"


def gpt_quality_for(model: str, quality: str) -> str:
    """xhigh / max 只有目录里标记 supports_xhigh 的模型可用（gpt-image-2.5-flare / sunburst），
    其他模型自动降为 high，避免 400。"""
    q = quality if quality in GPT_QUALITIES else "high"
    m = CATALOG.get(model)
    if q in GPT_25_ONLY_QUALITIES and not (m is not None and m.supports_xhigh):
        return "high"
    return q


def is_experimental_size(size: str) -> bool:
    w, h = (int(x) for x in size.split("x"))
    return w * h > GPT_EXPERIMENTAL_PIXELS


def encode_reference(path: Path) -> tuple[str, str]:
    """返回 (mime_type, 纯 base64)。过大的参考图先压缩。"""
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as im:
        fmt = (im.format or "PNG").upper()
        too_big = max(im.size) > REF_MAX_EDGE or len(raw) > REF_MAX_BYTES
        if not too_big and fmt in {"PNG", "JPEG", "WEBP"}:
            return f"image/{fmt.lower()}", base64.b64encode(raw).decode()
        im = im.copy()
        im.thumbnail((REF_MAX_EDGE, REF_MAX_EDGE))
        buf = io.BytesIO()
        if im.mode in ("RGBA", "LA", "P"):
            im.save(buf, "PNG", optimize=True)
            mime = "image/png"
        else:
            im.convert("RGB").save(buf, "JPEG", quality=92)
            mime = "image/jpeg"
    return mime, base64.b64encode(buf.getvalue()).decode()


@dataclass(frozen=True)
class ImageHttpRequest:
    """已构造好的 HTTP 请求描述（不含 Key），便于测试与日志。"""

    family: ImageFamily
    url: str
    json: dict[str, Any]
    size_label: str = ""


def build_openai_request(
    base_url: str,
    model: str,
    prompt: str,
    aspect_ratio: str,
    references: list[Path],
    tier: str = "2K",
    quality: str = "high",
) -> ImageHttpRequest:
    """GPT Image：0 张参考图 → generations；≥1 张 → edits（JSON + data URL 传图）。每次只出 1 张。"""
    size = gpt_image_size(aspect_ratio, tier)
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size,
        "quality": gpt_quality_for(model, quality),
        "output_format": "png",
        "async": True,
    }
    if not references:
        return ImageHttpRequest("openai", f"{base_url}/v1/images/generations", body, size)
    images = []
    for p in references:
        mime, b64 = encode_reference(p)
        images.append(f"data:{mime};base64,{b64}")
    body["images"] = images
    return ImageHttpRequest("openai", f"{base_url}/v1/images/edits", body, size)


def build_gemini_request(
    base_url: str,
    model: str,
    prompt: str,
    aspect_ratio: str,
    references: list[Path],
    tier: str = "2K",
) -> ImageHttpRequest:
    """Gemini：文生图与参考图编辑同一接口；参考图按顺序作为 inline_data part（纯 base64）。"""
    parts: list[dict[str, Any]] = [{"text": prompt}]
    for p in references:
        mime, b64 = encode_reference(p)
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
    body = {
        "async": True,
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"imageConfig": {"aspectRatio": aspect_ratio, "imageSize": tier.upper()}},
    }
    return ImageHttpRequest("gemini", f"{base_url}/v1beta/models/{model}:generateContent", body,
                            f"{aspect_ratio} · {tier.upper()}")


def extract_result_urls(body: dict[str, Any]) -> list[str] | None:
    """完成态返回图片 URL 列表；未完成返回 None。兼容 OpenAI(data) 与 Gemini(candidates) 两种形状。"""
    if isinstance(body.get("data"), list):
        out = []
        for item in body["data"]:
            if item.get("url"):
                out.append(item["url"])
            elif item.get("b64_json"):
                out.append("data:image/png;base64," + item["b64_json"])
        return out
    if isinstance(body.get("candidates"), list):
        out = []
        for cand in body["candidates"]:
            for part in (cand.get("content") or {}).get("parts") or []:
                text = (part.get("text") or "").strip()
                if text.startswith(("http://", "https://")):
                    out.append(text)
                inline = part.get("inline_data") or part.get("inlineData")
                if inline and inline.get("data"):
                    out.append(f"data:{inline.get('mime_type', 'image/png')};base64,{inline['data']}")
        return out
    return None


_HTML_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def non_json_message(resp: httpx.Response) -> str:
    """接口没有返回 JSON 时的说明。网关 / CDN 的 HTML 错误页只留标题，不把整页 HTML 写进日志。"""
    text = resp.text.strip()
    if text.startswith("<") or "html" in resp.headers.get("content-type", "").lower():
        m = _HTML_TITLE.search(text)
        title = " ".join(m.group(1).split())[:80] if m else ""
        return "网关返回了错误页" + (f"「{title}」" if title else "") + "，不是画图接口的应答"
    return text[:200]


def _error_text(body: dict[str, Any]) -> str:
    err = body.get("error")
    if isinstance(err, dict):
        err = err.get("message") or err.get("code")
    return str(err or body.get("message") or body.get("status") or "未知错误")


class ImageAPIError(RuntimeError):
    pass


class UpstreamBusyError(ImageAPIError):
    """上游算力暂时不足：任务没有生成出图，过一会儿重新提交通常就能成功。"""


# 上游繁忙的特征文本（小写匹配）。只认明确的「繁忙 / 限流」，其他失败原样报错
BUSY_MARKERS = ("temporarily busy", "try again later", "overloaded", "rate limit", "too many requests",
                "繁忙", "稍后重试", "稍后再试")
# 上游繁忙时自动重新提交的次数与基础等待秒数（第 n 次等待约 n × 基础秒数，带随机抖动错开）
BUSY_RETRIES = 3
BUSY_BACKOFF = 15.0


def is_busy_error(text: str) -> bool:
    t = text.lower()
    return any(m in t for m in BUSY_MARKERS)


# 只重试「请求还没发出去」的连接错误：服务端没收到，重试不会重复提交任务、不会多扣费
CONNECT_RETRIES = 3
CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)


async def with_connect_retry(fn, retries: int = CONNECT_RETRIES, backoff: float = 1.0):
    """本机代理偶发断连时自动重试，其他错误原样抛出。"""
    for attempt in range(retries + 1):
        try:
            return await fn()
        except CONNECT_ERRORS:
            if attempt == retries:
                raise
            await asyncio.sleep(backoff * (attempt + 1))


class RightAPIImageClient:
    """提交异步画图任务 → 轮询 /v1/tasks/{task_id} → 下载图片 URL。"""

    def __init__(
        self,
        base_url: str = "https://www.rightapi.ai",
        timeout: float = 600,
        poll_interval: float = 4.0,
        transport: httpx.AsyncBaseTransport | None = None,
        busy_retries: int = BUSY_RETRIES,
        busy_backoff: float = BUSY_BACKOFF,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._transport = transport
        self.busy_retries = max(0, busy_retries)
        self.busy_backoff = busy_backoff

    async def send(self, req: ImageHttpRequest, api_key: str, on_event: ProgressCallback | None = None) -> list[bytes]:
        """提交并等待出图。上游繁忙导致的失败会等一会儿自动重新提交；其他错误（含超时）不重试。"""
        if not api_key:
            raise ImageAPIError("未配置 RIGHTAPI_IMAGE_KEY")

        async def emit(msg: str) -> None:
            if on_event is not None:
                await on_event(msg)

        for attempt in range(self.busy_retries + 1):
            try:
                return await self._send_once(req, api_key, emit)
            except UpstreamBusyError as e:
                if attempt == self.busy_retries:
                    if self.busy_retries:
                        raise UpstreamBusyError(f"{e}（已自动重试 {self.busy_retries} 次仍繁忙，"
                                                "请稍后再试或换一个画图模型）") from e
                    raise
                # 随机抖动：避免同一批失败的任务在同一秒一起重新提交
                wait = self.busy_backoff * (attempt + 1) * random.uniform(0.8, 1.2)
                await emit(f"⏳ 上游繁忙，{wait:.0f} 秒后自动重新提交（第 {attempt + 1}/{self.busy_retries} 次重试）")
                await asyncio.sleep(wait)
        raise AssertionError("unreachable")

    async def _send_once(self, req: ImageHttpRequest, api_key: str, emit) -> list[bytes]:
        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=120), transport=self._transport) as client:
            try:
                resp = await with_connect_retry(lambda: client.post(req.url, json=req.json, headers=headers))
            except httpx.HTTPError as e:
                raise ImageAPIError(f"提交画图任务失败：{type(e).__name__}") from e
            body = self._json(resp)
            if resp.status_code >= 400:
                msg = f"提交画图任务失败：HTTP {resp.status_code} {_error_text(body)}"
                # 429 / 503 与明确的「繁忙」提示：服务端没有创建任务，重新提交安全
                if resp.status_code in (429, 503) or is_busy_error(_error_text(body)):
                    raise UpstreamBusyError(msg)
                # 500 / 502 / 504：说不清任务是否已经创建，自动重新提交可能重复扣费，所以只提示、不重试
                if resp.status_code in (500, 502, 504):
                    msg += "。RightAPI 服务端临时故障，请稍后再试；连续失败可换一个画图模型"
                raise ImageAPIError(msg)
            if str(body.get("status", "")).lower() in TERMINAL_FAILED:
                err = _error_text(body)
                raise (UpstreamBusyError if is_busy_error(err) else ImageAPIError)(f"画图任务提交即失败：{err}")

            urls = extract_result_urls(body)
            if urls is None:
                task_id = body.get("task_id")
                if not task_id:
                    raise ImageAPIError("接口既没有返回图片也没有返回 task_id")
                await emit(f"已提交异步任务 {task_id}，开始轮询…")
                urls = await self._poll(client, str(task_id), headers, emit)
            if not urls:
                raise ImageAPIError("任务完成但未返回图片地址")
            # 下载结果图：CDN 是第三方地址，不携带 API Key
            out = []
            for url in urls:
                out.append(await self._download(client, url))
        return out

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            return {"message": non_json_message(resp)}
        return data if isinstance(data, dict) else {"message": str(data)[:200]}

    async def _poll(self, client: httpx.AsyncClient, task_id: str, headers: dict, emit) -> list[str]:
        url = f"{self.base_url}/v1/tasks/{task_id}"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        last_progress = None
        while loop.time() < deadline:
            await asyncio.sleep(self.poll_interval)
            try:
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError:
                continue  # 网络抖动：继续轮询
            body = self._json(resp)
            if resp.status_code in (401, 403, 404):
                raise ImageAPIError(f"查询任务失败：HTTP {resp.status_code} {_error_text(body)}")
            if resp.status_code >= 400:
                continue
            urls = extract_result_urls(body)
            if urls is not None:
                return urls
            status = str(body.get("status", "")).lower()
            if status in TERMINAL_FAILED:
                err = _error_text(body)
                raise (UpstreamBusyError if is_busy_error(err) else ImageAPIError)(f"画图任务{status}：{err}")
            progress = body.get("progress")
            if progress is not None and progress != last_progress:
                last_progress = progress
                await emit(f"任务 {task_id[-8:]} 进度 {progress}%")
        raise ImageAPIError(f"画图任务 {task_id} 超过 {self.timeout:.0f} 秒未完成")

    @staticmethod
    async def _download(client: httpx.AsyncClient, url: str) -> bytes:
        if url.startswith("data:"):
            return base64.b64decode(url.split(",", 1)[1])
        try:
            resp = await with_connect_retry(lambda: client.get(url, follow_redirects=True))
        except httpx.HTTPError as e:
            raise ImageAPIError(f"下载生成图片失败：{type(e).__name__}") from e
        if resp.status_code >= 400:
            raise ImageAPIError(f"下载生成图片失败：HTTP {resp.status_code}")
        return resp.content
