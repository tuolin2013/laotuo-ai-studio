"""把类目 / 平台 / 用户想法 / 槽位等上下文渲染进 Skill 卡带，生成发给 LLM 的指令。"""

from __future__ import annotations

import json
from typing import Any

from app.providers.demo.content import camera_tags
from app.schemas.config import CategoryConfig, PlatformConfig
from app.schemas.skill import SkillCard

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
