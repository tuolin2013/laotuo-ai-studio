"""RightAPI 画图模型目录：每个模型显式登记协议，绝不按名字猜测。

依据 https://docs.rightapi.ai/docs/rc_draw/ 与后台「模型列表」：
- openai 协议：0 张参考图 POST /v1/images/generations；≥1 张 POST /v1/images/edits
- gemini 协议：POST /v1beta/models/{model}:generateContent（文生图与参考图编辑同一接口）
用错协议会直接 404 / 400，所以不在目录里的模型一律拒绝调用。

两个渠道各用各的 Key（RightAPI 的 Key 绑定渠道，只能调用该渠道下的模型）：
- hq：「image 高质量」渠道，RIGHTAPI_IMAGE_KEY
- std：「image 普通」渠道，RIGHTAPI_IMAGE_STD_KEY
两个渠道同名的模型（gpt-image-2），普通渠道一侧的目录 ID 加 `@image` 后缀区分，请求时仍发送原模型名。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Protocol = Literal["openai", "gemini"]
Channel = Literal["hq", "std"]
CHANNEL_LABELS: dict[str, str] = {"hq": "高质量", "std": "普通"}
# 模型渠道 → 运行时通道（EngineRuntime.is_live / Settings.channel_live 使用）
LIVE_CHANNELS: dict[str, str] = {"hq": "image", "std": "image_std"}


@dataclass(frozen=True)
class ImageModel:
    # 目录 ID：界面下拉框的值，全局唯一
    model_id: str
    protocol: Protocol
    channel: Channel
    label: str
    # GPT Image 是否支持 xhigh / max 质量档（文档：仅 gpt-image-2.5-flare / sunburst）
    supports_xhigh: bool = False
    # 请求体 / 路径里实际发送的模型名；为空时与 model_id 相同
    api_model: str = ""

    @property
    def request_model(self) -> str:
        return self.api_model or self.model_id


CATALOG: dict[str, ImageModel] = {
    m.model_id: m
    for m in [
        # ---------- image 高质量渠道（生产推荐：全模型 4K、异步、不限并发）----------
        ImageModel("gpt-image-2.5-flare", "openai", "hq", "GPT Image 2.5 Flare · 速度优先", True),
        ImageModel("gpt-image-2.5-sunburst", "openai", "hq", "GPT Image 2.5 Sunburst · 编辑精度优先", True),
        ImageModel("gpt-image-2", "openai", "hq", "GPT Image 2 · 基础"),
        ImageModel("gemini-3-pro-image-preview", "gemini", "hq", "Nano Banana Pro · 画质与一致性优先"),
        ImageModel("gemini-nano-banana-2.1", "gemini", "hq", "Nano Banana 2.1 · 速度优先"),
        # ---------- image 普通渠道 ----------
        ImageModel("gpt-image-2@image", "openai", "std", "GPT Image 2 · 基础", api_model="gpt-image-2"),
        ImageModel("gpt-image-2-vip", "openai", "std", "GPT Image 2 VIP"),
        ImageModel("gpt-image-2.5", "openai", "std", "GPT Image 2.5"),
        ImageModel("nano-banana-pro", "gemini", "std", "Nano Banana Pro"),
        ImageModel("nano-banana-2", "gemini", "std", "Nano Banana 2"),
        ImageModel("nano-banana-2-lite", "gemini", "std", "Nano Banana 2 Lite"),
    ]
}

# 生产默认：image 高质量渠道的 5 个模型排在前面（下拉框默认选第一个）
HQ_MODELS: list[str] = [m.model_id for m in CATALOG.values() if m.channel == "hq"]
STANDARD_MODELS: list[str] = [m.model_id for m in CATALOG.values() if m.channel == "std"]
ALL_MODELS: list[str] = [*HQ_MODELS, *STANDARD_MODELS]


class UnknownImageModel(ValueError):
    pass


def get_model(model_id: str) -> ImageModel:
    try:
        return CATALOG[model_id]
    except KeyError:
        raise UnknownImageModel(
            f"模型 {model_id!r} 不在 RightAPI 画图模型目录中，无法确定调用协议。"
            f"可用模型：{'、'.join(CATALOG)}"
        ) from None


def protocol_of(model_id: str) -> Protocol:
    return get_model(model_id).protocol


def live_channel_of(model_id: str) -> str:
    """模型对应的运行时通道：image（高质量）/ image_std（普通），用于判定真实 / 演示模式。"""
    return LIVE_CHANNELS[get_model(model_id).channel]


def choices(model_ids: list[str]) -> list[tuple[str, str]]:
    """界面下拉框：(显示名, 目录 ID)。显示名带渠道，便于区分两个渠道的同名模型。"""
    out = []
    for mid in model_ids:
        m = CATALOG.get(mid)
        if m is not None:
            proto = "OpenAI 生成" if m.protocol == "openai" else "Gemini 生成"
            out.append((f"【{CHANNEL_LABELS[m.channel]}】{m.request_model}｜{m.label}｜{proto}", m.model_id))
    return out
