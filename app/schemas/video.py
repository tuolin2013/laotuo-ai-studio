"""带货短视频分镜剧本模型（MiniMax H3 原生音画一体，无画外音）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import (
    AspectRatio,
    GenerationMode,
    PlanStateError,
    new_id,
    utc_now,
)
from app.schemas.editable import BindableItem, EditablePlanBase, ReferenceRequirement

AudioMode = Literal["native_dialogue", "ambient_sfx", "dialogue_and_ambient", "silent"]
ShotPurpose = Literal["hook", "selling_point", "proof", "scene", "call_to_action"]
# 痛点种草 / 产地溯源 / 成分测评 / 沉浸冲泡·喂食展示
VideoType = Literal["pain_point_seeding", "origin_story", "ingredient_review", "immersive_demo"]
VIDEO_TYPE_LABELS: dict[str, str] = {
    "pain_point_seeding": "痛点种草",
    "origin_story": "产地溯源",
    "ingredient_review": "成分测评",
    "immersive_demo": "沉浸冲泡 / 喂食展示",
}

# 官方 h3-prompt-writing 的两种输入模式：I2VA 首帧模式（base-en.txt）/ Ref2VA 全参考模式（ref-en.txt）
H3PromptMode = Literal["i2va", "ref2va"]
H3_PROMPT_MODES: tuple[H3PromptMode, ...] = ("i2va", "ref2va")
H3_MODE_LABELS: dict[str, str] = {"i2va": "I2VA 首帧模式", "ref2va": "Ref2VA 全参考模式"}

_DIALOGUE_MODES = {"native_dialogue", "dialogue_and_ambient"}
_AMBIENT_MODES = {"ambient_sfx", "dialogue_and_ambient"}


class FirstFrameImage(BaseModel):
    """分镜首帧候选图。比例由所属剧本强制锁定。"""

    frame_id: str = Field(default_factory=lambda: new_id("frame"))
    file_path: Path
    aspect_ratio: AspectRatio
    model: str
    prompt: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class VideoAsset(BaseModel):
    """分镜视频：由 Provider 生成，或从 ComfyUI 等外部工具回传绑定。"""

    video_id: str = Field(default_factory=lambda: new_id("vid"))
    file_path: Path
    provider: str
    source: Literal["generated", "external_upload"] = "generated"
    created_at: datetime = Field(default_factory=utc_now)


class StoryboardShot(BindableItem):
    shot_id: str = Field(default_factory=lambda: new_id("shot"))
    duration_seconds: float = Field(gt=0, le=20)
    shot_purpose: ShotPurpose = "selling_point"
    shot_size: str = ""
    camera_movement: str = ""
    scene_description: str
    subject_action: str = ""
    first_frame_description: str

    # —— 原生声音（H3 自带声音，不设画外音）——
    audio_mode: AudioMode
    character_dialogue: str | None = None
    ambient_sound_description: str = ""

    first_frame_generation_mode: GenerationMode = "text_to_image"

    # —— 渲染阶段产物 ——
    first_frame_prompt: str | None = None
    first_frame_candidates: list[FirstFrameImage] = Field(default_factory=list)
    confirmed_first_frame_id: str | None = None
    h3_prompt: str | None = None  # 当前用于视频生成的提示词（= 当前模式的版本）
    h3_prompt_mode: H3PromptMode = "i2va"
    h3_prompt_variants: dict[H3PromptMode, str] = Field(default_factory=dict)
    videos: list[VideoAsset] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _reject_voiceover(cls, data: Any) -> Any:
        if isinstance(data, dict):
            bad = [k for k in data if "voiceover" in k.lower() or "narration" in k.lower()]
            if bad:
                raise ValueError(
                    f"MiniMax H3 原生带声音，不支持画外音字段：{bad}。"
                    "请改用 character_dialogue（镜头内人物同期声）或 ambient_sound_description"
                )
        return data

    @model_validator(mode="after")
    def _check_audio(self):
        if self.character_dialogue is not None:
            self.character_dialogue = self.character_dialogue.strip() or None
        self.ambient_sound_description = self.ambient_sound_description.strip()
        if self.audio_mode in _DIALOGUE_MODES and not self.character_dialogue:
            raise ValueError(f"audio_mode={self.audio_mode} 需要填写 character_dialogue（人物同期声对白）")
        if self.audio_mode in _AMBIENT_MODES and not self.ambient_sound_description:
            raise ValueError(f"audio_mode={self.audio_mode} 需要填写 ambient_sound_description（原生环境音）")
        if self.audio_mode not in _DIALOGUE_MODES and self.character_dialogue:
            raise ValueError(f"audio_mode={self.audio_mode} 不应包含人物对白，请改为 native_dialogue 等模式")
        if self.audio_mode == "silent" and self.ambient_sound_description:
            raise ValueError("audio_mode=silent 时不应填写环境音")
        return self

    @property
    def item_id(self) -> str:
        return self.shot_id

    @property
    def mode(self) -> GenerationMode:
        return self.first_frame_generation_mode

    @property
    def confirmed_first_frame(self) -> FirstFrameImage | None:
        for f in self.first_frame_candidates:
            if f.frame_id == self.confirmed_first_frame_id:
                return f
        return None


class VideoScript(EditablePlanBase):
    """完整分镜剧本。所有分镜与首帧共用同一个视频比例。"""

    ITEMS_FIELD: ClassVar[str] = "shots"
    LOCKED_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"first_frame_candidates", "confirmed_first_frame_id", "videos", "h3_prompt", "first_frame_prompt",
         "h3_prompt_mode", "h3_prompt_variants"}
    )

    script_id: str = Field(default_factory=lambda: new_id("script"))
    platform_id: str
    category_id: str
    scriptwriter_skill_id: str
    video_type: VideoType
    aspect_ratio: AspectRatio
    target_duration_seconds: int = Field(default=15, ge=5, le=120)
    script_summary: str = ""
    shots: list[StoryboardShot] = Field(default_factory=list)

    # ---- 语义化别名 ----

    def add_shot(self, shot: StoryboardShot, index: int | None = None) -> StoryboardShot:
        return self.add_item(shot, index)

    def remove_shot(self, shot_id: str) -> None:
        self.remove_item(shot_id)

    def move_shot(self, shot_id: str, new_index: int) -> None:
        self.move_item(shot_id, new_index)

    def update_shot(self, shot_id: str, /, **changes: Any) -> StoryboardShot:
        return self.update_item(shot_id, **changes)

    def regenerate_shot(self, shot_id: str, new_shot: StoryboardShot) -> StoryboardShot:
        return self.replace_item(shot_id, new_shot)

    def get_shot(self, shot_id: str) -> StoryboardShot:
        return self.get_item(shot_id)

    def _on_item_edited(self, item: StoryboardShot, changed: set[str]) -> None:
        # 剧本内容变了，已生成的首帧与 H3 提示词全部失效
        item.first_frame_candidates = []
        item.first_frame_prompt = None
        item.confirmed_first_frame_id = None
        item.h3_prompt = None
        item.h3_prompt_variants = {}
        item.videos = []

    @property
    def total_duration_seconds(self) -> float:
        return sum(s.duration_seconds for s in self.shots)

    def validate_plan(self) -> list[str]:
        issues = super().validate_plan()
        if self.shots and self.shots[0].shot_purpose != "hook":
            issues.append("第 1 个分镜必须是黄金前 3 秒钩子（shot_purpose=hook）")
        if self.shots and self.shots[0].duration_seconds > 3:
            issues.append("第 1 个分镜（钩子）时长不能超过 3 秒")
        return issues

    # ---------- 首帧（比例锁死） ----------

    def add_first_frame(
        self, shot_id: str, file_path: Path | str, model: str, prompt: str = "", aspect_ratio: str | None = None
    ) -> FirstFrameImage:
        """登记一张首帧候选图。首帧比例强制等于视频比例。"""
        self.require_confirmed()
        if aspect_ratio is not None and aspect_ratio != self.aspect_ratio:
            raise ValueError(f"首帧比例 {aspect_ratio} 与视频比例 {self.aspect_ratio} 不一致，已拦截")
        shot = self.get_shot(shot_id)
        frame = FirstFrameImage(
            file_path=Path(file_path), aspect_ratio=self.aspect_ratio, model=model, prompt=prompt
        )
        shot.first_frame_candidates.append(frame)
        self._log("add_first_frame", shot_id, frame_id=frame.frame_id)
        return frame

    def confirm_first_frame(self, shot_id: str, frame_id: str) -> None:
        """用户勾选确认「商品不变形」的首帧。"""
        self.require_confirmed()
        shot = self.get_shot(shot_id)
        if frame_id not in {f.frame_id for f in shot.first_frame_candidates}:
            raise KeyError(f"找不到首帧：{frame_id}")
        shot.confirmed_first_frame_id = frame_id
        shot.h3_prompt = None
        shot.h3_prompt_variants = {}
        self._log("confirm_first_frame", shot_id, frame_id=frame_id)

    def require_first_frame_confirmed(self, shot_id: str) -> FirstFrameImage:
        """编译 H3 提示词 / 生成视频前的闸门。"""
        self.require_confirmed()
        frame = self.get_shot(shot_id).confirmed_first_frame
        if frame is None:
            raise PlanStateError("该分镜首帧尚未确认，请先勾选一张商品不变形的首帧图")
        return frame

    def set_h3_prompt(
        self, shot_id: str, prompt: str, mode: H3PromptMode | None = None, activate: bool = True
    ) -> None:
        """保存某个模式的 H3 提示词；activate=True 时设为视频生成使用的版本。"""
        self.require_first_frame_confirmed(shot_id)
        shot = self.get_shot(shot_id)
        mode = mode or shot.h3_prompt_mode
        shot.h3_prompt_variants[mode] = prompt
        if activate or shot.h3_prompt is None:
            shot.h3_prompt_mode = mode
            shot.h3_prompt = prompt
        self._log("set_h3_prompt", shot_id, mode=mode)

    def bind_video(
        self, shot_id: str, file_path: Path | str, provider: str, source: str = "generated"
    ) -> VideoAsset:
        """绑定生成的视频，或从 ComfyUI 等外部工具回传的视频。"""
        self.require_first_frame_confirmed(shot_id)
        asset = VideoAsset(file_path=Path(file_path), provider=provider, source=source)
        self.get_shot(shot_id).videos.append(asset)
        self._log("bind_video", shot_id, video_id=asset.video_id, source=source)
        return asset

    def combined_h3_prompts(self) -> str:
        """全局汇总：按分镜顺序拼接所有已编译的 H3 提示词，便于一次性复制。"""
        parts = []
        for i, shot in enumerate(self.shots, start=1):
            prompt = shot.h3_prompt or "（尚未编译）"
            parts.append(f"【分镜 {i}｜{shot.duration_seconds:g} 秒】\n{prompt}")
        return "\n\n".join(parts)


def shot_from_llm(data: dict[str, Any]) -> StoryboardShot:
    """把编剧 Skill 输出的单个分镜 JSON 转成 StoryboardShot。"""
    refs = [ReferenceRequirement.model_validate(r) for r in data.get("required_references") or []]
    blocked = {"shot_id", *VideoScript.LOCKED_FIELDS}
    fields = {k: v for k, v in data.items() if k != "required_references" and k not in blocked}
    return StoryboardShot(required_references=refs, **fields)
