"""公共免 Key 在线演示（LAOTUO_PUBLIC_DEMO=1）：样板项目、访客独立项目、过期清理与渲染排队。

- 开机用演示模式生成一套「张家界莓茶」示例素材（主图 4 张 + 详情页 4 屏，本机 PIL 绘制，不调用任何接口），
  存为样板项目 demo_template，并预先收集好动效广告的素材库；
- 每位访客打开页面时，从样板复制一份独立项目（新 project_id），只有自己看得到，互不干扰；
- 访客项目超过保留时间（默认 1 小时）没有操作就整个删除；还在用的项目里，过期的动效成片与单屏预览也删除；
- 动效渲染同一时间只跑 1 条，排队的（含正在渲染的）最多 5 条，满了直接提示稍后再试。

工作区是 <WORKSPACE_DIR>/public_demo/（app/settings.py 里换好），清理只在这里进行，碰不到本机的真实项目。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.engine.motion_engine import EXIT_ALLOWANCE, MotionEngine
from app.engine.planner_engine import PlannerEngine
from app.engine.prompt_compiler import image_skill_for_model
from app.engine.render_engine import RenderEngine
from app.engine.runtime import EngineRuntime
from app.schemas.common import new_id, utc_now
from app.schemas.motion import BEAT_RANGE, MotionAd, MotionScene, beats_for
from app.schemas.project import Project
from app.settings import (
    PUBLIC_MOTION_MAX_SECONDS,
    PUBLIC_OUTPUT_TTL_SECONDS,
    PUBLIC_QUEUE_MAX_SIZE,
    PUBLIC_WORKSPACE_SUBDIR,
)

TEMPLATE_ID = "demo_template"
DEMO_CATEGORY = "zhangjiajie_berry_tea"
DEMO_PLATFORM = "taobao"
# 样板项目的商品信息：只写外观、产地与规格，不写检测数据、价格这类需要真实凭据的内容
DEMO_PRODUCT_INFO = {"grade": "特级芽尖", "frost": "叶面满霜", "origin": "张家界永定区", "harvest_season": "春茶",
                     "spec": "50g × 2 罐"}
# 样板素材：(交付类型, 项目字段, 比例, 张数)
DEMO_PLANS = (("main_image", "main_image_plan", "1:1", 4), ("detail_page", "detail_page_plan", "3:4", 4))
# 同时保留的访客项目上限（每份约 2MB，加上成片也远小于 50GB 磁盘）；超过时删最久没有操作的
MAX_VISITOR_PROJECTS = 200


class PublicDemoError(RuntimeError):
    """样板项目生成失败。"""


def fit_motion(ad: MotionAd, max_seconds: float = PUBLIC_MOTION_MAX_SECONDS) -> str:
    """把动效方案收紧到 max_seconds 秒以内，返回说明（没有超时返回空字符串）。

    第一屏（钩子）和最后一屏（收尾行动）不动；先把中间分镜的拍数逐拍减到「刚好读完上面的字」（每次减余量最多的
    那一屏），还超时就从后往前删中间分镜。拍数仍是整数拍，每一刀照样落在节拍上。
    """
    before, count = ad.total_seconds, len(ad.scenes)
    if before <= max_seconds:
        return ""

    def floor(s: MotionScene) -> int:
        need = max(BEAT_RANGE[s.kind][0], beats_for(s.reading_seconds + EXIT_ALLOWANCE, ad.bpm))
        return min(need, s.beats)

    while ad.total_seconds > max_seconds:
        middle = ad.scenes[1:-1]
        slack = [(s.beats - floor(s), s) for s in middle if s.beats > floor(s)]
        if slack:
            s = max(slack, key=lambda x: x[0])[1]
            ad.update_scene(s.scene_id, beats=s.beats - 1)
        elif middle:
            ad.remove_scene(middle[-1].scene_id)
        else:
            break  # 只剩钩子和收尾：最长也不到 20 秒，不会走到这里
    removed = count - len(ad.scenes)
    return (f"公共在线演示单条动效最长 {max_seconds:g} 秒：已从 {before:g} 秒收紧到 {ad.total_seconds:g} 秒"
            + (f"（删掉中间 {removed} 屏）" if removed else "（中间几屏各少停一两拍）"))


class RenderGate:
    """动效渲染排队：同一时间只渲染 1 条（很吃 CPU），排队的（含正在渲染的）最多 max_size 条。

    只在事件循环线程里调用 enter / leave，不需要额外加锁。
    """

    def __init__(self, max_size: int = PUBLIC_QUEUE_MAX_SIZE):
        self.max_size = max_size
        self.lock = asyncio.Lock()
        self._count = 0

    @property
    def size(self) -> int:
        return self._count

    def enter(self) -> int | None:
        """领一个号：返回前面还有几条；已满时返回 None（不排队）。"""
        if self._count >= self.max_size:
            return None
        self._count += 1
        return self._count - 1

    def leave(self) -> None:
        self._count = max(0, self._count - 1)


class PublicDemo:
    def __init__(self, rt: EngineRuntime, forget: Callable[[str], None] | None = None,
                 ttl: float = PUBLIC_OUTPUT_TTL_SECONDS):
        if rt.settings.workspace_dir.name != PUBLIC_WORKSPACE_SUBDIR:
            # 过期清理会删除整个项目目录：只允许在公共演示专用的工作区里进行
            raise ValueError(f"公共演示的工作区必须是 {PUBLIC_WORKSPACE_SUBDIR}/ 子目录：{rt.settings.workspace_dir}")
        self.rt = rt
        self.ttl = ttl
        self.render_gate = RenderGate()
        self._forget = forget or (lambda _pid: None)
        self._template_lock = threading.Lock()
        self._clean_lock = threading.Lock()
        self._ready = False

    @property
    def template_dir(self) -> Path:
        return self.rt.store.project_dir(TEMPLATE_ID)

    def category_id(self) -> str:
        """与顶栏的默认类目一致：有张家界莓茶就用它，否则用第一个类目。"""
        return DEMO_CATEGORY if DEMO_CATEGORY in self.rt.categories else next(iter(self.rt.categories))

    def platform_id(self) -> str:
        return DEMO_PLATFORM if DEMO_PLATFORM in self.rt.platforms else next(iter(self.rt.platforms))

    # ---------------------------------------------------------------- 样板项目

    def prepare(self) -> None:
        """开机预载：清理过期的访客项目，重新生成样板项目（代码更新后样板跟着更新）。"""
        self.cleanup()
        self.ensure_template(rebuild=True)

    def ensure_template(self, rebuild: bool = False) -> None:
        """样板项目不存在时生成。同步方法：在没有运行事件循环的线程里调用（界面里用 asyncio.to_thread）。"""
        with self._template_lock:
            if rebuild or not self._ready or not (self.template_dir / "project.json").exists():
                shutil.rmtree(self.template_dir, ignore_errors=True)
                asyncio.run(self._build_template())
                self._ready = True

    async def _build_template(self) -> None:
        rt = self.rt
        cat_id, plat_id = self.category_id(), self.platform_id()
        cat = rt.category(cat_id)
        keys = {f.key for f in cat.product_form_fields}
        info = {k: v for k, v in DEMO_PRODUCT_INFO.items() if k in keys}
        project = Project(project_id=TEMPLATE_ID, name=cat.name, product_name=cat.name, category_id=cat_id,
                          platform_id=plat_id, product_info=info)
        rt.store.save(project)
        model = rt.settings.image_models[0]
        renderer = RenderEngine(rt)
        for kind, attr, ratio, count in DEMO_PLANS:
            plan, _ = await PlannerEngine(rt).generate(deliverable_type=kind, category_id=cat_id, platform_id=plat_id,
                                                       aspect_ratio=ratio, count=count, user_creative_idea=None,
                                                       product_info=info)
            plan.confirm_plan()
            setattr(project, attr, plan)
            async for ev in renderer.render_plan(project, plan, model, image_skill_for_model(model)):
                if ev.error:
                    raise PublicDemoError(f"样板素材生成失败：{ev.message}")
        # 动效广告的素材库先收集好：访客打开【动效广告】就能看到示例素材
        await MotionEngine(rt).refresh_assets(project)
        rt.store.save(project)

    def new_visitor_project(self) -> Project:
        """为一位访客从样板复制一份独立项目：素材文件一起复制，路径换成新项目的目录。同步方法（见 ensure_template）。"""
        self.ensure_template()
        self.cleanup()
        store = self.rt.store
        pid = new_id("proj")
        src, dst = self.template_dir, store.project_dir(pid)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("project.json", ".project.*"))
        data = json.loads((src / "project.json").read_text(encoding="utf-8"))
        data = _rebase(data, [(str(src), str(dst)), (src.as_posix(), dst.as_posix())])
        data.update(project_id=pid, created_at=utc_now())
        project = Project.model_validate(data)
        store.save(project)
        return project

    # ---------------------------------------------------------------- 清理

    def cleanup(self, now: float | None = None) -> int:
        """删除超过保留时间没有操作的访客项目（以 project.json 的修改时间为准，每次操作都会保存），
        以及仍在使用的项目里过期的动效成片与单屏预览。样板项目不删。返回删除的项目数。

        已有一次清理在进行时直接返回 0（渲染完成与新访客打开页面可能同时触发）。
        """
        if not self._clean_lock.acquire(blocking=False):
            return 0
        try:
            cutoff = (time.time() if now is None else now) - self.ttl
            removed = 0
            root = self.rt.store.root
            visitors = sorted(((_mtime(d / "project.json", fallback=d), d) for d in root.iterdir()
                               if d.is_dir() and d.name != TEMPLATE_ID) if root.is_dir() else [])
            # 反复刷新页面也会生成新项目：超过上限时先删最久没有操作的，给新访客腾位置
            overflow = max(0, len(visitors) - (MAX_VISITOR_PROJECTS - 1))
            for i, (mtime, d) in enumerate(visitors):
                if i < overflow or mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    self._forget(d.name)
                    removed += 1
                    continue
                motion = d / "motion"
                for out in motion.glob("render_*"):
                    if out.is_dir() and _mtime(out) < cutoff:
                        shutil.rmtree(out, ignore_errors=True)
                for still in motion.glob("stills/*"):
                    if still.is_file() and _mtime(still) < cutoff:
                        still.unlink(missing_ok=True)
            return removed
        finally:
            self._clean_lock.release()


def _mtime(path: Path, fallback: Path | None = None) -> float:
    for p in (path, fallback):
        if p is None:
            continue
        try:
            return p.stat().st_mtime
        except OSError:
            continue
    return 0.0


def _rebase(value: Any, pairs: list[tuple[str, str]]) -> Any:
    """把 JSON 里以样板目录开头的路径换成新项目的目录。"""
    if isinstance(value, dict):
        return {k: _rebase(v, pairs) for k, v in value.items()}
    if isinstance(value, list):
        return [_rebase(v, pairs) for v in value]
    if isinstance(value, str):
        for old, new in pairs:
            if value == old or value.startswith((old + "/", old + "\\")):
                return new + value[len(old):]
    return value
