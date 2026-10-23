"""演示模式的配音文案：输出与 voice_writer Skill 完全一致的 JSON 结构，后续分段、试听、生成、合成流程无差别。"""

from __future__ import annotations

import re

from app.engine.compliance import sanitize
from app.schemas.config import CategoryConfig, PlatformConfig
from app.schemas.voice import CJK_LANGUAGES, VOICE_PRESETS

PURPOSE_PRESET = {
    "live_selling": "热情女主播",
    "seeding": "温柔种草",
    "explainer": "专业讲解",
    "brand_story": "品牌旁白",
    "promo": "促销播报",
}
PLATFORM_CTA = {
    "taobao": "喜欢的话点下方链接，先加购收藏，活动期间下单更划算。",
    "pinduoduo": "拼单更划算，赶紧点下方链接，库存不多啦！",
    "wechat_shop": "点下方小黄车，带一份回家试试吧。",
}
_BRACKETS = re.compile(r"[（(][^()（）]*[)）]")
_FIRST_CLAUSE = re.compile(r"^[^，,；;。！!？?]+")


def _clause(text: str, limit: int = 22) -> str:
    """长卖点只取第一个小句，去掉括号里的补充说明。"""
    text = _BRACKETS.sub("", text or "").strip()
    m = _FIRST_CLAUSE.match(text)
    return (m.group(0) if m else text)[:limit].strip()


def demo_voice_script(category: CategoryConfig, platform: PlatformConfig, language: str, purpose: str,
                      duration: int, idea: str | None, product_info: dict[str, str] | None) -> dict:
    info = product_info or {}
    name = info.get("product_line") or category.name
    preset = VOICE_PRESETS[PURPOSE_PRESET.get(purpose, "热情女主播")]
    if language not in CJK_LANGUAGES:
        segments = [
            {"text": "Still looking for something that actually works for you?", "control": "", "pause_after": 0.6},
            {"text": "Meet our best seller, made with carefully selected ingredients.", "control": "",
             "pause_after": 0.3},
            {"text": "Thousands of customers come back for it every month.", "control": "", "pause_after": 0.3},
            {"text": "Tap the link below and try it today.", "control": "slightly faster, cheerful", "pause_after": 0},
        ]
        return {"title": "Demo voiceover", "voice_description": preset, "segments": segments,
                "notes": "演示模式：外语只给出固定的英文示例，真实 LLM 会按商品信息用目标语言撰写"}

    pain = _clause(category.pain_points[0]) if category.pain_points else "每天都在找好用的东西"
    segments = [
        {"text": f"{pain}？", "control": "", "pause_after": 0.6},
        {"text": f"你一定要试试这款{name}。", "control": "", "pause_after": 0.3},
    ]
    # 卖点段数按目标时长增减：中速口播约 4.5 字 / 秒，每段约 15 字
    room = max(1, min(len(category.core_selling_points), round(duration * 4.5 / 15) - 3))
    for point in category.core_selling_points[:room]:
        segments.append({"text": f"{_clause(point)}。", "control": "", "pause_after": 0.3})
    if offer := info.get("price_offer"):
        segments.append({"text": f"现在下单，{_clause(offer, 28)}。", "control": "语速稍快，热情", "pause_after": 0.6})
    cta = PLATFORM_CTA.get(platform.platform_id, "点击下方链接，马上下单！")
    segments.append({"text": cta, "control": "语速稍快，热情", "pause_after": 0})
    for s in segments:
        s["text"] = sanitize(s["text"], category, platform)
    note = "演示模式：按类目痛点、卖点与平台调性拼出的示例文案"
    if idea and idea.strip():
        note += f"；你的想法「{idea.strip()[:30]}」会在接入真实 LLM 后优先落实"
    if language != "mandarin":
        note += "；方言改写需要真实 LLM，演示文案仍是普通话"
    return {"title": f"{category.name}口播", "voice_description": preset, "segments": segments, "notes": note}
