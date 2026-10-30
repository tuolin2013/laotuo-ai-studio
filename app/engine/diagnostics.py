"""轻量连通性诊断：验证 RightAPI 的 LLM Key、两个画图渠道的 Key 与 Modal 服务是否可用，不生成图片、不消耗画图额度。

- LLM：发送一条极短消息，确认 Key 有效并探测可用的接口协议；失败时列出这把 Key 能调用的模型（列表接口不计费），
  提示所选模型是否不在 Key 绑定的渠道里。
- 画图：查询一个不存在的任务 GET /v1/tasks/{随机 id}。返回 401/403 说明 Key 无效；
  返回 404 / 任务不存在说明 Key 有效且任务查询接口可达。完全不提交画图任务。
- Modal H3：如配置了预热地址则发一次轻量 GET。
"""

from __future__ import annotations

import uuid

import httpx

from app.providers.image import with_connect_retry
from app.providers.image_models import get_model
from app.providers.llm import PROTOCOL_LABELS, LLMError, RightAPILLM, list_models
from app.providers.llm_providers import get_provider
from app.settings import Settings


def _mask(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if len(key) > 12 else "已填写"


async def check_llm(s: Settings, transport: httpx.AsyncBaseTransport | None = None) -> str:
    p = get_provider(s.llm_provider)
    if not s.llm_key:
        return f"🟡 LLM · {p.name}：未填写 API Key（{p.key_env}），将使用演示模式"
    if not s.llm_api_base:
        return f"🟡 LLM · {p.name}：未填写 Base URL，将使用演示模式"
    if not s.llm_model:
        return f"🟡 LLM · {p.name}：还没有选模型（点「🔄 获取可用模型」），将使用演示模式"
    name = f"LLM · {p.name}（{_mask(s.llm_key)} · {s.llm_api_base}）"
    llm = RightAPILLM(s.llm_api_base, s.llm_key, s.llm_model, s.llm_api_format, timeout=60, transport=transport)
    try:
        # 推理模型会先思考再回答，上限给得太小可能只剩思考、没有回答
        reply = await llm.complete("你是连通性测试助手。", "请只回复 OK", max_tokens=256)
    except LLMError as e:
        # 失败时附上这把 Key 实际能用的模型（列表不计费；有的中转不开放列表，就只报原错误）
        try:
            models = await list_models(s.llm_api_base, s.llm_key, s.llm_api_format, transport=transport)
        except LLMError:
            models = None
        if models and s.llm_model not in models:
            shown = "、".join(models[:12]) + ("…" if len(models) > 12 else "")
            return (f"❌ {name}：模型 {s.llm_model} 不在这把 Key 绑定的渠道里，可用模型：{shown}。"
                    "请点「🔄 获取可用模型」重新选择后保存")
        return f"❌ {name}：{e}"
    fmt = PROTOCOL_LABELS.get(llm.resolved_format or "", llm.resolved_format)
    return f"🟢 {name}：连通正常，模型 {s.llm_model}，接口 {fmt}，回复「{reply.strip()[:20]}」"


async def check_image(s: Settings, transport: httpx.AsyncBaseTransport | None = None,
                      channel: str = "hq") -> str:
    """channel：hq =「image 高质量」渠道（RIGHTAPI_IMAGE_KEY），std =「image 普通」渠道（RIGHTAPI_IMAGE_STD_KEY）。"""
    if channel == "std":
        name, key, env = "画图 · image 普通", s.image_std_key, "RIGHTAPI_IMAGE_STD_KEY"
        base = s.image_std_base_url or s.rightapi_base_url
    else:
        name, key, env = "画图 · image 高质量", s.image_key, "RIGHTAPI_IMAGE_KEY"
        base = s.rightapi_base_url
    if not key:
        return f"🟡 {name}：未填写 {env}（该渠道的模型将使用演示模式）"
    probe = f"task_{uuid.uuid4().hex}"
    try:
        async with httpx.AsyncClient(timeout=30, transport=transport) as client:
            resp = await with_connect_retry(lambda: client.get(
                f"{base}/v1/tasks/{probe}", headers={"Authorization": f"Bearer {key}"}))
    except httpx.HTTPError as e:
        return f"❌ {name}（{_mask(key)}）：无法连接 {base}（{type(e).__name__}）"
    if resp.status_code in (401, 403):
        return f"❌ {name}（{_mask(key)}）：Key 无效、已禁用或未绑定该渠道（HTTP {resp.status_code}）"
    if resp.status_code >= 500:
        return f"⚠️ {name}（{_mask(key)}）：服务端暂不可用（HTTP {resp.status_code}），稍后重试"
    models = "、".join(f"{get_model(m).request_model}({get_model(m).protocol})" for m in s.image_models
                      if get_model(m).channel == channel)
    return (f"🟢 {name}（{_mask(key)}）：Key 有效，任务查询接口可达（探测任务返回 HTTP {resp.status_code}）。"
            f"可选模型：{models or '无'}")


async def check_modal(s: Settings, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """只查找已部署的类，不启动 GPU、不产生费用。"""
    from app.providers.video.modal_minimax_h3 import ModalMiniMaxH3Provider, modal_logged_in

    if not modal_logged_in():
        return "🟡 Modal H3：本机未登录 Modal（终端运行 modal token new），将使用本地演示视频"
    res = await ModalMiniMaxH3Provider(s.modal_h3_app, s.modal_h3_class).warmup()
    return f"{'🟢' if res.ready else '❌'} Modal H3：{res.message}"


async def diagnose(s: Settings, transport: httpx.AsyncBaseTransport | None = None) -> str:
    lines = [
        f"**连通性诊断**（Base URL：`{s.rightapi_base_url}`）",
        "- " + await check_llm(s, transport),
        "- " + await check_image(s, transport, "hq"),
        "- " + await check_image(s, transport, "std"),
        "- " + await check_modal(s, transport),
    ]
    return "\n".join(lines)
