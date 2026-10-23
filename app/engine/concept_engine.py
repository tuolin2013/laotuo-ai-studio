"""创意大纲生成引擎：执行 concept_writer Skill，输出创意大纲。"""

from __future__ import annotations

import asyncio
from typing import Any

from app.engine.compiler import SYSTEM_PROMPT
from app.engine.runtime import EngineRuntime
from app.providers.demo import content as demo
from app.schemas.video import ConceptOutline


def compile_concept_prompt(
    skill: Any,
    video_type: str,
    aspect_ratio: str,
    target_duration_seconds: int,
    category: Any,
    platform: Any,
    user_creative_idea: str | None,
    product_info: dict[str, str] | None = None,
) -> str:
    """编译创意大纲生成提示词。"""
    import json
    idea = user_creative_idea.strip() if user_creative_idea and user_creative_idea.strip() else "（未填写）"
    return skill.render(
        video_type=video_type,
        aspect_ratio=aspect_ratio,
        target_duration_seconds=target_duration_seconds,
        category_json=json.dumps(category.prompt_dict(), ensure_ascii=False, indent=2),
        platform_json=json.dumps(platform.model_dump(), ensure_ascii=False, indent=2),
        product_info_json=json.dumps(product_info or {}, ensure_ascii=False, indent=2),
        user_creative_idea=idea,
    )


class ConceptEngine:
    def __init__(self, rt: EngineRuntime):
        self.rt = rt

    async def generate(
        self,
        *,
        category_id: str,
        platform_id: str,
        video_type: str,
        aspect_ratio: str,
        target_duration_seconds: int | None = None,
        user_creative_idea: str | None,
        product_info: dict[str, str] | None = None,
        skill_id: str = "default_concept_writer",
    ) -> tuple[ConceptOutline, str]:
        """生成创意大纲。"""
        cat, plat = self.rt.category(category_id), self.rt.platform(platform_id)
        duration = target_duration_seconds or 15
        info = {k: v for k, v in (product_info or {}).items() if v}
        skill = self.rt.skills.get("concept_writer", skill_id)
        prompt = compile_concept_prompt(skill, video_type, aspect_ratio, duration, cat, plat, user_creative_idea, info)

        if self.rt.is_live("llm"):
            data = await self.rt.llm().complete_json(SYSTEM_PROMPT, prompt)
            mode = "真实 LLM"
        else:
            await asyncio.sleep(self.rt.demo_delay)
            data = demo.demo_concept(video_type, cat, plat, aspect_ratio, duration, user_creative_idea, info)
            mode = "演示模式"

        concept = ConceptOutline(
            video_type=video_type,
            aspect_ratio=aspect_ratio,
            target_duration_seconds=duration,
            hook_design=data.get("hook_design", ""),
            core_message=data.get("core_message", ""),
            emotional_arc=data.get("emotional_arc", ""),
            call_to_action=data.get("call_to_action", ""),
            scenes_count=data.get("scenes_count", 3),
            scenes_outline=data.get("scenes_outline", []),
        )
        return concept, mode
