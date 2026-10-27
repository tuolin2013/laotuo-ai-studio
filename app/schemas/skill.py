"""插卡式 Skill 卡带模型。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

SkillSlot = Literal[
    "concept_writer", "story_scriptwriter", "ecommerce_planner", "video_scriptwriter", "image_prompt", "video_prompt",
    "video_editor", "voice_writer", "motion_director",
]
SKILL_SLOTS: tuple[SkillSlot, ...] = (
    "concept_writer",
    "story_scriptwriter",
    "ecommerce_planner",
    "video_scriptwriter",
    "image_prompt",
    "video_prompt",
    "video_editor",
    "voice_writer",
    "motion_director",
)

_VAR_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_SKILL_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")
# 本地 Pro 卡带包（config/skills_pro/）中的卡带在界面上的名称前缀
PRO_BADGE = "[👑 Pro]"


class SkillRenderError(ValueError):
    """渲染 Skill 时缺少变量。"""


class SkillBlock(BaseModel):
    """卡带正文中的一个区块：`# 标题 {#block_id 标记...}` 到下一个区块标题之间的内容。

    block_id 为 `_preamble` 时表示第一个区块标题之前的正文（渲染时不输出标题）。
    """

    block_id: str
    title: str = ""
    content: str = ""
    # final：子卡带不能替换 / 删除（仍可 append / prepend 扩展）；abstract：子卡带必须实现
    final: bool = False
    abstract: bool = False
    # 该区块的来源，如 "default_planner" 或 "default_planner + laotuo_xxx(append)"，供 --resolve 查看
    origin: str = ""


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
    # 适用类目 category_id 列表；为空表示通用卡带（适用所有类目）。类目专属卡带只在对应类目下出现并优先选中
    categories: list[str] = Field(default_factory=list)
    source_url: str = ""
    # 继承：父卡带的 skill_id（同一卡槽）。子卡带获得父卡带全部区块，可按区块 id 替换 / 追加 / 删除
    extends: str | None = None
    # 抽象卡带只供继承，不出现在界面下拉框、不能直接运行
    abstract: bool = False
    slot: SkillSlot
    body: str
    source_path: Path | None = None
    # 继承解析后的区块列表与继承链（[根卡带, ..., 本卡带]），由加载器填充
    blocks: list[SkillBlock] = Field(default_factory=list)
    inheritance_chain: list[str] = Field(default_factory=list)
    # 加载时读入的附属指南全文：{相对路径: 内容}
    reference_texts: dict[str, str] = Field(default_factory=dict)
    # 附属指南的实际文件（Pro 卡带可复用开源卡槽 references/ 下的同名指南，所以不一定在卡带同目录）
    reference_files: list[Path] = Field(default_factory=list)
    # 是否来自本地 Pro 卡带包（config/skills_pro/），由加载器按所在目录设置
    is_pro: bool = False
    # 是否为界面上的临时覆盖版本（不写回 .md 文件）
    is_override: bool = False

    @field_validator("skill_id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not _SKILL_ID_PATTERN.match(v):
            raise ValueError(f"skill_id 只能包含小写字母、数字、下划线：{v!r}")
        return v

    @field_validator("extends")
    @classmethod
    def _check_extends(cls, v: str | None) -> str | None:
        if v is not None and not _SKILL_ID_PATTERN.match(v):
            raise ValueError(f"extends 必须是合法的 skill_id：{v!r}")
        return v

    def applies_to(self, category_id: str | None) -> bool:
        return not self.categories or category_id is None or category_id in self.categories

    @property
    def display_name(self) -> str:
        """界面显示名：Pro 卡带在名称前标记「[👑 Pro]」。"""
        return f"{PRO_BADGE} {self.name}" if self.is_pro else self.name

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
        return list(self.reference_files)

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
