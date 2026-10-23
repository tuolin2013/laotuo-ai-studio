"""演示模式剪辑大师：按 video_editor 卡带的剪辑规则在本地算出剪辑方案，输出与真实 Skill 完全一致的 JSON。"""

from __future__ import annotations

from typing import Any

from app.engine.compliance import sanitize
from app.providers.demo.content import short
from app.schemas.config import CategoryConfig, PlatformConfig
from app.schemas.edit import MAX_HEADLINE_CHARS

HOOK_MAX_SECONDS = 3.5
SILENT_SHOT_MAX_SECONDS = 4.0
CTA_HEADLINE = "点击下方链接 立即下单"


def _cut_points(clip: dict[str, Any], is_hook: bool) -> tuple[float, float]:
    """入点 = 第一个声音段落前 0.4 秒（切掉静止首帧与台词前的空等），出点 = 最后一个声音段落后 0.4 秒。"""
    dur = float(clip["actual_seconds"])
    spans = clip.get("sound_spans") or []
    src_in = 0.0 if is_hook else min(0.3, dur / 4)
    if clip.get("spoken_line") and spans:
        src_in = max(spans[0][0] - 0.4, 0.0)
        src_out = spans[-1][1] + 0.4
    elif clip.get("spoken_line"):
        src_out = src_in + float(clip.get("estimated_speech_seconds") or 2) + 0.6
    else:
        src_out = src_in + min(SILENT_SHOT_MAX_SECONDS, max(float(clip["planned_seconds"]), 2.5))
    if is_hook and not spans:
        src_out = min(src_out, HOOK_MAX_SECONDS)
    # 给转场留出至少 0.3 秒余量画面
    return round(src_in, 2), round(min(src_out, max(dur - 0.3, src_in + 0.5)), 2)


def _headlines(cat: CategoryConfig, product_info: dict[str, str], lines: str) -> list[str]:
    """候选花字：类目卖点 + 商品规格，去掉台词里已经说过的。"""
    pool = [short(p, MAX_HEADLINE_CHARS) for p in cat.core_selling_points]
    pool += [short(v, MAX_HEADLINE_CHARS) for k, v in product_info.items() if k in {"spec", "dosage_form", "origin"}]
    out = []
    for text in pool:
        if text and text not in lines and text not in out:
            out.append(text)
    return out


def demo_edit(clips: list[dict[str, Any]], cat: CategoryConfig, plat: PlatformConfig, target_duration: int,
              idea: str | None, product_info: dict[str, str]) -> dict[str, Any]:
    lines = "".join(c.get("spoken_line") or "" for c in clips)
    pool = _headlines(cat, product_info, lines)
    rows = []
    for k, clip in enumerate(clips):
        is_hook, is_last = k == 0, k == len(clips) - 1
        src_in, src_out = _cut_points(clip, is_hook)
        purpose = clip.get("shot_purpose")
        headline = ""
        if is_last and purpose == "call_to_action":
            headline = CTA_HEADLINE
        elif not is_hook and purpose in {"selling_point", "proof"} and pool:
            headline = pool.pop(0)
        # 进入行动号召之前用一次叠化，其余硬切保持节奏
        nxt = clips[k + 1] if not is_last else None
        transition = "fade" if nxt is not None and nxt.get("shot_purpose") == "call_to_action" else "cut"
        rows.append({
            "shot_index": clip["shot_index"], "enabled": True, "source_in": src_in, "source_out": src_out,
            "transition_out": transition, "transition_seconds": 0.4, "volume": 1.0,
            "headline": sanitize(headline, cat, plat),
        })
    total = sum(r["source_out"] - r["source_in"] for r in rows)
    fades = sum(1 for r in rows if r["transition_out"] != "cut")
    summary = (f"{len(rows)} 镜按剧本顺序剪辑，钩子开场即入正题，切掉各镜开头静止首帧与台词后的空镜，"
               f"成片约 {total:.0f} 秒（剧本目标 {target_duration} 秒），{fades} 处叠化、其余硬切，"
               f"{sum(1 for r in rows if r['headline'])} 个镜头配卖点花字")
    if idea:
        summary += f"；按剪辑想法「{idea}」调整"
    return {"title": sanitize(f"{cat.name}带货短视频", cat, plat), "edit_summary": summary, "clips": rows}
