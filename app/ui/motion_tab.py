"""【动效广告】选项卡：零成本代码动效广告（Motion Graphics）。

三步：① 动效方案（AI 编排分镜：结构、文案、素材、拍数、镜头、转场，表格逐屏微调）→ ② 素材（自动收集本项目的
主图、详情页、视频首帧、分镜视频与参考图，也可另外上传）→ ③ 渲染（快速预览 540P / 1080P 成片 + 联系表与自检报告）。

只有「编排」会调用 LLM（未填 Key 时走演示模式）；画面、配乐、音效全部由本机代码渲染，不调用任何生图 / 生视频接口。
长任务都是 async 生成器，使用独立 concurrency_id，与其他选项卡互不阻塞。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import gradio as gr

from app.engine.motion_engine import MotionEngine, MotionError, default_scene, describe_scene, preflight
from app.schemas.edit import OUTPUT_SIZES
from app.schemas.motion import (
    BPM_RANGE,
    CAMERA_LABELS,
    DEFAULT_BPM,
    DEFAULT_THEME,
    MOTION_TRANSITION_LABELS,
    SCENE_LABELS,
    SOURCE_LABELS,
    TARGET_RANGE,
    THEME_LABELS,
    MotionAd,
    label_code,
)
from app.schemas.project import Project
from app.ui.header import Header, require_project
from app.ui.state import UIError, Workspace, _rows, friendly, log_line
from app.ui.theme import step_header

SCENE_HEADERS = ["屏", "类型", "拍数", "秒数", "主标题", "强调词", "副标题", "条目（｜分隔）", "数字 / 胶囊", "素材",
                 "镜头", "转场到下一屏"]
ASPECT_CHOICES = [("9:16 竖屏（抖音 / 视频号 / 小红书）", "9:16"), ("16:9 横屏（发布会 / B 站 / 详情页视频）", "16:9"),
                  ("1:1 方形（主图视频）", "1:1"), ("3:4 竖版（淘宝主图视频）", "3:4")]
THEME_CHOICES = [(v, k) for k, v in THEME_LABELS.items()]
KIND_CHOICES = [(v, k) for k, v in SCENE_LABELS.items()]
# 刷新输出的顺序（运行日志之后）；端到端测试按名字取位置
REFRESH_KEYS = ["status", "table", "scene_pick", "assets", "video", "sheet", "report", "files"]

GUIDE_MD = """
**动效引擎自动执行的法则**（整理自 LottieFiles、HeyGen HyperFrames、Remotion 社区等开源动效 Skill，
全文见 `config/skills/motion_director/references/motion_design_principles.md`）

- **卡点**：每屏都是整数拍，每一刀都落在节拍上；钩子之后的第一屏是「起势点」，配乐在这里进鼓点
- **缓动**：入场快进慢停、退场慢起快走，没有线性运动；回弹整支片子只用一次（收尾的价格胶囊）
- **文字**：主标题逐字遮罩上升，落定后完全静止供阅读（每秒最多 8 个字）；每屏只强调一个词
- **转场**：动势推移 / 穿梭缩放 / 色块擦除 / 卡点硬切，下一屏接住上一屏的速度；**不用叠化**
- **质感**：缓慢漂移的柔光背景、暗角、胶片颗粒、圆角卡片投影；竖屏文字避开平台按钮区
- **声音**：本机合成配乐与音效（呼啸声峰值对准剪辑点、一次起势接重击），响度统一到 -14 LUFS

**表格怎么改**：「类型」可填 钩子大字 / 商品主视觉 / 卖点清单 / 数据冲击 / 图集 / 长图滚动 / 收尾行动；
「素材」填 Step 2 里的编号（如 `A1, A3`）；「条目」用 ｜ 分隔；「镜头」可填 缓推 / 缓拉 / 横移 / 静止；
「转场」可填 动势推移 / 穿梭缩放 / 色块擦除 / 卡点硬切。改「屏」的数字可以调换顺序。
"""


# ---------------------------------------------------------------- 表格 <-> 模型


def scenes_to_rows(ad: MotionAd | None) -> list[list[Any]]:
    if ad is None:
        return []
    rows = []
    for sp in ad.timeline():
        s = sp.scene
        rows.append([sp.index + 1, SCENE_LABELS[s.kind], s.beats, round(sp.seconds, 2), s.headline, s.accent,
                     s.subline, "｜".join(s.items), s.stat_value, ", ".join(s.images), CAMERA_LABELS[s.camera],
                     MOTION_TRANSITION_LABELS[s.transition_out]])
    return rows


def _code(value: Any, labels: dict[str, str], what: str, row: str) -> str:
    code = label_code(value, labels)
    if code is None:
        raise UIError(f"{row}「{what}」填写「{value}」无效，可选：{' / '.join(labels.values())}")
    return code


def apply_scene_rows(ad: MotionAd, table: Any) -> int:
    """把表格修改写回动效方案（按行对应分镜），再按「屏」列重新排序。返回有变化的分镜数。"""
    rows = _rows(table)
    if not ad.scenes:
        return 0  # 还没有方案时表格可能带一行空白占位
    if len(rows) != len(ad.scenes):
        raise UIError("表格行数与分镜数不一致：增删分镜请使用表格下方的按钮")
    changed, order = 0, []
    for pos, (raw, scene) in enumerate(zip(rows, list(ad.scenes), strict=True)):
        row = [*raw, *[""] * (len(SCENE_HEADERS) - len(raw))]
        label = f"第 {pos + 1} 屏"
        try:
            beats = round(float(row[2]))
        except (TypeError, ValueError) as e:
            raise UIError(f"{label}「拍数」必须是数字") from e
        new = {
            "kind": _code(row[1], SCENE_LABELS, "类型", label),
            "beats": beats,
            "headline": " ".join(str(row[4] or "").split()),
            "accent": " ".join(str(row[5] or "").split()),
            "subline": " ".join(str(row[6] or "").split()),
            "items": [s.strip() for s in re.split(r"[|｜\n]", str(row[7] or "")) if s.strip()],
            "stat_value": " ".join(str(row[8] or "").split()),
            "images": list(dict.fromkeys(s.upper() for s in re.split(r"[,，、|｜\s]+", str(row[9] or "")) if s)),
            "camera": _code(row[10], CAMERA_LABELS, "镜头", label),
            "transition_out": _code(row[11], MOTION_TRANSITION_LABELS, "转场", label),
        }
        if {k: getattr(scene, k) for k in new} != new:
            try:
                ad.update_scene(scene.scene_id, **new)
            except ValueError as e:
                raise UIError(f"{label}修改无效：{friendly(e).removeprefix('❌ ')}") from e
            changed += 1
        try:
            rank = float(row[0])
        except (TypeError, ValueError):
            rank = pos + 1
        order.append((rank, pos, scene.scene_id))
    new_order = [sid for _, _, sid in sorted(order)]
    if new_order != [s.scene_id for s in ad.scenes]:
        ad.reorder(new_order)
        changed += 1
    return changed


# ---------------------------------------------------------------- 状态文案


def status_md(p: Project | None, rt) -> str:
    if p is None:
        return "先在顶部新建或加载一个项目"
    ad = p.motion_ad
    if ad is None or not ad.scenes:
        return ("还没有动效方案：选好比例、时长和节拍，点「🎬 AI 编排动效方案」。素材会自动从本项目的主图、详情页、"
                "视频首帧和分镜视频里收集；项目里还没有图片时，先在 Step 2 上传几张商品图")
    lines = [f"🎬 **{ad.title or '动效广告'}**　{len(ad.scenes)} 屏　｜　**{ad.total_seconds:g} 秒**"
             f"（目标 {ad.target_seconds} 秒）　｜　{ad.aspect_ratio} · {ad.bpm} BPM · {THEME_LABELS[ad.theme]}"
             + (f"　｜　{ad.planner_mode}" if ad.planner_mode else "")]
    if ad.notes:
        lines.append(f"💬 {ad.notes}")
    tips = preflight(ad, rt.category(p.category_id), rt.platform(p.platform_id))
    if tips:
        lines.append("**渲染前提醒**\n" + "\n".join(f"- ⚠️ {t}" for t in tips))
    lines.append("在表格里改文案、拍数、素材、镜头和转场，改完点「💾 保存修改」；比例、节拍、画面主题改了不用重新编排，"
                 "保存或渲染时直接生效。")
    return "\n\n".join(lines)


def asset_gallery(ad: MotionAd | None) -> list[tuple[str, str]]:
    if ad is None:
        return []
    return [(str(a.path), f"{a.key} · {SOURCE_LABELS[a.source]} · {a.label}") for a in ad.assets if a.exists]


# ---------------------------------------------------------------- 选项卡


def build_motion_tab(ws: Workspace, header: Header) -> None:
    rt = ws.rt
    engine = MotionEngine(rt)

    with gr.Row(elem_classes=["workbench"]):
        with gr.Column(scale=7):
            # ========================================================== Step 1：动效方案
            with gr.Column(elem_classes=["step-card"]):
                step_header(1, "动效方案：AI 编排分镜（结构 · 文案 · 素材 · 节拍 · 转场）")
                gr.Markdown("用项目里已有的主图、详情页、视频首帧和分镜视频，做一条按节拍剪辑的动效广告。"
                            "**画面、配乐、音效全部由本机代码渲染，不调用任何生图 / 生视频接口，不消耗画图额度。**")
                with gr.Row():
                    aspect = gr.Dropdown(ASPECT_CHOICES, value="9:16", label="画面比例")
                    theme = gr.Dropdown(THEME_CHOICES, value=DEFAULT_THEME, label="画面主题",
                                        info="发布会暗场，或沿用主图 / 详情页的 5 大美学流派")
                with gr.Row():
                    target = gr.Slider(*TARGET_RANGE, value=20, step=1, label="目标时长（秒）")
                    bpm = gr.Slider(*BPM_RANGE, value=DEFAULT_BPM, step=1, label="节拍速度（BPM）",
                                    info="72~88 沉稳高级 · 90~110 从容利落 · 112~128 促销感")
                with gr.Row():
                    idea = gr.Textbox(label="✨ 我的动效想法（选填）", lines=1, scale=4,
                                      placeholder="例如：开头用「饭后油腻？」抓人；重点讲检测报告；结尾写买 2 发 3")
                    plan_btn = gr.Button("🎬 AI 编排动效方案", variant="primary", scale=1)
                status = gr.Markdown(elem_classes=["status-note"])
                table = gr.Dataframe(headers=SCENE_HEADERS, interactive=True, wrap=True, static_columns=[3],
                                     datatype=["number", "str", "number", "number", "str", "str", "str", "str", "str",
                                               "str", "str", "str"],
                                     column_widths=[56, 104, 64, 64, 180, 96, 200, 220, 110, 110, 72, 104],
                                     label="动效分镜（可编辑：每屏的拍数、文案、素材编号、镜头与转场）",
                                     elem_classes=["plan-table"])
                with gr.Row():
                    scene_pick = gr.Dropdown(label="选中分镜", scale=3)
                    up_btn = gr.Button("⬆️ 上移", scale=1, min_width=80)
                    down_btn = gr.Button("⬇️ 下移", scale=1, min_width=80)
                    del_btn = gr.Button("🗑️ 删除", scale=1, min_width=80)
                with gr.Row():
                    insert_kind = gr.Dropdown(KIND_CHOICES, value="features", label="插入哪类分镜", scale=2)
                    add_btn = gr.Button("➕ 在选中分镜后面插入", scale=2)
                    save_btn = gr.Button("💾 保存修改", scale=1)
                with gr.Row():
                    still_btn = gr.Button("🖼️ 预览选中这一屏（落定后的画面，几秒出图）")
                still_img = gr.Image(label="单屏预览（半分辨率）", type="filepath", interactive=False, height=360,
                                     visible=False)
                with gr.Accordion("💡 动效法则与表格写法", open=False):
                    gr.Markdown(GUIDE_MD)

            # ========================================================== Step 2：素材
            with gr.Column(elem_classes=["step-card"]):
                step_header(2, "素材：自动收集本项目素材 / 另外上传")
                gr.Markdown("编排时自动收集：主图与详情页的定稿图（未勾选取最新一张）、详情页拼接长图、视频锁定首帧、"
                            "分镜最新一版视频、参考图。表格「素材」列填这里的编号。")
                assets = gr.Gallery(label="素材库（编号 · 来源 · 说明）", columns=5, height=300, object_fit="contain")
                with gr.Row():
                    upload = gr.File(label="上传图片（商品图、场景图、界面截图；可多选）", file_count="multiple",
                                     file_types=["image"], type="filepath", height=110, scale=3)
                    with gr.Column(scale=2):
                        as_screen = gr.Checkbox(label="作为界面截图（放进浏览器外框里展示）")
                        collect_btn = gr.Button("🔄 从项目重新收集素材")

            # ========================================================== Step 3：渲染
            with gr.Column(elem_classes=["step-card"]):
                step_header(3, "渲染：快速预览 / 1080P 成片 · 联系表与自检报告")
                with gr.Row():
                    music = gr.Checkbox(value=True, label="配乐（本机合成，按节拍与主题调式生成）")
                    sfx = gr.Checkbox(value=True, label="音效（转场呼啸、起势重击、逐条提示音）")
                    grain = gr.Checkbox(value=True, label="胶片颗粒（成片）")
                with gr.Row():
                    preview_btn = gr.Button("⚡ 快速预览（540P）", scale=1)
                    render_btn = gr.Button("🎞️ 渲染 1080P 成片", variant="primary", size="lg", scale=2,
                                           elem_classes=["cta"])
                video = gr.Video(label="最新成片", height=460, interactive=False, autoplay=False)
                with gr.Accordion("🔍 联系表与自检报告（每屏内容落定后各取一帧，逐项检查节拍、阅读时间、安全区、响度）",
                                  open=True):
                    sheet = gr.Image(label="联系表", type="filepath", interactive=False, height=420)
                    report = gr.Markdown()
                files = gr.File(label="下载（mp4 / 联系表 / 自检报告）", file_count="multiple", interactive=False)

        with gr.Column(scale=5, elem_classes=["log-dock"]):
            logs = gr.Textbox(label="📜 运行日志 Running Logs", lines=20, max_lines=20, autoscroll=True,
                              interactive=False, elem_classes=["terminal-log"])

    # ================================================================ 刷新
    refresh_outputs = [status, table, scene_pick, assets, video, sheet, report, files]
    assert len(refresh_outputs) == len(REFRESH_KEYS)
    skips = [gr.skip()] * len(refresh_outputs)

    def _refresh(pid, sel=None) -> list:
        p = ws.get(pid) if pid else None
        ad = p.motion_ad if p else None
        spans = ad.timeline() if ad else []
        choices = [(f"第 {sp.index + 1} 屏 · {sp.scene.label} · {sp.scene.headline[:10]}", sp.scene.scene_id)
                   for sp in spans]
        ids = [c[1] for c in choices]
        last = ad.latest_render if ad else None
        out_files = [str(f) for f in (last.video_path, last.contact_sheet_path) if f and f.exists()] if last else []
        if last:
            report_file = last.video_path.with_name(f"{last.video_path.stem}_review.md")
            out_files += [str(report_file)] if report_file.exists() else []
        return [
            status_md(p, rt),
            scenes_to_rows(ad),
            gr.update(choices=choices, value=sel if sel in ids else (ids[0] if ids else None)),
            asset_gallery(ad),
            str(last.video_path) if last else None,
            str(last.contact_sheet_path) if last and last.contact_sheet_path and last.contact_sheet_path.exists()
            else None,
            last.report_md if last else "渲染完成后，这里显示联系表与逐项自检结果",
            out_files or None,
        ]

    load_outputs = [aspect, target, bpm, theme, idea, music, sfx, grain]

    def _load_values(pid) -> list:
        p = ws.get(pid) if pid else None
        ad = p.motion_ad if p else None
        if ad is None:
            return ["9:16", 20, DEFAULT_BPM, DEFAULT_THEME, "", True, True, True]
        return [ad.aspect_ratio, ad.target_seconds, ad.bpm, ad.theme, ad.user_motion_idea or "", ad.music, ad.sfx,
                ad.grain]

    def _log(prev, msg):
        return (prev + "\n" if prev else "") + log_line(msg)

    def _ad(pid) -> tuple[Project, MotionAd]:
        p = require_project(ws, pid)
        if p.motion_ad is None or not p.motion_ad.scenes:
            raise gr.Error("请先点「🎬 AI 编排动效方案」")
        return p, p.motion_ad

    def _apply(ad: MotionAd, tbl, asp, tgt, b, th, mu, fx, gr_) -> int:
        n = apply_scene_rows(ad, tbl)
        if asp not in OUTPUT_SIZES:
            raise UIError(f"不支持的画面比例：{asp}")
        ad.set_options(aspect_ratio=asp, target_seconds=int(tgt), bpm=int(b), theme=th, music=bool(mu), sfx=bool(fx),
                       grain=bool(gr_))
        return n

    opt_inputs = [aspect, target, bpm, theme, music, sfx, grain]

    # ================================================================ Step 1：编排
    async def do_plan(pid, asp, tgt, b, th, idea_text, skill_id, prev):
        p = require_project(ws, pid)
        log = _log(prev, f"🎬 动效导演开始编排（{asp} · 约 {int(tgt)} 秒 · {int(b)} BPM · {THEME_LABELS.get(th, th)}"
                         f" · Skill {skill_id}）：先收集本项目素材…")
        yield [log, *skips]
        try:
            ad, mode = await engine.generate(p, aspect_ratio=asp, target_seconds=int(tgt), bpm=int(b), theme=th,
                                             skill_id=skill_id, idea=idea_text)
            async with ws.lock(pid):
                p.motion_ad = ad
                engine.save(p)
        except Exception as e:
            yield [_log(log, friendly(e)), *_refresh(pid)]
            return
        log = _log(log, f"✅ 动效方案完成（{mode}）：{len(ad.assets)} 个素材 → {len(ad.scenes)} 屏，"
                        f"共 {ad.total_seconds:g} 秒，每一刀都落在节拍上")
        for sp in ad.timeline():
            log = _log(log, "　" + describe_scene(sp.index + 1, sp.scene, sp.seconds))
        yield [_log(log, "可在表格里微调后点「⚡ 快速预览」或「🎞️ 渲染 1080P 成片」"), *_refresh(pid)]

    plan_btn.click(do_plan, [header.project_id, aspect, target, bpm, theme, idea,
                             header.skill_dropdowns["motion_director"], logs],
                   [logs, *refresh_outputs], concurrency_id="motion_tab", api_name="motion_plan")

    async def do_save(pid, tbl, *args):
        *opts, prev = args
        p, ad = _ad(pid)
        try:
            async with ws.lock(pid):
                n = _apply(ad, tbl, *opts)
                engine.save(p)
            msg = f"💾 已保存修改（{n} 屏有变化），成片约 {ad.total_seconds:g} 秒"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *_refresh(pid)]

    save_btn.click(do_save, [header.project_id, table, *opt_inputs, logs], [logs, *refresh_outputs],
                   api_name="motion_save")

    def _scene_op(op):
        async def handler(pid, sid, kind, tbl, *args):
            *opts, prev = args
            p, ad = _ad(pid)
            try:
                async with ws.lock(pid):
                    _apply(ad, tbl, *opts)
                    msg, new_sel = op(ad, sid, kind)
                    engine.save(p)
            except Exception as e:
                return [_log(prev, friendly(e)), *skips]
            return [_log(prev, msg), *_refresh(pid, sel=new_sel)]

        return handler

    def _need(ad: MotionAd, sid) -> int:
        if not sid:
            raise UIError("请先在「选中分镜」里选一屏")
        return [s.scene_id for s in ad.scenes].index(sid)

    def _move(delta):
        def op(ad: MotionAd, sid, _kind):
            i = _need(ad, sid)
            j = min(max(i + delta, 0), len(ad.scenes) - 1)
            ids = [s.scene_id for s in ad.scenes]
            ids.insert(j, ids.pop(i))
            ad.reorder(ids)
            return f"↕️ 已移到第 {j + 1} 屏", sid

        return op

    def _add(ad: MotionAd, sid, kind):
        i = _need(ad, sid) + 1 if sid else len(ad.scenes)
        scene = ad.insert_scene(default_scene(kind, ad), i)
        return f"➕ 已插入第 {i + 1} 屏（{scene.label}），请在表格里填写真实文案", scene.scene_id

    def _del(ad: MotionAd, sid, _kind):
        i = _need(ad, sid)
        ad.remove_scene(sid)
        return f"🗑️ 已删除第 {i + 1} 屏", ad.scenes[min(i, len(ad.scenes) - 1)].scene_id

    op_in = [header.project_id, scene_pick, insert_kind, table, *opt_inputs, logs]
    up_btn.click(_scene_op(_move(-1)), op_in, [logs, *refresh_outputs])
    down_btn.click(_scene_op(_move(1)), op_in, [logs, *refresh_outputs])
    add_btn.click(_scene_op(_add), op_in, [logs, *refresh_outputs])
    del_btn.click(_scene_op(_del), op_in, [logs, *refresh_outputs])

    async def do_still(pid, sid, tbl, *args):
        *opts, prev = args
        p, ad = _ad(pid)
        try:
            async with ws.lock(pid):
                _apply(ad, tbl, *opts)
                engine.save(p)
            i = _need(ad, sid)
            yield [_log(prev, f"🖼️ 正在渲染第 {i + 1} 屏的预览…"), gr.skip()]
            path = await engine.still(p, ad, i)
        except Exception as e:
            yield [_log(prev, friendly(e)), gr.skip()]
            return
        yield [_log(prev, f"🖼️ 第 {i + 1} 屏预览已生成"), gr.update(value=str(path), visible=True)]

    still_btn.click(do_still, [header.project_id, scene_pick, table, *opt_inputs, logs], [logs, still_img],
                    concurrency_id="motion_still")

    # ================================================================ Step 2：素材
    async def do_upload(pid, paths, screen, prev):
        p = require_project(ws, pid)
        if not paths:
            return [prev, *skips]
        log = prev
        try:
            async with ws.lock(pid):
                ad, _ = await engine.refresh_assets(p)
                for path in paths:
                    a = await engine.add_upload(p, ad, Path(path), as_screen=bool(screen))
                    log = _log(log, f"📥 已添加素材 {a.key}：{Path(path).name}（{a.width}×{a.height}）")
                engine.save(p)
        except Exception as e:
            log = _log(log, friendly(e))
        return [log, *_refresh(pid)]

    upload.upload(do_upload, [header.project_id, upload, as_screen, logs], [logs, *refresh_outputs],
                  concurrency_id="motion_assets", api_name="motion_upload")

    async def do_collect(pid, asp, prev):
        p = require_project(ws, pid)
        try:
            async with ws.lock(pid):
                ad, added = await engine.refresh_assets(p, asp)
                engine.save(p)
            msg = f"🔄 素材库共 {len(ad.assets)} 个，新增 {added} 个（已有素材的编号保持不变）"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *_refresh(pid)]

    collect_btn.click(do_collect, [header.project_id, aspect, logs], [logs, *refresh_outputs],
                      concurrency_id="motion_assets", api_name="motion_assets")

    # ================================================================ Step 3：渲染
    def _render(quality: str):
        async def handler(pid, tbl, *args):
            *opts, prev = args
            p, ad = _ad(pid)
            label = "快速预览（540P）" if quality == "preview" else "1080P 成片"
            try:
                async with ws.lock(pid):
                    _apply(ad, tbl, *opts)
                    engine.save(p)
            except Exception as e:
                yield [_log(prev, friendly(e)), *skips]
                return
            log = _log(prev, f"🎞️ 开始渲染{label}：{len(ad.scenes)} 屏 · {ad.total_seconds:g} 秒 · 全部在本机完成")
            yield [log, *skips]
            try:
                async for ev in engine.render(p, ad, quality):
                    log = _log(log, ev.message)
                    yield [log, *(_refresh(pid) if ev.done else skips)]
            except (MotionError, Exception) as e:
                yield [_log(log, friendly(e)), *skips]

        return handler

    render_in = [header.project_id, table, *opt_inputs, logs]
    preview_btn.click(_render("preview"), render_in, [logs, *refresh_outputs], concurrency_id="motion_render",
                      concurrency_limit=1, api_name="motion_preview")
    render_btn.click(_render("final"), render_in, [logs, *refresh_outputs], concurrency_id="motion_render",
                     concurrency_limit=1, api_name="motion_render")

    # 加载 / 切换项目时刷新全部内容并回填设定
    for event in (header.load_event, header.project_id.change(_refresh, header.project_id, refresh_outputs)):
        event.then(_load_values, header.project_id, load_outputs)
    header.load_event.then(_refresh, header.project_id, refresh_outputs)
