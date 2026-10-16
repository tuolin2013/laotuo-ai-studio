"""视频原生音画分镜编剧引擎：执行 video_scriptwriter Skill，输出 draft 状态的分镜剧本。"""

from __future__ import annotations

import asyncio
from typing import Any

from app.engine.compiler import SYSTEM_PROMPT, compile_scriptwriter
from app.engine.runtime import EngineRuntime
from app.providers.demo import content as demo
from app.schemas.common import mode_for_reference_count
from app.schemas.video import StoryboardShot, VideoScript, shot_from_llm

MIN_SHOTS, MAX_SHOTS = 3, 8


def _normalize_shot_data(data: dict[str, Any], is_first: bool) -> dict[str, Any]:
    data = dict(data)
    # 防御：模型若仍输出画外音字段，转为镜头内人物对白而不是直接丢弃
    for key in [k for k in data if "voiceover" in k.lower() or "narration" in k.lower()]:
        vo = data.pop(key)
        if vo and not data.get("character_dialogue"):
            data["character_dialogue"] = f"画面中的人物对着镜头自然地说：“{vo}”"
            data["audio_mode"] = "dialogue_and_ambient" if data.get("ambient_sound_description") else "native_dialogue"
    refs = (data.get("required_references") or [])[:3]
    data["required_references"] = refs
    data["first_frame_generation_mode"] = mode_for_reference_count(len(refs))
    if is_first:
        data["shot_purpose"] = "hook"
        data["duration_seconds"] = min(float(data.get("duration_seconds") or 3), 3)
    return data


class ScriptEngine:
    def __init__(self, rt: EngineRuntime):
        self.rt = rt

    async def generate(
        self,
        *,
        category_id: str,
        platform_id: str,
        video_type: str,
        aspect_ratio: str,
        shot_count: int,
        user_creative_idea: str | None,
        product_info: dict[str, str] | None = None,
        target_duration_seconds: int | None = None,
        skill_id: str = "default_scriptwriter",
        existing: VideoScript | None = None,
    ) -> tuple[VideoScript, str]:
        cat, plat = self.rt.category(category_id), self.rt.platform(platform_id)
        shot_count = max(MIN_SHOTS, min(MAX_SHOTS, int(shot_count)))
        duration = target_duration_seconds or max(15, 3 + (shot_count - 1) * 3)
        info = {k: v for k, v in (product_info or {}).items() if v}
        skill = self.rt.skills.get("video_scriptwriter", skill_id)
        prompt = compile_scriptwriter(skill, video_type, aspect_ratio, duration, cat, plat, user_creative_idea,
                                      shot_count=shot_count, product_info=info)
        if self.rt.is_live("llm"):
            data = await self.rt.llm().complete_json(SYSTEM_PROMPT, prompt)
            mode = "真实 LLM"
        else:
            await asyncio.sleep(self.rt.demo_delay)
            data = demo.demo_script(video_type, cat, plat, aspect_ratio, shot_count, duration, user_creative_idea,
                                    info)
            mode = "演示模式"
        shots = [shot_from_llm(_normalize_shot_data(s, i == 0)) for i, s in enumerate(data.get("shots") or [])]
        if existing is not None and existing.plan_status == "draft":
            script = existing
            script.video_type = video_type
            script.aspect_ratio = aspect_ratio
            script.target_duration_seconds = duration
            script.scriptwriter_skill_id = skill_id
        else:
            script = VideoScript(
                platform_id=platform_id, category_id=category_id, scriptwriter_skill_id=skill_id,
                video_type=video_type, aspect_ratio=aspect_ratio, target_duration_seconds=duration,
            )
        script.replace_all_items(shots, user_creative_idea)
        script.script_summary = data.get("script_summary", "")
        return script, mode

    async def regenerate_shot(
        self, script: VideoScript, shot_id: str, product_info: dict[str, str] | None = None
    ) -> StoryboardShot:
        script._require_draft()
        cat, plat = self.rt.category(script.category_id), self.rt.platform(script.platform_id)
        index = script._index_of(shot_id)
        info = {k: v for k, v in (product_info or {}).items() if v}
        if self.rt.is_live("llm"):
            skill = self.rt.skills.get("video_scriptwriter", script.scriptwriter_skill_id)
            base = compile_scriptwriter(skill, script.video_type, script.aspect_ratio, script.target_duration_seconds,
                                        cat, plat, script.user_creative_idea, shot_count=1, product_info=info)
            role = "黄金前 3 秒钩子" if index == 0 else f"第 {index + 1} 个分镜"
            prompt = f"{base}\n\n# 补充要求\n只重新编写{role}，shots 数组只含 1 个元素，与原剧本其他镜头衔接。"
            data = ((await self.rt.llm().complete_json(SYSTEM_PROMPT, prompt)).get("shots") or [{}])[0]
        else:
            await asyncio.sleep(self.rt.demo_delay)
            data = demo.demo_shot(script.video_type, cat, plat, script.aspect_ratio, script.user_creative_idea,
                                  info, index)
        return script.regenerate_shot(shot_id, shot_from_llm(_normalize_shot_data(data, index == 0)))
