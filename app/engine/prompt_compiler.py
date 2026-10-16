"""生图提示词与 MiniMax H3 音画提示词编译引擎。

- 生图：执行 image_prompt Skill，依据槽位「实际绑定」的参考图数量（0 / 1 / 多图）编译。
- H3：执行 video_prompt Skill，融合运镜标签、主体动作、人物同期声对白与环境音效；
  并汇总一条「基于第 1 分镜首帧的全局连续提示词」供 ComfyUI MiniMax All-in-One 使用。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.engine.compiler import SYSTEM_PROMPT, compile_h3_prompt, compile_image_prompt
from app.engine.runtime import EngineRuntime
from app.providers.demo import content as demo
from app.providers.image_models import protocol_of
from app.schemas.editable import ReferenceAsset
from app.schemas.plan import ImageSlot
from app.schemas.video import StoryboardShot, VideoScript

DEFAULT_IMAGE_SKILL = {"openai": "gpt_image_2", "gemini": "gemini_nano_banana"}


def image_skill_for_model(model: str) -> str:
    """按模型目录登记的协议选择默认生图提示词 Skill。"""
    return DEFAULT_IMAGE_SKILL[protocol_of(model)]


def slot_prompt_data(slot: ImageSlot) -> dict[str, Any]:
    return slot.model_dump(
        mode="json",
        include={"title", "subtitle", "copy_text", "selling_point", "visual_description", "differentiator",
                 "generation_mode", "aspect_ratio"},
    )


def shot_frame_data(shot: StoryboardShot, aspect_ratio: str) -> dict[str, Any]:
    """首帧图的生图输入：静止画面，不排版文字。"""
    return {
        "title": "",
        "copy_text": "",
        "visual_description": f"{shot.first_frame_description}。（{shot.shot_size}，{shot.scene_description}）"
                              "这是视频的静止首帧，画面中不要出现任何文字。",
        "generation_mode": shot.first_frame_generation_mode,
        "aspect_ratio": aspect_ratio,
    }


class PromptCompiler:
    def __init__(self, rt: EngineRuntime):
        self.rt = rt

    async def image_prompt(
        self,
        data: dict[str, Any],
        references: list[ReferenceAsset],
        category_id: str,
        platform_id: str,
        aspect_ratio: str,
        skill_id: str,
    ) -> str:
        cat, plat = self.rt.category(category_id), self.rt.platform(platform_id)
        roles = [a.role for a in references]
        # 模式以实际绑定的参考图数量为准
        data = {**data, "generation_mode": {0: "text_to_image", 1: "single_image_edit"}.get(len(roles),
                                                                                         "multi_image_fusion")}
        if self.rt.is_live("llm"):
            skill = self.rt.skills.get("image_prompt", skill_id)
            text = compile_image_prompt(skill, data, cat, plat, aspect_ratio, roles)
            out = await self.rt.llm().complete_json(SYSTEM_PROMPT, text)
        else:
            await asyncio.sleep(self.rt.demo_delay / 3)
            out = demo.demo_image_prompt(skill_id, data, roles, aspect_ratio, cat)
        prompt = str(out.get("prompt", "")).strip()
        if out.get("negative_notes"):
            prompt += f"\nAvoid: {out['negative_notes']}"
        return prompt

    async def slot_prompt(self, slot: ImageSlot, references, category_id, platform_id, skill_id) -> str:
        return await self.image_prompt(slot_prompt_data(slot), references, category_id, platform_id,
                                       slot.aspect_ratio, skill_id)

    async def first_frame_prompt(self, script: VideoScript, shot: StoryboardShot, references, skill_id) -> str:
        return await self.image_prompt(shot_frame_data(shot, script.aspect_ratio), references, script.category_id,
                                       script.platform_id, script.aspect_ratio, skill_id)

    async def h3_prompt(self, script: VideoScript, shot: StoryboardShot, skill_id: str = "minimax_h3_prompt") -> str:
        script.require_first_frame_confirmed(shot.shot_id)
        cat = self.rt.category(script.category_id)
        if self.rt.is_live("llm"):
            skill = self.rt.skills.get("video_prompt", skill_id)
            text = compile_h3_prompt(skill, shot, script.aspect_ratio, cat)
            out = await self.rt.llm().complete_json(SYSTEM_PROMPT, text)
        else:
            await asyncio.sleep(self.rt.demo_delay / 2)
            out = demo.demo_h3_prompt(shot.model_dump(mode="json"), script.aspect_ratio)
        prompt = str(out.get("prompt", "")).strip()
        script.set_h3_prompt(shot.shot_id, prompt)
        return prompt


def global_continuous_prompt(script: VideoScript) -> str:
    """ComfyUI MiniMax All-in-One：以第 1 分镜锁定首帧为起点的全局连续 H3 提示词。"""
    if not script.shots:
        return ""
    total = script.total_duration_seconds
    head = (
        f"以第 1 分镜锁定首帧为起点，生成一条约 {total:g} 秒、画面比例 {script.aspect_ratio} 的连续带货短视频，"
        "全程商品包装的形状、颜色、文字与 logo 保持不变；视频原生生成声音，不使用画外音与旁白。\n"
    )
    t = 0.0
    beats = []
    for i, shot in enumerate(script.shots, start=1):
        start, t = t, t + shot.duration_seconds
        tags = "".join(demo.camera_tags(shot.camera_movement, shot.shot_size))
        if shot.audio_mode == "native_dialogue":
            sound = f"人物同期声：{shot.character_dialogue}"
        elif shot.audio_mode == "ambient_sfx":
            sound = f"环境音：{shot.ambient_sound_description}"
        elif shot.audio_mode == "dialogue_and_ambient":
            sound = f"人物同期声：{shot.character_dialogue}；环境音：{shot.ambient_sound_description}"
        else:
            sound = "静音"
        beats.append(
            f"【{start:g}-{t:g}s｜镜头 {i}】{tags} {shot.scene_description}。{shot.subject_action}。声音——{sound}"
        )
    tail = "\n镜头之间自然衔接、光线连续；写实商业广告质感，无字幕，无水印，商品不变形，口型与台词同步。"
    return head + "\n".join(beats) + tail
