"""跨模块共享的类型、常量与异常。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

AspectRatio = Literal["1:1", "3:4", "4:3", "2:3", "3:2", "9:16", "16:9"]
GenerationMode = Literal["text_to_image", "single_image_edit", "multi_image_fusion"]
PlanStatus = Literal["draft", "confirmed"]
DeliverableType = Literal["main_image", "detail_page"]

# 每种生图模式允许的参考图数量（闭区间）
MODE_REFERENCE_RANGE: dict[str, tuple[int, int]] = {
    "text_to_image": (0, 0),
    "single_image_edit": (1, 1),
    "multi_image_fusion": (2, 3),
}

MAX_CANDIDATES_PER_SLOT = 4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def utc_now() -> datetime:
    return datetime.now(UTC)


def mode_for_reference_count(count: int) -> GenerationMode:
    """按参考图数量推断生图模式；超过 3 张时报错（应拆分槽位）。"""
    if count == 0:
        return "text_to_image"
    if count == 1:
        return "single_image_edit"
    if count <= 3:
        return "multi_image_fusion"
    raise ValueError(f"单个槽位最多 3 张参考图，当前 {count} 张，请拆分槽位")


class PlanStateError(RuntimeError):
    """在错误的状态下执行操作（例如编辑已确认的策划案、未确认就进入渲染）。"""


class PlanValidationError(ValueError):
    """策划案内容不一致，无法确认。issues 为逐条问题描述。"""

    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("策划案存在问题，无法确认：\n- " + "\n- ".join(issues))


class EditRecord(BaseModel):
    """一条用户/系统编辑记录，用于审计与回溯。"""

    timestamp: datetime = Field(default_factory=utc_now)
    action: str
    target_id: str | None = None
    details: dict = Field(default_factory=dict)
