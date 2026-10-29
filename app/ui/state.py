"""界面层共享状态与表格 <-> 模型转换。

Project 对象只保存在服务端内存（按 project_id 索引）与磁盘，浏览器端 gr.State 只存 project_id 字符串。
三个选项卡并发操作同一个项目时，各自只改自己的模块，并共享同一个 Project 实例，避免互相覆盖。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.engine.runtime import EngineRuntime
from app.schemas.common import MODE_REFERENCE_RANGE, PlanStateError, PlanValidationError
from app.schemas.editable import ReferenceRequirement
from app.schemas.plan import AESTHETIC_STYLE_NAMES, COMPOSITION_MODE_NAMES, ImagePlan, ImageSlot, design_code
from app.schemas.project import Project
from app.schemas.video import StoryboardShot, VideoScript

if TYPE_CHECKING:
    from app.engine.public_demo import PublicDemo

MODE_CHOICES = ["text_to_image", "single_image_edit", "multi_image_fusion"]
AUDIO_CHOICES = ["native_dialogue", "ambient_sfx", "dialogue_and_ambient", "silent"]

# 表格中美学流派 / 构图模式显示中文名，「自动」表示交给 AI 按指南匹配
AUTO_LABEL = "自动"
AESTHETIC_CHOICES = [("🤖 自动匹配（AI 按每张图的作用选择）", "auto"),
                     *((name, code) for code, name in AESTHETIC_STYLE_NAMES.items())]

SLOT_HEADERS = ["序号", "主标题", "副标题", "上图文案", "画面构图描述", "生图模式", "美学流派", "构图模式",
                "参考图角色（逗号分隔）"]
SHOT_HEADERS = [
    "镜头", "时长(秒)", "景别", "运镜", "画面描述", "主体动作", "首帧画面", "声音模式",
    "人物同期声对白", "原生环境音/音效", "首帧生图模式", "参考图角色（逗号分隔）",
]


class UIError(Exception):
    """可直接展示给用户的中文错误。"""


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log_line(msg: str) -> str:
    return f"[{now()}] {msg}"


class Workspace:
    """内存中的项目缓存 + 每个项目一把锁，保证并发落盘安全。"""

    def __init__(self, rt: EngineRuntime):
        self.rt = rt
        self._projects: dict[str, Project] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # 公共免 Key 在线演示（LAOTUO_PUBLIC_DEMO=1）时由 build_app 设置，见 app/engine/public_demo.py
        self.public: PublicDemo | None = None

    def get(self, project_id: str | None) -> Project:
        if not project_id:
            raise UIError("请先在顶部「项目」区新建或加载一个项目")
        if project_id not in self._projects:
            try:
                self._projects[project_id] = self.rt.store.load(project_id)
            except FileNotFoundError as e:
                raise UIError("找不到这个项目，可能已被删除（公共在线演示里超过 1 小时没有操作的项目会自动清理），"
                              "请刷新页面或新建项目") from e
        return self._projects[project_id]

    def forget(self, project_id: str) -> None:
        """项目目录被删除后（公共演示的过期清理）丢掉内存缓存。"""
        self._projects.pop(project_id, None)
        self._locks.pop(project_id, None)

    def put(self, project: Project) -> Project:
        self._projects[project.project_id] = project
        return project

    def save(self, project: Project) -> None:
        self.rt.store.save(project)

    def lock(self, project_id: str) -> asyncio.Lock:
        return self._locks[project_id]

    def project_choices(self) -> list[tuple[str, str]]:
        if self.public is not None:
            return []  # 公共在线演示：访客之间互相看不到项目
        out = []
        for p in self.rt.store.list_projects():
            cat = self.rt.categories.get(p.category_id)
            plat = self.rt.platforms.get(p.platform_id)
            label = f"{p.name}｜{cat.name if cat else p.category_id}｜{plat.name if plat else p.platform_id}"
            out.append((f"{label}｜{p.updated_at:%m-%d %H:%M}", p.project_id))
        return out


# ---------------------------------------------------------------- 表格转换


def _roles_text(refs: list[ReferenceRequirement]) -> str:
    return ", ".join(r.role for r in refs)


def _parse_roles(text: Any, old: list[ReferenceRequirement], role_desc: dict[str, str]) -> list[dict[str, str]]:
    roles = [r.strip() for r in str(text or "").replace("，", ",").replace("、", ",").split(",") if r.strip()]
    old_desc = {r.role: r.description for r in old}
    return [{"role": r, "description": old_desc.get(r) or role_desc.get(r, "")} for r in dict.fromkeys(roles)]


def _rows(table: Any) -> list[list[Any]]:
    """兼容 pandas DataFrame / list[list] 两种表格值。"""
    if table is None:
        return []
    if hasattr(table, "values") and hasattr(table, "columns"):
        return [list(r) for r in table.values.tolist()]
    if isinstance(table, dict) and "data" in table:
        return [list(r) for r in table["data"]]
    return [list(r) for r in table]


def _design_name(code: str, names: dict[str, str]) -> str:
    return names.get(code, AUTO_LABEL)


def _parse_design(value: Any, names: dict[str, str], row_no: Any, label: str) -> str:
    code = design_code(value, names)
    if code is None:
        raise UIError(f"第 {row_no} 行{label}「{value}」无效，可选：{AUTO_LABEL} / {' / '.join(names.values())}")
    return code


def slots_to_rows(plan: ImagePlan | None) -> list[list[Any]]:
    if plan is None:
        return []
    return [
        [i, s.title, s.subtitle, s.copy_text, s.visual_description, s.generation_mode,
         _design_name(s.aesthetic_style, AESTHETIC_STYLE_NAMES),
         _design_name(s.composition_mode, COMPOSITION_MODE_NAMES), _roles_text(s.required_references)]
        for i, s in enumerate(plan.slots, start=1)
    ]


def apply_slot_rows(plan: ImagePlan, table: Any, role_desc: dict[str, str]) -> int:
    """把界面表格的修改写回策划案（只更新有变化的槽位）。返回修改的槽位数。"""
    rows = _rows(table)
    if len(rows) != len(plan.slots):
        raise UIError("表格行数与槽位数不一致：增删槽位请使用下方「新增 / 删除」按钮")
    changed = 0
    for row, slot in zip(rows, plan.slots, strict=True):
        row = [*row, *[""] * (len(SLOT_HEADERS) - len(row))]
        mode = str(row[5] or "").strip()
        if mode not in MODE_CHOICES:
            raise UIError(f"第 {row[0]} 行生图模式「{mode}」无效，可选：{' / '.join(MODE_CHOICES)}")
        new = {
            "title": str(row[1] or "").strip(),
            "subtitle": str(row[2] or "").strip(),
            "copy_text": str(row[3] or "").strip(),
            "visual_description": str(row[4] or "").strip(),
            "generation_mode": mode,
            "aesthetic_style": _parse_design(row[6], AESTHETIC_STYLE_NAMES, row[0], "美学流派"),
            "composition_mode": _parse_design(row[7], COMPOSITION_MODE_NAMES, row[0], "构图模式"),
            "required_references": _parse_roles(row[8], slot.required_references, role_desc),
        }
        old = {
            "title": slot.title, "subtitle": slot.subtitle, "copy_text": slot.copy_text,
            "visual_description": slot.visual_description, "generation_mode": slot.generation_mode,
            "aesthetic_style": slot.aesthetic_style, "composition_mode": slot.composition_mode,
            "required_references": [r.model_dump() for r in slot.required_references],
        }
        if new != old:
            plan.update_slot(slot.slot_id, **new)
            changed += 1
    return changed


def shots_to_rows(script: VideoScript | None) -> list[list[Any]]:
    if script is None:
        return []
    return [
        [i, s.duration_seconds, s.shot_size, s.camera_movement, s.scene_description, s.subject_action,
         s.first_frame_description, s.audio_mode, s.character_dialogue or "", s.ambient_sound_description,
         s.first_frame_generation_mode, _roles_text(s.required_references)]
        for i, s in enumerate(script.shots, start=1)
    ]


def apply_shot_rows(script: VideoScript, table: Any, role_desc: dict[str, str]) -> int:
    rows = _rows(table)
    if len(rows) != len(script.shots):
        raise UIError("表格行数与分镜数不一致：增删分镜请使用下方「新增 / 删除」按钮")
    changed = 0
    for row, shot in zip(rows, script.shots, strict=True):
        row = [*row, *[""] * (len(SHOT_HEADERS) - len(row))]
        audio = str(row[7] or "").strip()
        mode = str(row[10] or "").strip()
        if audio not in AUDIO_CHOICES:
            raise UIError(f"镜头 {row[0]} 声音模式「{audio}」无效，可选：{' / '.join(AUDIO_CHOICES)}")
        if mode not in MODE_CHOICES:
            raise UIError(f"镜头 {row[0]} 首帧生图模式「{mode}」无效，可选：{' / '.join(MODE_CHOICES)}")
        try:
            duration = float(row[1])
        except (TypeError, ValueError) as e:
            raise UIError(f"镜头 {row[0]} 时长必须是数字") from e
        new = {
            "duration_seconds": duration,
            "shot_size": str(row[2] or "").strip(),
            "camera_movement": str(row[3] or "").strip(),
            "scene_description": str(row[4] or "").strip(),
            "subject_action": str(row[5] or "").strip(),
            "first_frame_description": str(row[6] or "").strip(),
            "audio_mode": audio,
            "character_dialogue": str(row[8] or "").strip() or None,
            "ambient_sound_description": str(row[9] or "").strip(),
            "first_frame_generation_mode": mode,
            "required_references": _parse_roles(row[11], shot.required_references, role_desc),
        }
        old = {k: getattr(shot, k) for k in new if k != "required_references"}
        old["required_references"] = [r.model_dump() for r in shot.required_references]
        if new != old:
            try:
                script.update_shot(shot.shot_id, **new)
            except ValueError as e:
                raise UIError(f"镜头 {row[0]} 修改无效：{_first_error(e)}") from e
            changed += 1
    return changed


def _first_error(e: Exception) -> str:
    errors = getattr(e, "errors", None)
    if callable(errors):
        try:
            return "；".join(str(x.get("msg", "")).removeprefix("Value error, ") for x in errors())
        except (TypeError, AttributeError):
            pass
    return str(e)


def friendly(e: Exception) -> str:
    if isinstance(e, PlanValidationError):
        return "❌ 无法确认，请修正以下问题：\n" + "\n".join(f"- {i}" for i in e.issues)
    if isinstance(e, PlanStateError | UIError):
        return f"⚠️ {e}"
    return f"❌ {_first_error(e)}"


def design_hint() -> str:
    return (f"美学流派可填：{AUTO_LABEL} / {' / '.join(AESTHETIC_STYLE_NAMES.values())}；"
            f"构图模式可填：{AUTO_LABEL} / {' / '.join(COMPOSITION_MODE_NAMES.values())}")


def mode_hint() -> str:
    parts = []
    for mode, (lo, hi) in MODE_REFERENCE_RANGE.items():
        n = f"{lo}" if lo == hi else f"{lo}~{hi}"
        parts.append(f"`{mode}` = {n} 张参考图")
    return "生图模式与参考图数量必须一致：" + "，".join(parts)


def new_blank_slot(aspect_ratio: str) -> ImageSlot:
    return ImageSlot(title="新槽位", visual_description="请填写画面描述", generation_mode="text_to_image",
                     aspect_ratio=aspect_ratio)


def new_blank_shot() -> StoryboardShot:
    return StoryboardShot(duration_seconds=3, scene_description="请填写画面", first_frame_description="请填写首帧",
                          audio_mode="ambient_sfx", ambient_sound_description="请填写环境音")
