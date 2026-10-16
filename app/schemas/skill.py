"""插卡式 Skill 卡带模型。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

SkillSlot = Literal["ecommerce_planner", "video_scriptwriter", "image_prompt", "video_prompt"]
SKILL_SLOTS: tuple[SkillSlot, ...] = (
    "ecommerce_planner",
    "video_scriptwriter",
    "image_prompt",
    "video_prompt",
)

_VAR_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_SKILL_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")


class SkillRenderError(ValueError):
    """渲染 Skill 时缺少变量。"""


class SkillCard(BaseModel):
    skill_id: str
    name: str
    description: str = ""
    version: str = "1.0"
    target_models: list[str] = Field(default_factory=list)
    variables: list[str] = Field(default_factory=list)
    # 附属指南文件（相对卡带所在目录，如 references/base-en.txt）；正文中出现的 references/*.txt 也会自动识别
    references: list[str] = Field(default_factory=list)
    # 为 true 时在界面下拉框中排在第一位，作为该卡槽默认卡带
    default: bool = False
    source_url: str = ""
    slot: SkillSlot
    body: str
    source_path: Path | None = None
    # 加载时读入的附属指南全文：{相对路径: 内容}
    reference_texts: dict[str, str] = Field(default_factory=dict)
    # 是否为界面上的临时覆盖版本（不写回 .md 文件）
    is_override: bool = False

    @field_validator("skill_id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not _SKILL_ID_PATTERN.match(v):
            raise ValueError(f"skill_id 只能包含小写字母、数字、下划线：{v!r}")
        return v

    @property
    def placeholders(self) -> set[str]:
        """正文中实际出现的变量名。"""
        return set(_VAR_PATTERN.findall(self.body))

    def render(self, **values: object) -> str:
        """把 {{ var }} 替换为实际值。缺失任一变量时报错，避免把占位符发给模型。"""
        missing = sorted(self.placeholders - values.keys())
        if missing:
            raise SkillRenderError(f"Skill {self.skill_id} 缺少变量：{', '.join(missing)}")
        return _VAR_PATTERN.sub(lambda m: str(values[m.group(1)]), self.body)

    @property
    def reference_paths(self) -> list[Path]:
        base = self.source_path.parent if self.source_path else Path(".")
        return [base / r for r in self.reference_texts]

    def reference_context(self, names: list[str] | None = None) -> str:
        """把附属指南拼成追加在指令后面的上下文。names 为文件名（如 base-en.txt），None 表示全部。"""
        parts = []
        for rel, text in self.reference_texts.items():
            if names is None or Path(rel).name in names:
                parts.append(f'<reference_file path="{rel}">\n{text.strip()}\n</reference_file>')
        return "\n\n".join(parts)

    def with_override(self, body: str) -> SkillCard:
        """返回一个使用临时正文的副本，原卡带与磁盘文件不受影响。"""
        return self.model_copy(update={"body": body, "is_override": True})
