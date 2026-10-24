"""【动效广告】引擎：收集项目素材 → 动效导演 Skill 编排分镜 → 纠正为合法的 MotionAd → 本机渲染 mp4。

- 素材全部来自项目里已经生成 / 上传的东西：主图与详情页的定稿图（未勾选取最新一张）、详情页拼接长图、
  视频锁定首帧、分镜最新一版视频、参考图，以及在本选项卡另外上传的图片；
- 只有「编排分镜」会调用 LLM（未填 Key 时走演示模式）；渲染、配乐、音效全部在本机完成，不调用任何生图 / 生视频接口；
- 每次渲染都写进新的文件夹，绝不覆盖旧成片。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from app.engine.compiler import SYSTEM_PROMPT, compile_motion_director
from app.engine.compliance import find_violations, sanitize
from app.engine.edit_engine import analyze_media
from app.engine.motion.encode import MotionRenderError, render_motion, render_still
from app.engine.render_engine import RenderEvent
from app.engine.runtime import EngineRuntime
from app.engine.video_cloner import ffmpeg_exe
from app.providers.demo.motion import demo_motion
from app.schemas.config import CategoryConfig, PlatformConfig
from app.schemas.edit import MAX_TITLE_CHARS, visible_len
from app.schemas.motion import (
    BEAT_RANGE,
    CAMERA_LABELS,
    HOOK_MAX_SECONDS,
    MAX_ASSETS,
    MAX_BUTTON_CHARS,
    MAX_HEADLINE_CHARS,
    MAX_ITEM_CHARS,
    MAX_ITEMS,
    MAX_SCENE_IMAGES,
    MAX_SCENES,
    MAX_STAT_CHARS,
    MAX_SUBLINE_CHARS,
    MIN_IMAGES,
    MOTION_TRANSITION_LABELS,
    SCENE_KINDS,
    THEME_LABELS,
    MotionAd,
    MotionAsset,
    MotionRender,
    MotionScene,
    beats_for,
    label_code,
)
from app.schemas.project import Project

DEFAULT_DIRECTOR_SKILL = "default_motion_director"
# 渲染档位：快速预览 540P、无颗粒、CRF 23；成片 1080P、CRF 17
PREVIEW_SCALE = 0.5
FINAL_CRF, PREVIEW_CRF = 17, 23
# 详情页拼接长图：统一宽度、最多几屏
LONG_WIDTH = 1080
LONG_MAX_SCREENS = 10
# 退场转场约 0.3 秒、片尾收回 0.45 秒：阅读时间之外再留这么多（与自检的算法一致）
EXIT_ALLOWANCE = 0.45
MAX_TRANSITION_KINDS = 3
UPLOAD_TYPES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


class MotionError(ValueError):
    """可以直接展示给用户的中文错误。"""


def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        return im.size


def _norm(path: Path) -> str:
    return str(Path(path).resolve()).lower()


def _chosen(slot) -> Path | None:
    chosen = [c for c in slot.candidates if c.selected] or slot.candidates[-1:]
    for c in chosen:
        if c.file_path.exists():
            return c.file_path
    return None


def stitch_long_image(paths: list[Path], out_dir: Path) -> Path | None:
    """把详情页各屏按顺序竖向拼成一张长图（同一组图只拼一次，文件名按内容取哈希）。"""
    paths = [p for p in paths if p.exists()][:LONG_MAX_SCREENS]
    if len(paths) < 2:
        return None
    key = hashlib.sha1("|".join(f"{p}:{p.stat().st_mtime_ns}" for p in paths).encode()).hexdigest()[:10]
    out = out_dir / f"detail_long_{key}.jpg"
    if out.exists():
        return out
    tiles = []
    for p in paths:
        with Image.open(p) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            h = round(im.height * LONG_WIDTH / im.width)
            tiles.append(im.resize((LONG_WIDTH, h), Image.Resampling.LANCZOS))
    canvas = Image.new("RGB", (LONG_WIDTH, sum(t.height for t in tiles)), (255, 255, 255))
    y = 0
    for t in tiles:
        canvas.paste(t, (0, y))
        y += t.height
    canvas.save(out, quality=90)
    return out


def real_product_info(project: Project, cat: CategoryConfig) -> dict[str, str]:
    """商品信息里用户真正填写的内容：表单未改动时保存的是占位示例（如「如：买二送一」），不能当成事实写进广告。"""
    placeholders = {f.key: f.placeholder for f in cat.product_form_fields}
    out = {}
    for k, v in project.product_info.items():
        v = (v or "").strip()
        if v and v != placeholders.get(k) and not v.startswith(("如：", "如:", "按")):
            out[k] = v
    return out


def product_title(project: Project) -> str:
    """商品名：没有单独填写时用项目名（「未命名项目」除外）。"""
    name = (project.product_name or "").strip()
    if not name and project.name != "未命名项目":
        name = project.name.strip()
    return name


# ---------------------------------------------------------------- 纠正 Skill 输出


def _clean(text: Any, limit: int, cat: CategoryConfig, plat: PlatformConfig) -> str:
    out = sanitize(" ".join(str(text or "").split()), cat, plat).strip()
    while visible_len(out) > limit:
        out = out[:-1].rstrip()
    return out


def _list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(v) for v in (value or []) if str(v or "").strip()]


def _int(value: Any, default: int) -> int:
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return default


def assemble_plan(data: dict[str, Any], assets: list[MotionAsset], cat: CategoryConfig, plat: PlatformConfig, *,
                  aspect_ratio: str, target_seconds: int, bpm: int, theme: str, skill_id: str, idea: str | None,
                  previous: MotionAd | None = None) -> MotionAd:
    """把 Skill 输出的动效 JSON 纠正为合法的 MotionAd。

    纠正规则：未知分镜类型丢弃；文案去违禁词并截到字数上限；强调词不在主标题里时清空；
    素材编号只保留存在的，缺素材的分镜自动补一个没用过的素材（补不上就丢弃）；
    拍数限制在该类型的范围内，并至少能读完上面的字；钩子 ≤3 秒排第一，收尾行动排最后；
    叠化等未知转场改为动势推移，色块擦除全片最多一次，转场种类最多 3 种，最后一屏硬切。
    """
    keys = [a.key for a in assets]
    by_key = {a.key: a for a in assets}
    beat = 60 / bpm
    used: set[str] = set()

    def spare(n: int, prefer_long: bool = False) -> list[str]:
        pool = sorted(keys, key=lambda k: (k in used, (by_key[k].shape != "长图") if prefer_long else 0))
        return pool[:n]

    scenes: list[MotionScene] = []
    wiped = False
    for row in [r for r in data.get("scenes") or [] if isinstance(r, dict)][:MAX_SCENES]:
        kind = str(row.get("kind") or "").strip()
        if kind not in SCENE_KINDS:
            continue
        head = _clean(row.get("headline"), MAX_HEADLINE_CHARS, cat, plat)
        accent = _clean(row.get("accent"), MAX_HEADLINE_CHARS, cat, plat)
        item_limit = MAX_BUTTON_CHARS if kind == "cta" else MAX_ITEM_CHARS
        items = [x for x in (_clean(i, item_limit, cat, plat) for i in _list(row.get("items"))) if x]
        items = items[:1] if kind == "cta" else items[:MAX_ITEMS]
        images = [k for k in dict.fromkeys(s.strip().upper() for s in _list(row.get("images"))) if k in by_key]
        need = MIN_IMAGES.get(kind, 0)
        if len(images) < need:
            extra = [k for k in spare(len(keys), prefer_long=kind == "showcase") if k not in images]
            images += extra[:need - len(images)]  # 只补到最低要求的数量
        images = images[:MAX_SCENE_IMAGES]
        if len(images) < need or (kind == "features" and len(items) < 2):
            continue
        trans = label_code(row.get("transition_out"), MOTION_TRANSITION_LABELS) or "push"
        if trans == "wipe":
            trans, wiped = ("push", wiped) if wiped else ("wipe", True)
        fields = {
            "kind": kind, "headline": head, "accent": accent if accent and accent in head else "",
            "subline": _clean(row.get("subline"), MAX_SUBLINE_CHARS, cat, plat), "items": items,
            "stat_value": _clean(row.get("stat_value"), MAX_STAT_CHARS, cat, plat), "images": images,
            "camera": label_code(row.get("camera"), CAMERA_LABELS) or "push_in", "transition_out": trans,
        }
        if kind == "stat" and not fields["stat_value"]:
            continue
        if kind in {"hook", "cta"} and not head:
            continue
        lo, hi = BEAT_RANGE[kind]
        fields["beats"] = min(max(_int(row.get("beats"), lo), lo), hi)
        probe = MotionScene.model_construct(**fields)
        fields["beats"] = min(max(fields["beats"], beats_for(probe.reading_seconds + EXIT_ALLOWANCE, bpm)), hi)
        if kind == "hook":  # 黄金 3 秒
            fields["beats"] = max(lo, min(fields["beats"], math.floor(HOOK_MAX_SECONDS / beat + 1e-6)))
        try:
            scenes.append(MotionScene(**fields))
        except ValueError:
            continue
        used.update(images)

    title = _clean(data.get("title"), MAX_TITLE_CHARS, cat, plat) or f"{cat.name} 动效广告"
    hooks = [s for s in scenes if s.kind == "hook"]
    if hooks:
        scenes.remove(hooks[0])
        scenes.insert(0, hooks[0])
    else:
        hb = max(2, min(math.floor(HOOK_MAX_SECONDS / beat + 1e-6), BEAT_RANGE["hook"][1]))
        scenes.insert(0, MotionScene(kind="hook", beats=hb, headline=_clean(title, 12, cat, plat) or cat.name,
                                     transition_out="zoom"))
    ctas = [s for s in scenes if s.kind == "cta"]
    if ctas:
        scenes.remove(ctas[-1])
        scenes.append(ctas[-1])
    else:
        scenes.append(MotionScene(kind="cta", beats=max(BEAT_RANGE["cta"][0], beats_for(3.0 + EXIT_ALLOWANCE, bpm)),
                                  headline="喜欢就带回家", accent="带回家", items=["立即下单"], transition_out="cut"))
    # 转场种类最多 3 种（多出来的改成动势推移），最后一屏硬切
    kinds: list[str] = []
    for i, s in enumerate(scenes):
        t = "cut" if i == len(scenes) - 1 else s.transition_out
        if t != "cut" and t not in kinds:
            if len(kinds) >= MAX_TRANSITION_KINDS:
                t = "push" if "push" in kinds else kinds[0]
            else:
                kinds.append(t)
        if t != s.transition_out:
            scenes[i] = s.model_copy(update={"transition_out": t})
    scenes = scenes[:MAX_SCENES]

    ad = MotionAd.sized(aspect_ratio, title=title, bpm=bpm, theme=theme, target_seconds=target_seconds,
                        assets=list(assets), director_skill_id=skill_id,
                        notes=str(data.get("notes") or "").strip()[:300], user_motion_idea=idea or None)
    ad.replace_scenes(scenes)
    if previous is not None:  # 重新编排时沿用成片选项与渲染记录
        ad.music, ad.sfx, ad.grain = previous.music, previous.sfx, previous.grain
        ad.renders = list(previous.renders)
    return ad


# ---------------------------------------------------------------- 引擎


class MotionEngine:
    def __init__(self, rt: EngineRuntime):
        self.rt = rt

    def motion_dir(self, project: Project, sub_id: str | None = None) -> Path:
        return self.rt.store.asset_dir(project.project_id, "motion", sub_id)

    def save(self, project: Project) -> None:
        self.rt.store.save(project)

    # ---------------------------------------------------------------- 素材

    async def collect_assets(self, project: Project, previous: MotionAd | None = None) -> list[MotionAsset]:
        """按「主图 → 视频首帧 → 详情页 → 详情页长图 → 分镜视频 → 参考图 → 上传」收集，最多 40 个，同一文件只算一次。"""
        found: list[tuple[str, str, Path, str]] = []  # (source, label, path, kind)
        if project.main_image_plan:
            for i, slot in enumerate(project.main_image_plan.slots, 1):
                if p := _chosen(slot):
                    found.append(("main_image", f"主图 {i}：{slot.title}", p, "image"))
        script = project.video_script
        if script:
            for i, shot in enumerate(script.shots, 1):
                frame = shot.confirmed_first_frame
                if frame and frame.file_path.exists():
                    found.append(("first_frame", f"镜头 {i} 首帧：{shot.scene_description[:16]}", frame.file_path,
                                  "image"))
        detail: list[Path] = []
        if project.detail_page_plan:
            for i, slot in enumerate(project.detail_page_plan.slots, 1):
                if p := _chosen(slot):
                    detail.append(p)
                    found.append(("detail_page", f"详情页第 {i} 屏：{slot.title}", p, "image"))
        if len(detail) >= 2:
            long = await asyncio.to_thread(stitch_long_image, detail, self.motion_dir(project))
            if long:
                label = f"详情页长图（{min(len(detail), LONG_MAX_SCREENS)} 屏拼接）"
                found.append(("detail_long", label, long, "image"))
        if script:
            for i, shot in enumerate(script.shots, 1):
                videos = shot.playable_videos
                if videos:
                    found.append(("shot_video", f"镜头 {i} 视频：{shot.scene_description[:16]}", videos[-1].file_path,
                                  "video"))
        for attr in ("main_image_plan", "detail_page_plan", "video_script"):
            plan = getattr(project, attr)
            cat = self.rt.categories.get(project.category_id)
            for ref in plan.reference_assets if plan else []:
                if ref.file_path.exists():
                    desc = cat.reference_image_roles.get(ref.role, "") if cat else ""
                    found.append(("reference", f"参考图 {ref.role}：{desc[:16]}", ref.file_path, "image"))
        for a in previous.assets if previous else []:
            if a.source in {"upload", "screen"} and a.exists:
                found.append((a.source, a.label, a.path, a.kind))

        seen: set[str] = set()
        assets: list[MotionAsset] = []
        for source, label, path, kind in found:
            key = _norm(path)
            if key in seen or len(assets) >= MAX_ASSETS:
                continue
            seen.add(key)
            try:
                if kind == "video":
                    info = await analyze_media(path)
                    w, h, dur = info.width, info.height, info.duration
                else:
                    (w, h), dur = await asyncio.to_thread(_image_size, path), 0.0
            except (OSError, ValueError, RuntimeError):
                continue  # 损坏的素材跳过，不影响其他素材
            assets.append(MotionAsset(key=f"A{len(assets) + 1}", kind=kind, path=Path(path), label=label[:40],
                                      source=source, width=w, height=h, duration_seconds=dur))
        return assets

    async def refresh_assets(self, project: Project, aspect_ratio: str = "9:16") -> tuple[MotionAd, int]:
        """重新收集项目素材并合并进动效方案：已有素材保持原编号（分镜引用不变），新素材追加编号。

        返回 (方案, 新增数)。
        """
        ad = project.motion_ad or MotionAd.sized(aspect_ratio)
        found = await self.collect_assets(project, ad)
        known = {_norm(a.path) for a in ad.assets}
        added = 0
        for a in found:
            if _norm(a.path) in known or len(ad.assets) >= MAX_ASSETS:
                continue
            ad.add_asset(a.path, kind=a.kind, label=a.label, source=a.source, width=a.width, height=a.height,
                         duration_seconds=a.duration_seconds)
            added += 1
        project.motion_ad = ad
        return ad, added

    async def add_upload(self, project: Project, ad: MotionAd, src: Path, as_screen: bool = False) -> MotionAsset:
        """把上传的图片加进素材库（复制进项目 motion/ 目录）。"""
        if src.suffix.lower() not in UPLOAD_TYPES:
            raise MotionError(f"只支持 {' / '.join(UPLOAD_TYPES)} 格式的图片")
        stamp = datetime.now().strftime("%m%d%H%M%S%f")
        saved = await asyncio.to_thread(self.rt.store.import_file, project.project_id, src, "motion",
                                        dest_name=f"upload_{stamp}{src.suffix.lower()}")
        try:
            w, h = await asyncio.to_thread(_image_size, saved)
        except OSError as e:
            saved.unlink(missing_ok=True)
            raise MotionError(f"无法读取图片 {src.name}") from e
        return ad.add_asset(saved, kind="image", label=f"{'界面截图' if as_screen else '上传'}：{src.stem[:20]}",
                            source="screen" if as_screen else "upload", width=w, height=h)

    # ---------------------------------------------------------------- 编排

    async def generate(self, project: Project, *, aspect_ratio: str = "9:16", target_seconds: int = 20,
                       bpm: int = 100, theme: str = "keynote_dark", skill_id: str | None = None,
                       idea: str | None = None) -> tuple[MotionAd, str]:
        skill_id = skill_id or DEFAULT_DIRECTOR_SKILL
        if theme not in THEME_LABELS:
            raise MotionError(f"未知的画面主题：{theme}")
        cat, plat = self.rt.category(project.category_id), self.rt.platform(project.platform_id)
        previous = project.motion_ad
        if previous is not None:  # 已有方案：沿用素材编号（新素材追加），只把文件还在的素材交给 Skill
            merged, _ = await self.refresh_assets(project, aspect_ratio)
            assets = [a for a in merged.assets if a.exists]
        else:
            assets = await self.collect_assets(project)
        if not assets:
            raise MotionError("项目里还没有可用的素材：先在【主图】【详情页】或【视频】选项卡生成图片，"
                              "或在本页上传几张商品图")
        payload = [a.payload() for a in assets]
        info = real_product_info(project, cat)
        idea = (idea or "").strip() or None
        if self.rt.is_live("llm"):
            skill = self.rt.skills.get("motion_director", skill_id)
            prompt = compile_motion_director(
                skill, aspect_ratio=aspect_ratio, target_seconds=target_seconds, bpm=bpm,
                theme_label=THEME_LABELS[theme], assets=payload, category=cat, platform=plat, product_info=info,
                user_motion_idea=idea)
            data = await self.rt.llm().complete_json(SYSTEM_PROMPT, prompt)
            mode = "真实 LLM"
        else:
            await asyncio.sleep(self.rt.demo_delay)
            data = demo_motion(payload, cat, plat, info, target_seconds, bpm, idea, product_title(project))
            mode = "演示模式"
        ad = assemble_plan(data, assets, cat, plat, aspect_ratio=aspect_ratio, target_seconds=target_seconds,
                           bpm=bpm, theme=theme, skill_id=skill_id, idea=idea, previous=previous)
        ad.planner_mode = mode
        return ad, mode

    # ---------------------------------------------------------------- 渲染

    async def render(self, project: Project, ad: MotionAd, quality: str = "final") -> AsyncIterator[RenderEvent]:
        """渲染整条动效广告，逐条产出进度事件；最后一条事件 done=True 并带 video_path。

        渲染用的是方案快照：渲染过程中继续改表格，不影响本次结果。
        """
        ad.require_renderable()
        ffmpeg = ffmpeg_exe()
        if not ffmpeg:
            raise MotionError("缺少 ffmpeg：请运行 pip install imageio-ffmpeg")
        snapshot = ad.model_copy(deep=True)
        stamp = datetime.now().strftime("%m%d_%H%M%S")
        out_dir = self.motion_dir(project, f"render_{stamp}_{quality}")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str] = asyncio.Queue()

        def say(msg: str) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, msg)

        preview = quality == "preview"
        task = asyncio.create_task(asyncio.to_thread(
            render_motion, snapshot, out_dir, ffmpeg, scale=PREVIEW_SCALE if preview else 1.0,
            crf=PREVIEW_CRF if preview else FINAL_CRF, progress=say, stem=f"动效广告_{stamp}"))
        while not task.done() or not queue.empty():
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield RenderEvent(getter.result())
            else:
                getter.cancel()
        try:
            res = task.result()
        except (MotionRenderError, OSError, ValueError) as e:
            raise MotionError(f"渲染失败：{e}") from e
        render = MotionRender(video_path=res.video_path, contact_sheet_path=res.contact_sheet_path,
                              report_md=res.review.markdown(), quality="preview" if preview else "final",
                              width=res.width, height=res.height, fps=snapshot.fps, seconds=round(res.seconds, 2),
                              warnings=res.review.warnings)
        # 渲染期间重新编排过方案时，记录挂到当前方案上
        (project.motion_ad or ad).add_render(render)
        self.save(project)
        yield RenderEvent(f"🎉 {'快速预览' if preview else '1080P 成片'}完成：{res.width}×{res.height}，"
                          f"{res.seconds:.1f} 秒", video_path=res.video_path, done=True)

    async def still(self, project: Project, ad: MotionAd, scene_index: int) -> Path:
        """某一屏落定后的单帧预览（半分辨率），表格微调时快速看版式。"""
        ad.require_renderable()
        out = self.motion_dir(project, "stills") / f"still_{datetime.now():%H%M%S%f}.png"
        return await asyncio.to_thread(render_still, ad.model_copy(deep=True), out, scene_index, ffmpeg_exe())


def default_scene(kind: str, ad: MotionAd) -> MotionScene:
    """界面「插入分镜」用的占位分镜：能通过校验，文案写明「请填写」，提醒用户改成真实内容（不填假数字）。"""
    if kind not in SCENE_KINDS:
        raise MotionError(f"未知的分镜类型：{kind}")
    ready = [a for a in ad.assets if a.exists]
    long = [a.key for a in ready if a.shape == "长图"]
    keys = [a.key for a in ready]
    need = MIN_IMAGES.get(kind, 0)
    if len(keys) < need:
        raise MotionError(f"素材不够：这类分镜至少需要 {need} 个素材，请先在 Step 2 收集或上传素材")
    fields: dict[str, Any] = {"kind": kind, "headline": "请填写主标题", "camera": "push_in", "transition_out": "push"}
    if kind == "features":
        fields["items"] = ["请填写卖点一", "请填写卖点二"]
    elif kind == "stat":
        fields.update(headline="请填写数据说明", stat_value="0", subline="请填写出处 / 标准")
    elif kind == "cta":
        fields["items"] = ["立即下单"]
    if kind == "showcase":
        fields["images"] = (long or keys)[:1]
    elif kind == "gallery":
        fields["images"] = keys[:4]
    elif need:
        fields["images"] = keys[:1]
    lo, hi = BEAT_RANGE[kind]
    probe = MotionScene.model_construct(**fields)
    fields["beats"] = min(max(lo, beats_for(probe.reading_seconds + EXIT_ALLOWANCE, ad.bpm)), hi)
    return MotionScene(**fields)


def preflight(ad: MotionAd, cat: CategoryConfig, plat: PlatformConfig) -> list[str]:
    """渲染前的快速检查（不渲染）：阅读时间、总时长、素材文件、违禁词。返回提醒列表。"""
    tips = []
    for sp in ad.timeline():
        s = sp.scene
        if sp.seconds - EXIT_ALLOWANCE + 1e-6 < s.reading_seconds:
            need = beats_for(s.reading_seconds + EXIT_ALLOWANCE, ad.bpm)
            tips.append(f"第 {sp.index + 1} 屏（{s.label}）文字偏多：{sp.seconds:.1f} 秒读不完，"
                        f"建议至少 {need} 拍或删减文字")
    total, target = ad.total_seconds, ad.target_seconds
    if ad.scenes and not 0.75 * target <= total <= 1.25 * target:
        tips.append(f"总时长 {total:g} 秒，与目标 {target} 秒相差较多：可增减拍数或分镜")
    keys = {a.key for a in ad.assets}
    missing = sorted({k for s in ad.scenes for k in s.images if k in keys and not ad.asset(k).exists})
    if missing:
        tips.append(f"素材 {'、'.join(missing)} 的文件已不存在，请换一个素材或重新生成方案")
    texts = " ".join(t for s in ad.scenes for t in (s.headline, s.subline, s.stat_value, *s.items))
    hits = find_violations(texts, cat, plat)
    if hits:
        tips.append(f"命中违禁词 {'、'.join(hits)}，请修改文案")
    return tips


def describe_scene(i: int, s: MotionScene, seconds: float) -> str:
    parts = [f"{i}. {s.label} {s.beats} 拍（{seconds:.1f} 秒）"]
    if s.headline:
        parts.append(f"「{s.headline}」")
    if s.stat_value:
        parts.append(f"· {s.stat_value}")
    if s.items:
        parts.append(f"· {' / '.join(s.items)}")
    if s.images:
        parts.append(f"· 素材 {'、'.join(s.images)}")
    parts.append(f"· {CAMERA_LABELS[s.camera]} · → {MOTION_TRANSITION_LABELS[s.transition_out]}")
    return " ".join(parts)

