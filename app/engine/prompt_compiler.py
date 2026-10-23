"""生图提示词与 MiniMax H3 音画提示词编译引擎。

- 生图：执行 image_prompt Skill，依据槽位「实际绑定」的参考图数量（0 / 1 / 多图）编译。
- H3：执行 video_prompt Skill，融合运镜标签、主体动作、人物同期声对白与环境音效。
  官方卡带的两种写法由系统按分镜自动选择（auto_h3_mode），用户不需要理解或选择。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from app.engine.compiler import (
    SYSTEM_PROMPT,
    compile_h3_prompt,
    compile_image_prompt,
    compile_official_h3_prompt,
    h3_structure_issues,
    supports_h3_modes,
)
from app.engine.runtime import EngineRuntime
from app.providers.demo import content as demo
from app.providers.demo.h3_official import demo_official_h3_prompt
from app.providers.image_models import protocol_of
from app.schemas.editable import ReferenceAsset
from app.schemas.plan import ImageSlot
from app.schemas.video import H3_MODE_LABELS, H3PromptMode, StoryboardShot, VideoScript

DEFAULT_IMAGE_SKILL = {"openai": "gpt_image_2", "gemini": "gemini_nano_banana"}
DEFAULT_H3_SKILL = "official_h3_prompt_writing"
HANZI = re.compile(r"[\u4e00-\u9fff]")


def image_skill_for_model(model: str) -> str:
    """按模型目录登记的协议选择默认生图提示词 Skill。"""
    return DEFAULT_IMAGE_SKILL[protocol_of(model)]


def h3_mode_label(shot: StoryboardShot) -> str:
    """日志 / 界面用：当前 H3 提示词采用的写法。"""
    mode = shot.h3_official_mode
    return H3_MODE_LABELS[mode] if mode else "自定义卡带格式"


def auto_h3_mode(script: VideoScript, shot: StoryboardShot) -> H3PromptMode:
    """分镜绑定了商品参考图 → Ref2VA（首帧 + 参考图一起送给 H3，包装更稳）；没有 → I2VA（只用锁定首帧）。

    与视频后端的传图规则一致：只有文件确实存在的参考图才会被送出。
    """
    refs = [a for a in script.references_for(shot.shot_id) if a.file_path.exists()]
    return "ref2va" if refs else "i2va"


def slot_prompt_data(slot: ImageSlot, deliverable_type: str | None = None) -> dict[str, Any]:
    """槽位的生图输入。

    deliverable_type 让生图 Skill 区分主图与详情页：详情页信息屏（用法 / 问答 / 配料表等）放宽上图字数；
    copy_text_hanzi_count 由代码数好汉字数，模型不用自己数（大模型数中文字数不可靠）。
    """
    data = slot.model_dump(
        mode="json",
        include={"title", "subtitle", "copy_text", "selling_point", "visual_description", "differentiator",
                 "generation_mode", "aspect_ratio", "aesthetic_style", "composition_mode"},
    )
    if deliverable_type:
        data["deliverable_type"] = deliverable_type
    data["copy_text_hanzi_count"] = len(HANZI.findall(slot.copy_text or ""))
    return data


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

    async def slot_prompt(self, slot: ImageSlot, references, category_id, platform_id, skill_id,
                          deliverable_type: str | None = None) -> str:
        return await self.image_prompt(slot_prompt_data(slot, deliverable_type), references, category_id,
                                       platform_id, slot.aspect_ratio, skill_id)

    async def first_frame_prompt(self, script: VideoScript, shot: StoryboardShot, references, skill_id) -> str:
        return await self.image_prompt(shot_frame_data(shot, script.aspect_ratio), references, script.category_id,
                                       script.platform_id, script.aspect_ratio, skill_id)

    async def h3_prompt(
        self,
        script: VideoScript,
        shot: StoryboardShot,
        skill_id: str = DEFAULT_H3_SKILL,
        mode: H3PromptMode | None = None,
        activate: bool = True,
    ) -> str:
        """编译单个分镜的 H3 提示词。

        官方 h3-prompt-writing 卡带支持两种模式：i2va（base-en.txt 三段式）/ ref2va（ref-en.txt 六段式）；
        mode 不传时按 auto_h3_mode 自动选择。旧版自定义卡带（正文不含 {{ h3_mode }}）仍按原格式编译。
        """
        script.require_first_frame_confirmed(shot.shot_id)
        cat = self.rt.category(script.category_id)
        roles = [a.role for a in script.references_for(shot.shot_id)]
        skill = self.rt.skills.get("video_prompt", skill_id or DEFAULT_H3_SKILL)
        official = supports_h3_modes(skill)
        mode = mode or auto_h3_mode(script, shot)
        if self.rt.is_live("llm"):
            if official:
                text = compile_official_h3_prompt(skill, shot, script.aspect_ratio, cat, mode, roles)
            else:
                text = compile_h3_prompt(skill, shot, script.aspect_ratio, cat)
            out = await self.rt.llm().complete_json(SYSTEM_PROMPT, text)
        else:
            await asyncio.sleep(self.rt.demo_delay / 2)
            if official:
                out = demo_official_h3_prompt(shot.model_dump(mode="json"), script.aspect_ratio, cat, mode, roles)
            else:
                out = demo.demo_h3_prompt(shot.model_dump(mode="json"), script.aspect_ratio)
        prompt = str(out.get("prompt", "")).strip()
        if official:
            issues = h3_structure_issues(prompt, mode)
            if issues:
                raise ValueError(f"H3 提示词不符合官方 {mode} 结构：{'；'.join(issues)}")
        script.set_h3_prompt(shot.shot_id, prompt, mode=mode if official else None, activate=activate)
        return prompt
