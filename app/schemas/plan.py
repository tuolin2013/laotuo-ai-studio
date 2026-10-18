"""主图 / 详情页策划案模型。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from app.schemas.common import (
    MAX_CANDIDATES_PER_SLOT,
    AspectRatio,
    DeliverableType,
    GenerationMode,
    new_id,
    utc_now,
)
from app.schemas.editable import BindableItem, EditablePlanBase, ReferenceRequirement

# 电商图片三维设计体系（详见 config/skills/image_prompt/references/ecommerce_aesthetics_guide.md）
# auto = 交给策划 / 生图 Skill 按指南自动匹配
AestheticStyle = Literal[
    "auto", "new_chinese_zen", "organic_nature_sunlight", "clinical_lab_minimal", "nordic_warm_lifestyle",
    "high_impact_retail",
]
CompositionMode = Literal[
    "auto", "rule_of_thirds_negative_space", "extreme_macro_texture", "depth_layering", "diagonal_dynamic_flow",
    "split_screen_comparison", "knolling_flat_lay",
]

# 中文名（界面、策划指令与演示图共用）
AESTHETIC_STYLE_NAMES: dict[str, str] = {
    "new_chinese_zen": "新中式茶席禅意",
    "organic_nature_sunlight": "原生态山野呼吸",
    "clinical_lab_minimal": "科学实证极简",
    "nordic_warm_lifestyle": "北欧暖阳治愈",
    "high_impact_retail": "高对比零售视觉锤",
}
COMPOSITION_MODE_NAMES: dict[str, str] = {
    "rule_of_thirds_negative_space": "三分留白构图",
    "extreme_macro_texture": "光学微距满画幅",
    "depth_layering": "前中远三层景深",
    "diagonal_dynamic_flow": "对角线动态构图",
    "split_screen_comparison": "分屏对比构图",
    "knolling_flat_lay": "90°俯拍秩序构图",
}


def design_code(value: Any, names: dict[str, str]) -> str | None:
    """把代码或中文名（可带 emoji、引号、只写一部分）解析为设计代码。

    空值 / auto / 自动… 返回 "auto"；无法识别或同时匹配多个时返回 None。
    """
    text = str(value or "").strip().strip("`'\"").strip()
    if not text or text.lower() == "auto" or text.startswith("自动"):
        return "auto"
    if text in names:
        return text
    hits = [code for code, name in names.items()
            if code in text or name in text or (len(text) >= 2 and text in name)]
    return hits[0] if len(hits) == 1 else None


class CandidateImage(BaseModel):
    """一张生成的候选图。"""

    candidate_id: str = Field(default_factory=lambda: new_id("cand"))
    file_path: Path
    model: str
    prompt: str = ""
    selected: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class ImageSlot(BindableItem):
    """主图中的一张图，或详情页中的一屏。"""

    slot_id: str = Field(default_factory=lambda: new_id("slot"))
    title: str
    subtitle: str = ""
    copy_text: str = ""
    selling_point: str = ""
    visual_description: str
    differentiator: str = ""
    generation_mode: GenerationMode
    aspect_ratio: AspectRatio
    # 电商图片三维设计体系
    aesthetic_style: AestheticStyle = "auto"
    composition_mode: CompositionMode = "auto"
    candidate_count: int = Field(default=1, ge=1, le=MAX_CANDIDATES_PER_SLOT)
    candidates: list[CandidateImage] = Field(default_factory=list)
    compiled_prompt: str | None = None

    @property
    def item_id(self) -> str:
        return self.slot_id

    @property
    def mode(self) -> GenerationMode:
        return self.generation_mode


class ImagePlan(EditablePlanBase):
    """主图组或详情页的完整策划案。"""

    ITEMS_FIELD: ClassVar[str] = "slots"
    LOCKED_FIELDS: ClassVar[frozenset[str]] = frozenset({"candidates"})

    plan_id: str = Field(default_factory=lambda: new_id("plan"))
    deliverable_type: DeliverableType
    platform_id: str
    category_id: str
    planner_skill_id: str
    plan_summary: str = ""
    default_aspect_ratio: AspectRatio
    # Step 1 选择的全局美学流派：非 auto 时所有槽位统一使用该流派（表格中仍可逐张修改）
    aesthetic_style: AestheticStyle = "auto"
    slots: list[ImageSlot] = Field(default_factory=list)

    # ---- 语义化别名，便于 UI 层调用 ----

    def add_slot(self, slot: ImageSlot, index: int | None = None) -> ImageSlot:
        return self.add_item(slot, index)

    def remove_slot(self, slot_id: str) -> None:
        self.remove_item(slot_id)

    def move_slot(self, slot_id: str, new_index: int) -> None:
        self.move_item(slot_id, new_index)

    def update_slot(self, slot_id: str, /, **changes: Any) -> ImageSlot:
        return self.update_item(slot_id, **changes)

    def regenerate_slot(self, slot_id: str, new_slot: ImageSlot) -> ImageSlot:
        return self.replace_item(slot_id, new_slot)

    def get_slot(self, slot_id: str) -> ImageSlot:
        return self.get_item(slot_id)

    def _on_item_edited(self, item: ImageSlot, changed: set[str]) -> None:
        # 文案或画面变了，旧的编译提示词失效
        item.compiled_prompt = None

    def set_candidate_count(self, slot_id: str, count: int) -> None:
        """Stage 2 设置单槽位候选图数量（确认后也可调整）。"""
        self.require_confirmed()
        slot = self.get_slot(slot_id)
        if not 1 <= count <= MAX_CANDIDATES_PER_SLOT:
            raise ValueError(f"候选图数量需在 1~{MAX_CANDIDATES_PER_SLOT} 之间")
        slot.candidate_count = count

    def select_candidate(self, slot_id: str, candidate_id: str, selected: bool = True) -> None:
        slot = self.get_slot(slot_id)
        for c in slot.candidates:
            if c.candidate_id == candidate_id:
                c.selected = selected
                return
        raise KeyError(f"找不到候选图：{candidate_id}")


def slot_from_llm(data: dict[str, Any], default_aspect_ratio: AspectRatio) -> ImageSlot:
    """把策划 Skill 输出的单个槽位 JSON 转成 ImageSlot（忽略多余字段）。"""
    refs = [ReferenceRequirement.model_validate(r) for r in data.get("required_references") or []]
    allowed = set(ImageSlot.model_fields) - {"slot_id", "candidates", "required_references"}
    fields = {k: v for k, v in data.items() if k in allowed}
    fields.setdefault("aspect_ratio", default_aspect_ratio)
    # 美学与构图是 LLM 自由输出的字段：缺失或无法识别时退回 auto，不让整套策划失败
    fields["aesthetic_style"] = design_code(fields.get("aesthetic_style"), AESTHETIC_STYLE_NAMES) or "auto"
    fields["composition_mode"] = design_code(fields.get("composition_mode"), COMPOSITION_MODE_NAMES) or "auto"
    return ImageSlot(required_references=refs, **fields)
