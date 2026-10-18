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

# 按内容形式分类（商用成熟分类）
VideoType = Literal["lifestyle_seeding", "product_showcase", "trust_building", "comparison_review", "emotional_story"]
VIDEO_TYPE_LABELS: dict[str, str] = {
    "lifestyle_seeding": "场景种草类（痛点场景 + 解决方案 / 使用场景植入）",
    "product_showcase": "产品展示类（开箱 / 冲泡 / 喂食演示 / 细节特写）",
    "trust_building": "信任背书类（产地溯源 / 成分检测 / 工艺认证）",
    "comparison_review": "对比测评类（横向对比 / 前后效果 / 成分拆解）",
    "emotional_story": "情感故事类（品牌故事 / 用户故事 / 匠人精神）",
}

# 官方 h3-prompt-writing 的两种输入模式：I2VA 首帧模式（base-en.txt）/ Ref2VA 全参考模式（ref-en.txt）
H3PromptMode = Literal["i2va", "ref2va"]
H3_PROMPT_MODES: tuple[H3PromptMode, ...] = ("i2va", "ref2va")
H3_MODE_LABELS: dict[str, str] = {"i2va": "I2VA 首帧模式", "ref2va": "Ref2VA 全参考模式"}

_DIALOGUE_MODES = {"native_dialogue", "dialogue_and_ambient"}
_AMBIENT_MODES = {"ambient_sfx", "dialogue_and_ambient"}


class ConceptOutline(BaseModel):
    """创意大纲：30秒电梯演讲,讲清楚钩子、卖点、行动号召。"""

    concept_id: str = Field(default_factory=lambda: new_id("concept"))
    video_type: VideoType
    aspect_ratio: AspectRatio
    target_duration_seconds: int = Field(default=15, ge=5, le=120)

    # 核心创意
    hook_design: str = Field(description="钩子设计:前3秒用什么抓住用户(视觉/声音/反差/冲突)")
    core_message: str = Field(description="核心信息:这条视频要传递的一句话卖点")
    emotional_arc: str = Field(description="情感曲线:从什么情绪到什么情绪(如:好奇→惊喜→信任)")
    call_to_action: str = Field(description="行动号召:结尾如何促成转化")

    # 结构规划
    scenes_count: int = Field(default=3, ge=2, le=6, description="分几个场景/段落")
    scenes_outline: list[str] = Field(default_factory=list, description="每个场景的一句话概述")

    created_at: datetime = Field(default_factory=utc_now)


class StoryScript(BaseModel):
    """完整剧本:人类可读的 Markdown 格式,包含故事、对白、场景描述。"""

    story_id: str = Field(default_factory=lambda: new_id("story"))
    concept_id: str | None = None  # 关联的创意大纲
    video_type: VideoType
    aspect_ratio: AspectRatio

    # 剧本正文(Markdown格式)
    title: str = Field(description="视频标题")
    logline: str = Field(description="一句话故事梗概")
    content: str = Field(description="完整剧本正文(Markdown):分场景,含对白、动作、场景描述、情感标注")

    # 创作元数据
    characters: list[str] = Field(default_factory=list, description="出场人物列表")
    locations: list[str] = Field(default_factory=list, description="拍摄场景列表")
    props: list[str] = Field(default_factory=list, description="关键道具")

    plan_status: Literal["draft", "confirmed"] = "draft"
    created_at: datetime = Field(default_factory=utc_now)

    def confirm(self) -> None:
        """确认剧本,准备编译成分镜脚本。"""
        if not self.content.strip():
            raise ValueError("剧本内容不能为空")
        self.plan_status = "confirmed"

    def unlock(self) -> None:
        """解锁剧本返回修改。"""
        self.plan_status = "draft"


# 剧本来源：自己策划原创 / 克隆参考爆款视频（两者产出同一种 VideoScript，后续流程完全共用）
ScriptMode = Literal["original", "clone"]
CloneStrategy = Literal["replicate", "adapt"]
CLONE_STRATEGY_LABELS: dict[str, str] = {
    "replicate": "1:1 复刻分镜节奏仅换品",
    "adapt": "借鉴钩子与结构改编",
}


class ReferenceVideoFrame(BaseModel):
    """参考视频按时间轴均匀抽取的关键帧。"""

    index: int = Field(ge=1)
    timestamp_seconds: float = Field(ge=0)
    timestamp: str  # 如 00:01.5
    file_path: Path
    # 本地像素统计（演示模式拆解用）：平均亮度 0~255；与上一关键帧的画面变化 0~1（越大越可能是切镜）
    brightness: float = 0
    change_score: float = 0


class ReferenceVideoSource(BaseModel):
    """用户上传的参考爆款视频及其关键帧。"""

    video_id: str = Field(default_factory=lambda: new_id("refvid"))
    file_path: Path
    file_name: str = ""
    duration_seconds: float = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    aspect_ratio: AspectRatio
    frames: list[ReferenceVideoFrame] = Field(default_factory=list)


class CloneShotBreakdown(BaseModel):
    """原片逐段拆解：时间点、A/B-Roll、景别、运镜、台词锚点。"""

    timestamp: str = ""
    roll: str = ""
    shot_size: str = ""
    camera_movement: str = ""
    content: str = ""
    dialogue_anchor: str = ""


class CloneAnalysis(BaseModel):
    """《原爆款视频拆解报告》。"""

    hook_analysis: str = ""
    rhythm_analysis: str = ""
    a_b_roll_ratio: str = ""
    dialogue_anchoring: str = ""
    shot_breakdown: list[CloneShotBreakdown] = Field(default_factory=list)
    source_note: str = ""

    def to_markdown(self) -> str:
        def cell(text: str) -> str:
            return (text or "-").replace("|", "／").replace("\n", " ")

        parts = [
            "### 🔍 原爆款视频拆解报告",
            f"**① 前 3 秒钩子手法**：{self.hook_analysis or '-'}",
            f"**② 切镜节奏**：{self.rhythm_analysis or '-'}",
            f"**③ A-Roll / B-Roll 比例**：{self.a_b_roll_ratio or '-'}",
            f"**④ 台词与画面锚定**：{self.dialogue_anchoring or '-'}",
        ]
        if self.shot_breakdown:
            rows = ["| 原片时间点 | A/B-Roll | 景别 | 运镜 | 原片画面 | 台词 / 声音锚点 |", "|---|---|---|---|---|---|"]
            rows += [f"| {cell(b.timestamp)} | {cell(b.roll)} | {cell(b.shot_size)} | {cell(b.camera_movement)} | "
                     f"{cell(b.content)} | {cell(b.dialogue_anchor)} |" for b in self.shot_breakdown]
            parts.append("\n".join(rows))
        if self.source_note:
            parts.append(f"> {self.source_note}")
        return "\n\n".join(parts)


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
    # 克隆模式：该分镜复刻的原片时间点（如 00:01.5）；原创剧本为空
    source_timestamp: str = ""

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

    # ---- 剧本来源：原创策划 / 克隆参考爆款视频 ----
    script_mode: ScriptMode = "original"
    clone_strategy: CloneStrategy | None = None
    reference_video: ReferenceVideoSource | None = None
    clone_analysis: CloneAnalysis | None = None

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
