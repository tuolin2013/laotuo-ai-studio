"""视频原生音画分镜编剧引擎：执行 video_scriptwriter Skill，输出 draft 状态的分镜剧本。

双路径，统一输出 VideoScript（后续确认 / 首帧 / H3 / 视频流程完全共用）：
- mode="original"：按视频类型、镜头数与想法原创策划；
- mode="clone"：以参考爆款视频的关键帧（多模态图像）+ 时间戳为依据执行 hypit_viral_clone 卡带，
  先输出《原爆款视频拆解报告》，再按原片结构替换为当前商品。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.engine.compiler import SYSTEM_PROMPT, compile_clone_scriptwriter, compile_scriptwriter
from app.engine.runtime import EngineRuntime
from app.engine.video_cloner import frame_to_base64
from app.providers.demo import content as demo
from app.providers.demo.clone import demo_clone_script
from app.schemas.common import mode_for_reference_count
from app.schemas.video import CloneAnalysis, ReferenceVideoSource, StoryboardShot, VideoScript, shot_from_llm

MIN_SHOTS, MAX_SHOTS = 3, 8
CLONE_SKILL_ID = "hypit_viral_clone"
# 克隆剧本没有「视频类型」入参，登记为产品展示类以满足 VideoScript 的类型约束
CLONE_VIDEO_TYPE = "product_showcase"


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
    if data.get("source_timestamp") is None:
        data.pop("source_timestamp", None)
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
        mode: str = "original",
        reference_video: ReferenceVideoSource | None = None,
        clone_strategy: str = "replicate",
    ) -> tuple[VideoScript, str]:
        if mode == "clone":
            return await self._generate_clone(
                category_id=category_id, platform_id=platform_id, aspect_ratio=aspect_ratio,
                user_creative_idea=user_creative_idea, product_info=product_info, source=reference_video,
                strategy=clone_strategy, skill_id=skill_id, existing=existing,
                target_duration_seconds=target_duration_seconds)
        if mode != "original":
            raise ValueError(f"未知的剧本生成方式：{mode}")
        cat, plat = self.rt.category(category_id), self.rt.platform(platform_id)
        shot_count = max(MIN_SHOTS, min(MAX_SHOTS, int(shot_count)))
        duration = target_duration_seconds or max(15, 3 + (shot_count - 1) * 3)
        info = {k: v for k, v in (product_info or {}).items() if v}
        skill = self.rt.skills.get("video_scriptwriter", skill_id)
        prompt = compile_scriptwriter(skill, video_type, aspect_ratio, duration, cat, plat, user_creative_idea,
                                      shot_count=shot_count, product_info=info)
        if self.rt.is_live("llm"):
            data = await self.rt.llm().complete_json(SYSTEM_PROMPT, prompt)
            mode_text = "真实 LLM"
        else:
            await asyncio.sleep(self.rt.demo_delay)
            data = demo.demo_script(video_type, cat, plat, aspect_ratio, shot_count, duration, user_creative_idea,
                                    info)
            mode_text = "演示模式"
        script = self._assemble(data, existing, platform_id=platform_id, category_id=category_id,
                                skill_id=skill_id, video_type=video_type, aspect_ratio=aspect_ratio,
                                duration=duration, idea=user_creative_idea)
        script.script_mode, script.clone_strategy = "original", None
        script.reference_video, script.clone_analysis = None, None
        return script, mode_text

    @staticmethod
    def _assemble(data: dict[str, Any], existing: VideoScript | None, *, platform_id: str, category_id: str,
                  skill_id: str, video_type: str, aspect_ratio: str, duration: int,
                  idea: str | None) -> VideoScript:
        """把 Skill 输出的 shots 写入同一个可编辑 VideoScript（草稿则原地替换，保留编辑记录）。"""
        shots = [shot_from_llm(_normalize_shot_data(s, i == 0)) for i, s in enumerate(data.get("shots") or [])]
        if not shots:
            raise ValueError("编剧 Skill 没有输出任何分镜（shots 为空）")
        if existing is not None and existing.plan_status == "draft":
            script = existing
            script.video_type = video_type
            script.aspect_ratio = aspect_ratio
            script.target_duration_seconds = duration
            script.scriptwriter_skill_id = skill_id
            script.platform_id, script.category_id = platform_id, category_id
        else:
            script = VideoScript(
                platform_id=platform_id, category_id=category_id, scriptwriter_skill_id=skill_id,
                video_type=video_type, aspect_ratio=aspect_ratio, target_duration_seconds=duration,
            )
        script.replace_all_items(shots, idea)
        script.script_summary = data.get("script_summary", "")
        return script

    async def _generate_clone(
        self, *, category_id: str, platform_id: str, aspect_ratio: str, user_creative_idea: str | None,
        product_info: dict[str, str] | None, source: ReferenceVideoSource | None, strategy: str, skill_id: str,
        existing: VideoScript | None, target_duration_seconds: int | None,
    ) -> tuple[VideoScript, str]:
        if source is None or not source.frames:
            raise ValueError("请先上传参考爆款视频并完成关键帧抽取")
        if strategy not in {"replicate", "adapt"}:
            raise ValueError(f"未知的克隆策略：{strategy}")
        cat, plat = self.rt.category(category_id), self.rt.platform(platform_id)
        info = {k: v for k, v in (product_info or {}).items() if v}
        # 每张关键帧代表原片一段，分镜数 = 关键帧数；总时长跟随原片
        shot_count = max(MIN_SHOTS, min(MAX_SHOTS, len(source.frames)))
        duration = target_duration_seconds or round(max(5, min(60, source.duration_seconds)))
        skill = self.rt.skills.get("video_scriptwriter", skill_id)
        if self.rt.is_live("llm"):
            prompt = compile_clone_scriptwriter(skill, source, strategy, aspect_ratio, duration, cat, plat,
                                                user_creative_idea, shot_count, info)
            images = [frame_to_base64(f.file_path) for f in source.frames]
            data = await self.rt.llm().complete_json(SYSTEM_PROMPT, prompt, images=images)
            mode_text = "真实 LLM · 多模态识图"
        else:
            await asyncio.sleep(self.rt.demo_delay)
            data = demo_clone_script(source, cat, plat, strategy, aspect_ratio, user_creative_idea, info)
            mode_text = "演示模式"
        analysis = CloneAnalysis.model_validate(data.get("clone_analysis") or {})
        script = self._assemble(data, existing, platform_id=platform_id, category_id=category_id,
                                skill_id=skill_id, video_type=CLONE_VIDEO_TYPE, aspect_ratio=aspect_ratio,
                                duration=duration, idea=user_creative_idea)
        script.script_mode, script.clone_strategy = "clone", strategy
        script.reference_video, script.clone_analysis = source, analysis
        return script, mode_text

    async def regenerate_shot(
        self, script: VideoScript, shot_id: str, product_info: dict[str, str] | None = None
    ) -> StoryboardShot:
        script._require_draft()
        cat, plat = self.rt.category(script.category_id), self.rt.platform(script.platform_id)
        index = script._index_of(shot_id)
        info = {k: v for k, v in (product_info or {}).items() if v}
        if self.rt.is_live("llm"):
            # 克隆卡带依赖整条参考视频；单镜重写使用原创编剧卡带，并附上原片拆解报告作为结构约束
            sid = script.scriptwriter_skill_id if script.script_mode == "original" else "default_scriptwriter"
            skill = self.rt.skills.get("video_scriptwriter", sid)
            base = compile_scriptwriter(skill, script.video_type, script.aspect_ratio, script.target_duration_seconds,
                                        cat, plat, script.user_creative_idea, shot_count=1, product_info=info)
            role = "黄金前 3 秒钩子" if index == 0 else f"第 {index + 1} 个分镜"
            prompt = f"{base}\n\n# 补充要求\n只重新编写{role}，shots 数组只含 1 个元素，与原剧本其他镜头衔接。"
            if script.clone_analysis is not None:
                prompt += "\n\n# 参考爆款视频拆解（保持该结构与节奏）\n\n" + script.clone_analysis.to_markdown()
            data = ((await self.rt.llm().complete_json(SYSTEM_PROMPT, prompt)).get("shots") or [{}])[0]
        else:
            await asyncio.sleep(self.rt.demo_delay)
            data = demo.demo_shot(script.video_type, cat, plat, script.aspect_ratio, script.user_creative_idea,
                                  info, index)
        old = script.get_shot(shot_id)
        if old.source_timestamp and not data.get("source_timestamp"):
            data = {**data, "source_timestamp": old.source_timestamp}
        return script.regenerate_shot(shot_id, shot_from_llm(_normalize_shot_data(data, index == 0)))
