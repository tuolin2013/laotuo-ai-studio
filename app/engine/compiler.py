"""把类目 / 平台 / 用户想法 / 槽位等上下文渲染进 Skill 卡带，生成发给 LLM 的指令。"""

from __future__ import annotations

import json

from app.schemas.config import CategoryConfig, PlatformConfig
from app.schemas.plan import ImageSlot
from app.schemas.skill import SkillCard
from app.schemas.video import StoryboardShot

IDEA_PLACEHOLDER = "（未填写）"
SYSTEM_PROMPT = "你是电商 AIGC 内容工厂中的专业 Skill 执行器。严格按照用户消息中的 Skill 指令与输出格式工作。"


def _dump(model) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _idea(text: str | None) -> str:
    return text.strip() if text and text.strip() else IDEA_PLACEHOLDER


def compile_planner(
    skill: SkillCard,
    deliverable_type: str,
    category: CategoryConfig,
    platform: PlatformConfig,
    aspect_ratio: str,
    requested_count: int | None,
    user_creative_idea: str | None,
) -> str:
    return skill.render(
        deliverable_type=deliverable_type,
        category_json=_dump(category),
        platform_json=_dump(platform),
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
) -> str:
    return skill.render(
        video_type=video_type,
        aspect_ratio=aspect_ratio,
        target_duration_seconds=target_duration_seconds,
        category_json=_dump(category),
        platform_json=_dump(platform),
        user_creative_idea=_idea(user_creative_idea),
    )


def compile_image_prompt(
    skill: SkillCard,
    slot: ImageSlot,
    category: CategoryConfig,
    platform: PlatformConfig,
    reference_roles: list[str],
) -> str:
    slot_data = slot.model_dump(
        mode="json", exclude={"candidates", "extra_reference_ids", "excluded_reference_ids", "compiled_prompt"}
    )
    return skill.render(
        slot_json=json.dumps(slot_data, ensure_ascii=False, indent=2),
        category_json=_dump(category),
        platform_json=_dump(platform),
        aspect_ratio=slot.aspect_ratio,
        reference_roles="、".join(reference_roles) if reference_roles else "无（纯文生图）",
    )


def compile_h3_prompt(
    skill: SkillCard, shot: StoryboardShot, aspect_ratio: str, category: CategoryConfig
) -> str:
    shot_data = shot.model_dump(
        mode="json",
        include={
            "duration_seconds", "shot_purpose", "shot_size", "camera_movement", "scene_description",
            "subject_action", "first_frame_description", "audio_mode", "character_dialogue",
            "ambient_sound_description",
        },
    )
    return skill.render(
        shot_json=json.dumps(shot_data, ensure_ascii=False, indent=2),
        aspect_ratio=aspect_ratio,
        duration_seconds=f"{shot.duration_seconds:g}",
        category_json=_dump(category),
    )
