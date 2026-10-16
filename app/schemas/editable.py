"""策划案 / 分镜剧本共用的「草稿可编辑 → 确认定稿」状态机与参考图清单逻辑。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import (
    MODE_REFERENCE_RANGE,
    EditRecord,
    GenerationMode,
    PlanStateError,
    PlanStatus,
    PlanValidationError,
    new_id,
    utc_now,
)


class ReferenceRequirement(BaseModel):
    """槽位需要的一张参考图（按角色描述，尚未上传）。"""

    role: str
    description: str = ""

    @field_validator("role")
    @classmethod
    def _normalize_role(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("参考图角色不能为空")
        return v


class ReferenceChecklistItem(BaseModel):
    """《所需参考图清单》中的一项：同一角色只上传一次，绑定到所有需要它的槽位。"""

    role: str
    description: str
    slot_ids: list[str]


class ReferenceAsset(BaseModel):
    """用户上传的一张参考图。"""

    asset_id: str = Field(default_factory=lambda: new_id("ref"))
    role: str
    file_path: Path
    uploaded_at: datetime = Field(default_factory=utc_now)


class BindableItem(BaseModel):
    """可绑定参考图的槽位 / 分镜的公共字段。"""

    required_references: list[ReferenceRequirement] = Field(default_factory=list)
    # Stage 2 单槽位独立增删：额外加入 / 排除的参考图 asset_id
    extra_reference_ids: list[str] = Field(default_factory=list)
    excluded_reference_ids: list[str] = Field(default_factory=list)

    @property
    def item_id(self) -> str:  # pragma: no cover - 由子类覆盖
        raise NotImplementedError

    @property
    def mode(self) -> GenerationMode:  # pragma: no cover - 由子类覆盖
        raise NotImplementedError

    def mode_issues(self, label: str) -> list[str]:
        lo, hi = MODE_REFERENCE_RANGE[self.mode]
        n = len(self.required_references)
        issues = []
        if not lo <= n <= hi:
            want = f"{lo}" if lo == hi else f"{lo}~{hi}"
            issues.append(f"{label}：生图模式 {self.mode} 需要 {want} 张参考图，当前登记了 {n} 张")
        roles = [r.role for r in self.required_references]
        if len(roles) != len(set(roles)):
            issues.append(f"{label}：参考图角色重复")
        return issues


class EditablePlanBase(BaseModel):
    """草稿 / 确认状态机。子类通过 ITEMS_FIELD 指定条目列表字段名。"""

    ITEMS_FIELD: ClassVar[str]
    # 编辑后需要清空的子类派生字段（例如首帧确认）在 _on_item_edited 中处理
    LOCKED_FIELDS: ClassVar[frozenset[str]] = frozenset()

    plan_status: PlanStatus = "draft"
    user_creative_idea: str | None = None
    confirmed_at: datetime | None = None
    reference_checklist: list[ReferenceChecklistItem] = Field(default_factory=list)
    reference_assets: list[ReferenceAsset] = Field(default_factory=list)
    edit_log: list[EditRecord] = Field(default_factory=list)

    @field_validator("user_creative_idea")
    @classmethod
    def _blank_idea_is_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    # ---------- 内部工具 ----------

    @property
    def items(self) -> list[Any]:
        return getattr(self, self.ITEMS_FIELD)

    def _log(self, action: str, target_id: str | None = None, **details: Any) -> None:
        self.edit_log.append(EditRecord(action=action, target_id=target_id, details=details))

    def _require_draft(self) -> None:
        if self.plan_status != "draft":
            raise PlanStateError("方案已确认锁定，请先点击「解锁修改」再编辑")

    def require_confirmed(self) -> None:
        """进入传图 / 渲染阶段前的闸门。"""
        if self.plan_status != "confirmed":
            raise PlanStateError("方案尚未确认，请先点击「确认策划方案」")

    def _index_of(self, item_id: str) -> int:
        for i, item in enumerate(self.items):
            if item.item_id == item_id:
                return i
        raise KeyError(f"找不到条目：{item_id}")

    def get_item(self, item_id: str) -> Any:
        return self.items[self._index_of(item_id)]

    def _on_item_edited(self, item: Any, changed: set[str]) -> None:
        """子类钩子：条目被编辑后清理失效的派生数据。"""

    # ---------- 草稿编辑 ----------

    def add_item(self, item: Any, index: int | None = None) -> Any:
        self._require_draft()
        if index is None:
            self.items.append(item)
        else:
            self.items.insert(index, item)
        self._log("add_item", item.item_id, index=index)
        return item

    def remove_item(self, item_id: str) -> None:
        self._require_draft()
        self.items.pop(self._index_of(item_id))
        self._log("remove_item", item_id)

    def move_item(self, item_id: str, new_index: int) -> None:
        self._require_draft()
        if not 0 <= new_index < len(self.items):
            raise IndexError(f"目标位置越界：{new_index}")
        item = self.items.pop(self._index_of(item_id))
        self.items.insert(new_index, item)
        self._log("move_item", item_id, new_index=new_index)

    def update_item(self, item_id: str, /, **changes: Any) -> Any:
        """手工编辑条目字段。通过重新校验构造新对象，保证字段类型正确。"""
        self._require_draft()
        idx = self._index_of(item_id)
        old = self.items[idx]
        forbidden = (changes.keys() & self.LOCKED_FIELDS) | ({"slot_id", "shot_id"} & changes.keys())
        if forbidden:
            raise ValueError(f"以下字段不能手工编辑：{', '.join(sorted(forbidden))}")
        data = old.model_dump()
        data.update(changes)
        new = type(old).model_validate(data)
        self.items[idx] = new
        self._on_item_edited(new, set(changes))
        self._log("update_item", item_id, fields=sorted(changes))
        return new

    def replace_item(self, item_id: str, new_item: Any) -> Any:
        """单独重新生成某一个条目：保留原 ID 与位置。"""
        self._require_draft()
        idx = self._index_of(item_id)
        id_field = "slot_id" if hasattr(new_item, "slot_id") else "shot_id"
        new_item = new_item.model_copy(update={id_field: item_id})
        self.items[idx] = new_item
        self._on_item_edited(new_item, set(type(new_item).model_fields))
        self._log("regenerate_item", item_id)
        return new_item

    def replace_all_items(self, new_items: list[Any], user_creative_idea: str | None = None) -> None:
        """修改想法后「重新生成整套」。"""
        self._require_draft()
        self.user_creative_idea = self._blank_idea_is_none(user_creative_idea)
        setattr(self, self.ITEMS_FIELD, list(new_items))
        self.reference_checklist = []
        self._log("regenerate_all", count=len(new_items))

    # ---------- 确认 / 解锁 ----------

    def validate_plan(self) -> list[str]:
        """返回全部问题；空列表表示可以确认。子类可扩展。"""
        if not self.items:
            return ["方案至少需要 1 个条目"]
        issues: list[str] = []
        for i, item in enumerate(self.items, start=1):
            issues.extend(item.mode_issues(f"第 {i} 项"))
        return issues

    def compute_reference_checklist(self) -> list[ReferenceChecklistItem]:
        """基于当前（最终）条目去重汇总参考图清单，按首次出现顺序排列。"""
        merged: dict[str, ReferenceChecklistItem] = {}
        for item in self.items:
            for req in item.required_references:
                entry = merged.get(req.role)
                if entry is None:
                    merged[req.role] = ReferenceChecklistItem(
                        role=req.role, description=req.description, slot_ids=[item.item_id]
                    )
                    continue
                if item.item_id not in entry.slot_ids:
                    entry.slot_ids.append(item.item_id)
                if req.description and req.description not in entry.description:
                    entry.description = "；".join(filter(None, [entry.description, req.description]))
        return list(merged.values())

    def confirm_plan(self) -> list[ReferenceChecklistItem]:
        """Lock & Confirm：校验通过后锁定，并重新计算《所需参考图清单》。"""
        self._require_draft()
        issues = self.validate_plan()
        if issues:
            raise PlanValidationError(issues)
        self.reference_checklist = self.compute_reference_checklist()
        self.plan_status = "confirmed"
        self.confirmed_at = utc_now()
        self._log("confirm_plan", roles=[c.role for c in self.reference_checklist])
        return self.reference_checklist

    def unlock_plan(self) -> None:
        """解锁退回草稿。已上传的参考图保留，重新确认时按角色自动重新绑定。"""
        self.require_confirmed()
        self.plan_status = "draft"
        self.confirmed_at = None
        self._log("unlock_plan")

    # ---------- Stage 2：按清单传图 ----------

    def upload_reference(self, role: str, file_path: Path | str) -> ReferenceAsset:
        """按角色上传一次，自动绑定到所有需要该角色的条目。"""
        self.require_confirmed()
        if role not in {c.role for c in self.reference_checklist}:
            raise ValueError(f"参考图角色 {role!r} 不在本方案的所需参考图清单中")
        # 同一角色重复上传视为替换
        self.reference_assets = [a for a in self.reference_assets if a.role != role]
        asset = ReferenceAsset(role=role, file_path=Path(file_path))
        self.reference_assets.append(asset)
        self._log("upload_reference", asset.asset_id, role=role)
        return asset

    def add_slot_reference(self, item_id: str, file_path: Path | str, role: str = "custom") -> ReferenceAsset:
        """为单个条目额外上传一张参考图（只绑定到该条目）。"""
        self.require_confirmed()
        item = self.get_item(item_id)
        asset = ReferenceAsset(role=role, file_path=Path(file_path))
        self.reference_assets.append(asset)
        item.extra_reference_ids.append(asset.asset_id)
        self._log("add_slot_reference", item_id, asset_id=asset.asset_id)
        return asset

    def remove_slot_reference(self, item_id: str, asset_id: str) -> None:
        """从单个条目移除一张参考图，不影响其他条目。"""
        self.require_confirmed()
        item = self.get_item(item_id)
        if asset_id in item.extra_reference_ids:
            item.extra_reference_ids.remove(asset_id)
        elif asset_id not in item.excluded_reference_ids:
            item.excluded_reference_ids.append(asset_id)
        self._log("remove_slot_reference", item_id, asset_id=asset_id)

    def references_for(self, item_id: str) -> list[ReferenceAsset]:
        """条目实际绑定的参考图（生图路由以此数量为准）。"""
        item = self.get_item(item_id)
        roles = [r.role for r in item.required_references]
        by_role = {a.role: a for a in self.reference_assets if a.asset_id not in item.extra_reference_ids}
        bound = [by_role[r] for r in roles if r in by_role]
        bound += [a for a in self.reference_assets if a.asset_id in item.extra_reference_ids]
        return [a for a in bound if a.asset_id not in item.excluded_reference_ids]

    def missing_references(self) -> list[ReferenceChecklistItem]:
        uploaded = {a.role for a in self.reference_assets}
        return [c for c in self.reference_checklist if c.role not in uploaded]
