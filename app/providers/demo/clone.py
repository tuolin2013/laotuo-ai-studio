"""零 Key 演示模式：参考爆款视频克隆。

基于真实抽取的关键帧（时间戳、亮度、相邻帧画面变化）生成「原视频拆解报告 + 替换本品后的分镜剧本」，
JSON 结构与 hypit_viral_clone 卡带的真实 LLM 输出完全一致。
"""

from __future__ import annotations

from typing import Any

from app.engine.video_cloner import format_timestamp, segment_bounds
from app.providers.demo.content import _TEA, _clean, _info_line, _pick, _ref, _shot_bank, short
from app.schemas.config import CategoryConfig, PlatformConfig
from app.schemas.video import ReferenceVideoSource

CUT_THRESHOLD = 0.18  # 相邻关键帧画面差异超过该值视为一次切镜


def _rolls(source: ReferenceVideoSource) -> list[str]:
    """A-Roll / B-Roll 判定：开头钩子与结尾号召为人物出镜，画面突变后的镜头视为产品 B-Roll 插入。"""
    n = len(source.frames)
    rolls = []
    for i, f in enumerate(source.frames):
        if i == 0 or i == n - 1:
            rolls.append("A-Roll")
        elif f.change_score >= CUT_THRESHOLD or i % 2 == 1:
            rolls.append("B-Roll")
        else:
            rolls.append("A-Roll")
    return rolls


def _hook_style(source: ReferenceVideoSource) -> tuple[str, str]:
    first = source.frames[0]
    second = source.frames[1] if len(source.frames) > 1 else first
    if second.change_score >= CUT_THRESHOLD * 1.5:
        return "结果先行 + 快切", "开场 1 秒内直接给出最抓眼的画面，随后硬切进入讲解，制造“先看结果再听原因”的好奇"
    if first.brightness < 90:
        return "暗场悬念", "开场画面偏暗、信息量少，用一句反常识台词制造悬念，引导观众等待揭晓"
    return "人物反差台词", "人物正对镜头直接抛出痛点 / 反常识台词，用表情和语气在 3 秒内建立代入感"


def demo_clone_script(
    source: ReferenceVideoSource,
    cat: CategoryConfig,
    plat: PlatformConfig,
    strategy: str,
    aspect_ratio: str,
    idea: str | None,
    product_info: dict[str, str],
) -> dict[str, Any]:
    frames = source.frames
    bounds = segment_bounds(source)
    rolls = _rolls(source)
    cuts = [f for f in frames[1:] if f.change_score >= CUT_THRESHOLD]
    a_count = rolls.count("A-Roll")
    hook_name, hook_desc = _hook_style(source)
    avg = source.duration_seconds / max(len(frames), 1)

    bank = _shot_bank(cat, "immersive_demo")
    hook_t, cta_t, body = bank[0], bank[-1], bank[1:-1]
    talk_body = [t for t in body if t["dialogue"]] or body
    sfx_body = [t for t in body if not t["dialogue"]] or body
    person = "年轻女生" if cat.category_id == _TEA else "年轻铲屎官"
    pain = short(_pick(cat.pain_points, 0, cat.name), 12)
    info_line = _info_line(product_info)
    adapt = strategy == "adapt"

    breakdown, shots = [], []
    for i, (f, roll, (start, end)) in enumerate(zip(frames, rolls, bounds, strict=True)):
        seg = end - start
        changed = f.change_score >= CUT_THRESHOLD
        is_last = i == len(frames) - 1
        if i == 0:
            t = dict(hook_t)
            if hook_name == "人物反差台词":
                t.update(audio="dialogue_and_ambient", size="近景", cam="固定机位",
                         dialogue=f"{person}对着镜头，带点不服气地说：“{pain}？我不信！”",
                         ambient=t["ambient"] or "室内轻微环境底噪")
        elif is_last:
            t = cta_t
        elif roll == "A-Roll":
            t = talk_body[i % len(talk_body)]
        else:
            t = sfx_body[i % len(sfx_body)]
        stamp = f.timestamp
        content = (f"{'人物出镜' if roll == 'A-Roll' else '产品画面'}，"
                   f"{'画面相对上一帧明显变化（切镜）' if changed else '延续上一画面'}，"
                   f"{'偏暗' if f.brightness < 90 else '明亮' if f.brightness > 170 else '中等亮度'}")
        anchor = ("开场钩子台词 / 强声音" if i == 0 else "行动号召台词" if is_last
                  else "人物台词推进卖点" if roll == "A-Roll" else "物理动作音效承接上一句台词")
        breakdown.append({
            "timestamp": f"{format_timestamp(start)}–{format_timestamp(end)}（关键帧 {stamp}）",
            "roll": roll, "shot_size": t["size"], "camera_movement": t["cam"],
            "content": content, "dialogue_anchor": anchor,
        })
        if adapt:
            prefix = f"借鉴原片 {stamp} 的{'人物表达' if roll == 'A-Roll' else '产品特写'}思路改编："
        else:
            prefix = f"1:1 复刻原片 {stamp} 的景别与运镜，原片商品替换为本品："
        scene = prefix + t["scene"]
        if i == 0 and idea:
            scene += f"；整体贯彻用户想法「{idea}」"
        if info_line and t["purpose"] == "proof":
            scene += f"（{info_line}）"
        duration = round(max(1.5, min(seg, 3.0)), 1) if i == 0 else round(max(2.0, min(seg, 10.0)), 1)
        shots.append({
            "source_timestamp": stamp,
            "duration_seconds": duration,
            "shot_purpose": "hook" if i == 0 else "call_to_action" if is_last else t["purpose"],
            "shot_size": t["size"],
            "camera_movement": t["cam"],
            "scene_description": scene,
            "subject_action": t["action"],
            "first_frame_description": t["first"],
            "audio_mode": t["audio"],
            "character_dialogue": t["dialogue"],
            "ambient_sound_description": t["ambient"],
            "first_frame_generation_mode": t["mode"],
            "required_references": [_ref(cat, role) for role in t["refs"]],
        })

    pct = round(a_count / len(frames) * 100)
    analysis = {
        "hook_analysis": f"【{hook_name}】{hook_desc}。复刻时第 1 镜保留该手法，"
                         f"并让{cat.name}在 3 秒内出现在画面中。",
        "rhythm_analysis": (f"原片 {source.duration_seconds:g} 秒，抽取 {len(frames)} 张关键帧，平均每段约 {avg:.1f} 秒；"
                            f"检测到 {len(cuts)} 次明显画面切换"
                            + (f"（{'、'.join(c.timestamp for c in cuts)}）" if cuts else "")
                            + "。切镜点基本落在台词转折与产品揭示处。"),
        "a_b_roll_ratio": (f"A-Roll（人物出镜说话）约 {pct}% / B-Roll（产品特写、使用过程）约 {100 - pct}%，"
                           "以“人物说一句 → 产品画面印证一句”的方式交替。"),
        "dialogue_anchoring": ("A-Roll 段落由人物抛出卖点或疑问，紧接着的 B-Roll 用对应的产品画面和物理音效作答；"
                               "克隆后台词全部改为镜头内人物同期声，原片若有画外音一律取消。"),
        "shot_breakdown": breakdown,
        "source_note": (f"演示模式：基于真实抽取的 {len(frames)} 张关键帧的时间戳与像素变化生成；"
                        "填写 LLM Key 后由 Claude 识图给出逐帧拆解。"),
    }
    label = "借鉴钩子与结构改编" if adapt else "1:1 复刻分镜节奏仅换品"
    summary = (f"克隆「{source.file_name}」（{label}）：{hook_name}开场，{len(shots)} 镜，"
               f"A/B-Roll 交替，原片商品替换为{cat.name}，全片无画外音")
    if idea:
        summary += f"，按用户想法「{idea}」调整"
    return _clean({"clone_analysis": analysis, "script_summary": summary, "shots": shots}, cat, plat)
