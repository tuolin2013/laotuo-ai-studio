"""项目级操作：新建 / 加载、跨模块复用参考图、定稿导出。"""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.engine.runtime import EngineRuntime
from app.schemas.editable import EditablePlanBase, ReferenceAsset
from app.schemas.project import Project

MODULES = ("main_image_plan", "detail_page_plan", "video_script")
MODULE_LABELS = {"main_image_plan": "主图", "detail_page_plan": "详情页", "video_script": "视频"}


def create_project(rt: EngineRuntime, name: str, category_id: str, platform_id: str,
                   product_info: dict[str, str] | None = None) -> Project:
    project = Project(name=name.strip() or "未命名项目", category_id=category_id, platform_id=platform_id,
                      product_info=dict(product_info or {}))
    rt.store.save(project)
    return project


def all_reference_assets(project: Project, exclude: str | None = None) -> dict[str, ReferenceAsset]:
    """项目内其他模块已上传的参考图，按角色取最新一张。"""
    found: dict[str, ReferenceAsset] = {}
    for attr in MODULES:
        if attr == exclude:
            continue
        plan: EditablePlanBase | None = getattr(project, attr)
        if plan is None:
            continue
        for asset in plan.reference_assets:
            if asset.file_path.exists():
                prev = found.get(asset.role)
                if prev is None or asset.uploaded_at > prev.uploaded_at:
                    found[asset.role] = asset
    return found


def reuse_references(project: Project, target_attr: str) -> list[str]:
    """把其他模块已上传的同角色参考图，一键绑定到目标模块尚缺的清单项。返回复用的角色列表。"""
    plan: EditablePlanBase | None = getattr(project, target_attr)
    if plan is None:
        raise ValueError("该模块还没有策划案")
    plan.require_confirmed()
    pool = all_reference_assets(project, exclude=target_attr)
    reused = []
    for item in plan.missing_references():
        asset = pool.get(item.role)
        if asset is not None:
            plan.upload_reference(item.role, asset.file_path)
            reused.append(item.role)
    return reused


def export_winners(rt: EngineRuntime, project: Project, attr: str) -> Path:
    """把各槽位勾选的定稿图（未勾选则取最新一张）按顺序打包为 zip。"""
    plan = getattr(project, attr)
    if plan is None:
        raise ValueError("该模块还没有策划案")
    out_dir = rt.store.project_dir(project.project_id) / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{attr}_{project.project_id}.zip"
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, slot in enumerate(plan.slots, start=1):
            chosen = [c for c in slot.candidates if c.selected] or slot.candidates[-1:]
            for j, c in enumerate(chosen, start=1):
                if c.file_path.exists():
                    safe_title = "".join(ch for ch in slot.title if ch not in '\\/:*?"<>|')[:20]
                    zf.write(c.file_path, f"{i:02d}_{safe_title}_{j}{c.file_path.suffix}")
                    count += 1
            if slot.compiled_prompt:
                zf.writestr(f"prompts/{i:02d}_{slot.slot_id}.txt", slot.compiled_prompt)
    if count == 0:
        zip_path.unlink(missing_ok=True)
        raise ValueError("还没有可导出的图片，请先生成候选图")
    return zip_path


def winners_in_order(project: Project, attr: str) -> list[Path]:
    """详情页长图序列预览：每屏定稿图（未勾选取最新）。"""
    plan = getattr(project, attr)
    if plan is None:
        return []
    out = []
    for slot in plan.slots:
        chosen = [c for c in slot.candidates if c.selected] or slot.candidates[-1:]
        out.extend(c.file_path for c in chosen[:1] if c.file_path.exists())
    return out
