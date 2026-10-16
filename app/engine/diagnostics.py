"""轻量连通性诊断：验证 2 把 RightAPI Key 与 Modal 地址是否可用，不生成图片、不消耗画图额度。

- LLM：发送一条极短消息（max_tokens 很小），确认 Key 有效并探测可用的接口格式。
- 画图：只请求 GET /v1/models 列表，确认 Key 能访问 Image 渠道并查看可用模型名。
- Modal H3：如配置了预热地址则发一次轻量 GET。
"""

from __future__ import annotations

import httpx

from app.providers.llm import LLMError, RightAPILLM
from app.settings import Settings


def _mask(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if len(key) > 12 else "已填写"


async def check_llm(s: Settings, transport: httpx.AsyncBaseTransport | None = None) -> str:
    if not s.llm_key:
        return "🟡 LLM：未填写 RIGHTAPI_LLM_KEY（将使用演示模式）"
    llm = RightAPILLM(s.rightapi_base_url, s.llm_key, s.llm_model, s.llm_api_format, timeout=60, transport=transport)
    try:
        reply = await llm.complete("你是连通性测试助手。", "请只回复 OK", max_tokens=16)
    except LLMError as e:
        return f"❌ LLM（{_mask(s.llm_key)}）：{e}"
    return f"🟢 LLM（{_mask(s.llm_key)}）：连通正常，接口格式 {llm.resolved_format}，模型回复「{reply.strip()[:20]}」"


async def check_image(s: Settings, transport: httpx.AsyncBaseTransport | None = None) -> str:
    if not s.image_key:
        return "🟡 画图：未填写 RIGHTAPI_IMAGE_KEY（将使用演示模式）"
    headers = {"Authorization": f"Bearer {s.image_key}", "x-goog-api-key": s.image_key}
    try:
        async with httpx.AsyncClient(timeout=30, transport=transport) as client:
            resp = await client.get(f"{s.rightapi_base_url}/v1/models", headers=headers)
    except httpx.HTTPError as e:
        return f"❌ 画图（{_mask(s.image_key)}）：网络错误 {type(e).__name__}"
    if resp.status_code in (401, 403):
        return f"❌ 画图（{_mask(s.image_key)}）：Key 无效或无权限（HTTP {resp.status_code}）"
    if resp.status_code >= 400:
        return f"⚠️ 画图（{_mask(s.image_key)}）：/v1/models 返回 HTTP {resp.status_code}，Key 可能仍可用于画图"
    try:
        ids = [m.get("id", "") for m in resp.json().get("data", [])]
    except ValueError:
        ids = []
    wanted = [*s.openai_image_models, *s.gemini_image_models]
    found = [m for m in wanted if m in ids]
    missing = [m for m in wanted if m not in ids]
    msg = f"🟢 画图（{_mask(s.image_key)}）：连通正常"
    if ids:
        msg += f"；渠道可用模型匹配 {len(found)}/{len(wanted)}"
        if missing:
            msg += f"（未在列表中：{'、'.join(missing)}）"
    return msg


async def check_modal(s: Settings, transport: httpx.AsyncBaseTransport | None = None) -> str:
    if not s.modal_h3_url:
        return "🟡 Modal H3：未填写 MODAL_H3_ENDPOINT_URL（将使用本地演示视频）"
    if not s.modal_h3_warmup_url:
        return "⚪ Modal H3：已填写地址；未配置 MODAL_H3_WARMUP_URL，跳过检测（可在视频页点「🔥 预热」）"
    try:
        async with httpx.AsyncClient(timeout=60, transport=transport) as client:
            resp = await client.get(s.modal_h3_warmup_url)
    except httpx.HTTPError as e:
        return f"❌ Modal H3：预热地址不可达 {type(e).__name__}"
    return f"{'🟢' if resp.status_code < 400 else '❌'} Modal H3：预热地址返回 HTTP {resp.status_code}"


async def diagnose(s: Settings, transport: httpx.AsyncBaseTransport | None = None) -> str:
    lines = [
        f"**连通性诊断**（Base URL：`{s.rightapi_base_url}`）",
        "- " + await check_llm(s, transport),
        "- " + await check_image(s, transport),
        "- " + await check_modal(s, transport),
    ]
    return "\n".join(lines)
