"""把类目 / 平台 / 用户想法 / 槽位等上下文渲染进 Skill 卡带，生成发给 LLM 的指令。"""

from __future__ import annotations

import json
from typing import Any

from app.providers.demo.content import camera_tags
from app.schemas.config import CategoryConfig, PlatformConfig
from app.schemas.skill import SkillCard
from app.schemas.video import ReferenceVideoSource

IDEA_PLACEHOLDER = "（未填写）"
SYSTEM_PROMPT = "你是电商 AIGC 内容工厂中的专业 Skill 执行器。严格按照用户消息中的 Skill 指令与输出格式工作。"


def _dump(model) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _idea(text: str | None) -> str:
    return text.strip() if text and text.strip() else IDEA_PLACEHOLDER


def _render(skill: SkillCard, **values: Any) -> str:
    """只传入卡带实际用到的变量，兼容用户自定义的精简卡带。"""
    used = skill.placeholders
    return skill.render(**{k: v for k, v in values.items() if k in used})


def compile_planner(
    skill: SkillCard,
    deliverable_type: str,
    category: CategoryConfig,
    platform: PlatformConfig,
    aspect_ratio: str,
    requested_count: int | None,
    user_creative_idea: str | None,
    product_info: dict[str, str] | None = None,
) -> str:
    return _render(
        skill,
        deliverable_type=deliverable_type,
        category_json=_dump(category),
        platform_json=_dump(platform),
        product_info_json=_json(product_info or {}),
        requested_count=requested_count if requested_count else "auto",
        aspect_ratio=aspect_ratio,
        user_creative_idea=_idea(user_creative_idea),
    )


def compile_scriptwriter(
    skill: SkillCard,
    video_type: str,
    aspect_ratio: str,
    target_duration_seconds: int,
    category: CategoryConfig,
    platform: PlatformConfig,
    user_creative_idea: str | None,
    shot_count: int = 5,
    product_info: dict[str, str] | None = None,
) -> str:
    return _render(
        skill,
        video_type=video_type,
        aspect_ratio=aspect_ratio,
        shot_count=shot_count,
        target_duration_seconds=target_duration_seconds,
        category_json=_dump(category),
        platform_json=_dump(platform),
        product_info_json=_json(product_info or {}),
        user_creative_idea=_idea(user_creative_idea),
    )


def compile_clone_scriptwriter(
    skill: SkillCard,
    source: ReferenceVideoSource,
    clone_strategy: str,
    aspect_ratio: str,
    target_duration_seconds: int,
    category: CategoryConfig,
    platform: PlatformConfig,
    user_creative_idea: str | None,
    shot_count: int,
    product_info: dict[str, str] | None = None,
) -> str:
    """克隆编剧：参考视频元信息 + 关键帧时间轴（图片本身作为多模态输入另行附带）。"""
    from app.engine.video_cloner import format_timestamp, segment_bounds

    meta = {"file_name": source.file_name, "duration_seconds": source.duration_seconds,
            "width": source.width, "height": source.height, "aspect_ratio": source.aspect_ratio,
            "keyframe_count": len(source.frames)}
    timeline = [
        {"image": f"第 {f.index} 张图", "timestamp": f.timestamp,
         "segment": f"{format_timestamp(a)}–{format_timestamp(b)}", "change_from_previous": f.change_score}
        for f, (a, b) in zip(source.frames, segment_bounds(source), strict=True)
    ]
    return _render(
        skill,
        clone_strategy=clone_strategy,
        shot_count=shot_count,
        aspect_ratio=aspect_ratio,
        target_duration_seconds=target_duration_seconds,
        video_meta_json=_json(meta),
        frame_timeline_json=_json(timeline),
        category_json=_dump(category),
        platform_json=_dump(platform),
        product_info_json=_json(product_info or {}),
        user_creative_idea=_idea(user_creative_idea),
    )


def compile_image_prompt(
    skill: SkillCard,
    slot_data: dict[str, Any],
    category: CategoryConfig,
    platform: PlatformConfig,
    aspect_ratio: str,
    reference_roles: list[str],
) -> str:
    """slot_data 为槽位（或分镜首帧）的画面信息字典。"""
    roles = "、".join(f"图{i + 1}={r}" for i, r in enumerate(reference_roles)) or "无（纯文生图）"
    return _render(
        skill,
        slot_json=_json(slot_data),
        category_json=_dump(category),
        platform_json=_dump(platform),
        aspect_ratio=aspect_ratio,
        reference_roles=roles,
    )


H3_SHOT_FIELDS = {
    "duration_seconds", "shot_purpose", "shot_size", "camera_movement", "scene_description",
    "subject_action", "first_frame_description", "audio_mode", "character_dialogue",
    "ambient_sound_description",
}


def compile_h3_prompt(skill: SkillCard, shot, aspect_ratio: str, category: CategoryConfig) -> str:
    shot_data = shot.model_dump(mode="json", include=H3_SHOT_FIELDS)
    return _render(
        skill,
        shot_json=_json(shot_data),
        aspect_ratio=aspect_ratio,
        duration_seconds=f"{shot.duration_seconds:g}",
        category_json=_dump(category),
        camera_tag_hint=" ".join(camera_tags(shot.camera_movement, shot.shot_size)),
    )


# ---------------------------------------------------------------- 官方 h3-prompt-writing（I2VA / Ref2VA）

H3_MIN_SECONDS, H3_MAX_SECONDS = 4.0, 15.0
H3_GUIDE_FOR_MODE = {"i2va": "base-en.txt", "ref2va": "ref-en.txt"}
I2VA_INSTRUCTION = ("For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) "
                    "is fully referenced.")
I2VA_FIELDS = ("integrated_multimodal_description", "overall_soundscape", "non_diegetic_music")
REF2VA_SECTIONS = ("subject_definitions", "summary", "retention_analysis", "detailed_description",
                   "overall_soundscape", "non_diegetic_music")
MAX_PICTURES = 9  # 与 Modal H3 ref2va 后端一致：首帧 + 最多 8 张商品参考


def h3_target_seconds(duration_seconds: float) -> float:
    """官方要求描述时长落在 4–15 秒：分镜时长超出范围时就近取值。"""
    return round(min(max(float(duration_seconds), H3_MIN_SECONDS), H3_MAX_SECONDS), 2)


def picture_roles(reference_roles: list[str]) -> list[str]:
    """<Picture 1> 固定为锁定首帧，其后依次为该分镜绑定的商品参考图（与视频后端传图顺序一致）。"""
    return ["locked_first_frame", *reference_roles][:MAX_PICTURES]


def picture_label_lines(mode: str, reference_roles: list[str]) -> str:
    """i2va 只传锁定首帧；ref2va 依次列出首帧与商品参考图（与视频后端传图顺序一致）。"""
    first = "- <Picture 1>: the locked first frame of [Shot 1] at 0.00 seconds (packaging verified undistorted)."
    if mode == "i2va":
        return first
    roles = picture_roles(reference_roles)
    lines = [first, *(f"- <Picture {i}>: product reference photo, role `{r}`." for i, r in
                      enumerate(roles[1:], start=2))]
    return "\n".join(lines)


def supports_h3_modes(skill: SkillCard) -> bool:
    """卡带正文使用了 {{ h3_mode }} 即视为支持官方双模式。"""
    return "h3_mode" in skill.placeholders


def compile_official_h3_prompt(
    skill: SkillCard, shot, aspect_ratio: str, category: CategoryConfig, mode: str, reference_roles: list[str]
) -> str:
    """渲染官方卡带，并把所选模式对应的 references 指南全文附在指令之后。"""
    shot_data = shot.model_dump(mode="json", include=H3_SHOT_FIELDS)
    text = _render(
        skill,
        h3_mode=mode,
        shot_json=_json(shot_data),
        aspect_ratio=aspect_ratio,
        duration_seconds=f"{h3_target_seconds(shot.duration_seconds):.2f}",
        category_json=_dump(category),
        picture_labels=picture_label_lines(mode, reference_roles),
        camera_tag_hint=" ".join(camera_tags(shot.camera_movement, shot.shot_size)),
    )
    guide = skill.reference_context([H3_GUIDE_FOR_MODE[mode]])
    return f"{text}\n\n# Attached Reference Guide\n\n{guide}" if guide else text


def h3_structure_issues(prompt: str, mode: str) -> list[str]:
    """检查提示词是否符合官方结构：字段齐全且顺序正确。返回问题列表（空表示合格）。"""
    names = I2VA_FIELDS if mode == "i2va" else REF2VA_SECTIONS
    issues: list[str] = []
    if mode == "i2va" and not prompt.lstrip().startswith(I2VA_INSTRUCTION):
        issues.append("第一行不是官方 I2VA 首帧指令")
    positions = []
    for name in names:
        pos = prompt.find(f"{name}:")
        if pos == -1:
            issues.append(f"缺少字段 {name}")
        positions.append(pos)
    found = [p for p in positions if p != -1]
    if found != sorted(found):
        issues.append("字段顺序与官方规范不一致")
    if mode == "ref2va" and "<Picture 1>" not in prompt:
        issues.append("缺少 <Picture 1> 标签")
    return issues
