"""【带货视频】Step 4：剪辑大师一键成片 + 一键导出剪映 / 达芬奇草稿。

布局（build_edit_layout）放在视频选项卡左侧 Step 3 之后；事件（wire_edit_panel）在运行日志组件创建后再绑定，
与 Step 1~3 共用同一个运行日志。Step 4 的刷新输出独立于视频选项卡的 refresh_outputs，不改变其位置顺序。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gradio as gr

from app.engine.draft_export import export_jianying, export_resolve
from app.engine.edit_engine import EditEngine, analyze_media, latest_videos, stale_shots
from app.engine.edit_render import render_final
from app.schemas.edit import TRANSITION_LABELS, EditPlan, transition_code
from app.schemas.project import Project
from app.ui.header import Header, require_project
from app.ui.state import UIError, Workspace, friendly, log_line
from app.ui.theme import step_header

EDIT_HEADERS = ["顺序", "镜头", "启用", "入点(秒)", "出点(秒)", "成片时长(秒)", "转场到下一镜", "转场时长(秒)",
                "原声音量", "卖点花字（≤14字）", "台词字幕"]
TRANSITION_HINT = " / ".join(TRANSITION_LABELS.values())
BGM_TYPES = [".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"]
_SHOT_NO = re.compile(r"(\d+)")


@dataclass
class EditPanel:
    status: gr.Markdown
    idea: gr.Textbox
    plan_btn: gr.Button
    table: gr.Dataframe
    burn_subs: gr.Checkbox
    burn_heads: gr.Checkbox
    bgm_file: gr.File
    bgm_volume: gr.Slider
    duck: gr.Checkbox
    save_btn: gr.Button
    render_btn: gr.Button
    final_video: gr.Video
    jy_dir: gr.Textbox
    jy_btn: gr.Button
    resolve_btn: gr.Button
    export_file: gr.File
    export_info: gr.Markdown


def build_edit_layout(default_jianying_dir: Path | None) -> EditPanel:
    with gr.Column(elem_classes=["step-card"]):
        step_header(4, "剪辑大师一键成片 · 导出剪映 / 达芬奇草稿")
        gr.Markdown("剪辑大师会先**逐个分析各分镜视频里台词与声音的实际位置**，再决定顺序、掐头去尾、转场和卖点花字，"
                    "台词字幕直接取自剧本里的人物同期声对白（不做语音识别，字不会错）。"
                    "生成方案后可以在表格里微调，再一键合成 mp4，或导出成剪映 / 达芬奇工程继续精剪。")
        with gr.Row():
            idea = gr.Textbox(label="✂️ 我的剪辑想法（选填）", lines=1, scale=4,
                              placeholder="例如：控制在 30 秒内；节奏再快一点；结尾花字写「买二送一」")
            plan_btn = gr.Button("🧠 生成剪辑方案", variant="primary", scale=1)
        status = gr.Markdown("Step 3 生成分镜视频后，点「生成剪辑方案」", elem_classes=["status-note"])
        table = gr.Dataframe(headers=EDIT_HEADERS, interactive=True, wrap=True, static_columns=[1, 5],
                             datatype=["number", "str", "bool", "number", "number", "number", "str", "number",
                                       "number", "str", "str"],
                             column_widths=[64, 80, 64, 90, 90, 104, 116, 104, 90, 200, 320],
                             label=f"剪辑方案（可编辑：改「顺序」可调换镜头先后；转场可填 {TRANSITION_HINT}）",
                             elem_classes=["plan-table"])
        with gr.Accordion("🎚️ 成片选项：字幕、花字与背景音乐", open=False):
            with gr.Row():
                burn_subs = gr.Checkbox(value=True, label="成片烧录台词字幕")
                burn_heads = gr.Checkbox(value=True, label="成片烧录卖点花字")
                duck = gr.Checkbox(value=True, label="人物说话时自动压低背景音乐")
            with gr.Row():
                bgm_file = gr.File(label="🎵 背景音乐（选填，mp3 / wav / m4a；请使用有版权授权的音乐）",
                                   file_types=BGM_TYPES, type="filepath", height=120, scale=3)
                bgm_volume = gr.Slider(0, 1, value=0.18, step=0.01, label="背景音乐音量", scale=2)
        with gr.Row():
            save_btn = gr.Button("💾 保存剪辑修改")
            render_btn = gr.Button("🎞️ 一键成片（合成 mp4）", variant="primary", size="lg", scale=2,
                                   elem_classes=["cta"])
        final_video = gr.Video(label="成片预览", height=420, interactive=False, autoplay=False, visible=False)
        gr.Markdown("#### 📦 导出剪辑工程（时间线与成片完全一致：顺序、出入点、转场、字幕都已排好）")
        jy_dir = gr.Textbox(value=str(default_jianying_dir or ""),
                            label="剪映草稿文件夹（已自动检测，留空则只打包下载）",
                            info="剪映 → 设置 → 全局设置 → 草稿位置；填写后草稿直接出现在剪映「本地草稿」里",
                            max_lines=1)
        with gr.Row():
            jy_btn = gr.Button("🎬 一键导出剪映草稿")
            resolve_btn = gr.Button("🎨 一键导出达芬奇工程（FCPXML / OTIO + 字幕）")
        export_file = gr.File(label="导出文件（点击下载）", interactive=False)
        export_info = gr.Markdown()
    return EditPanel(status, idea, plan_btn, table, burn_subs, burn_heads, bgm_file, bgm_volume, duck, save_btn,
                     render_btn, final_video, jy_dir, jy_btn, resolve_btn, export_file, export_info)


# ---------------------------------------------------------------- 表格 <-> 模型


def edit_to_rows(plan: EditPlan | None) -> list[list[Any]]:
    if plan is None:
        return []
    return [[i, f"镜头 {c.shot_index}", c.enabled, c.source_in, c.source_out, c.length_seconds,
             TRANSITION_LABELS[c.transition_out], c.transition_seconds, c.volume, c.headline, c.subtitle]
            for i, c in enumerate(plan.clips, start=1)]


def _rows(table: Any) -> list[list[Any]]:
    if table is None:
        return []
    if hasattr(table, "values") and hasattr(table, "columns"):
        return [list(r) for r in table.values.tolist()]
    if isinstance(table, dict) and "data" in table:
        return [list(r) for r in table["data"]]
    return [list(r) for r in table]


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "否", "no", "off", "停用"}
    return bool(value)


def _float(value: Any, label: str, row: str) -> float:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError) as e:
        raise UIError(f"{row}「{label}」必须是数字") from e


def apply_edit_rows(plan: EditPlan, table: Any) -> int:
    """把表格修改写回剪辑方案（按「镜头 N」对应片段），再按「顺序」列重新排序。返回有变化的片段数。"""
    rows = _rows(table)
    if not rows:
        return 0
    if len(rows) != len(plan.clips):
        raise UIError("表格行数与剪辑方案不一致：请重新生成剪辑方案")
    by_shot = {c.shot_index: c for c in plan.clips}
    changed, order = 0, []
    for pos, raw in enumerate(rows):
        row = [*raw, *[""] * (len(EDIT_HEADERS) - len(raw))]
        m = _SHOT_NO.search(str(row[1]))
        clip = by_shot.get(int(m.group(1))) if m else None
        if clip is None:
            raise UIError(f"第 {pos + 1} 行的镜头「{row[1]}」不在剪辑方案里：请重新生成剪辑方案")
        label = f"镜头 {clip.shot_index}"
        kind = transition_code(row[6])
        if kind is None:
            raise UIError(f"{label}「转场」填写「{row[6]}」无效，可选：{TRANSITION_HINT}")
        new = {
            "enabled": _bool(row[2]),
            "source_in": _float(row[3], "入点", label),
            "source_out": _float(row[4], "出点", label),
            "transition_out": kind,
            "transition_seconds": _float(row[7], "转场时长", label),
            "volume": _float(row[8], "原声音量", label),
            "headline": str(row[9] or "").strip(),
            "subtitle": str(row[10] or "").strip(),
        }
        old = {k: getattr(clip, k) for k in new}
        if new != old:
            try:
                plan.update_clip(clip.clip_id, **new)
            except ValueError as e:
                raise UIError(f"{label} 修改无效：{friendly(e).removeprefix('❌ ')}") from e
            changed += 1
        try:
            rank = float(row[0])
        except (TypeError, ValueError):
            rank = pos + 1
        order.append((rank, pos, clip.clip_id))
    new_order = [cid for _, _, cid in sorted(order)]
    if new_order != [c.clip_id for c in plan.clips]:
        plan.reorder(new_order)
        changed += 1
    return changed


# ---------------------------------------------------------------- 状态文案


def edit_status_md(p: Project | None) -> str:
    s = p.video_script if p else None
    plan = p.video_edit if p else None
    if s is None or s.plan_status != "confirmed":
        return "确认分镜剧本并在 Step 3 生成视频后，就可以让剪辑大师一键成片"
    ready = latest_videos(s)
    if plan is None:
        if not ready:
            return "还没有分镜视频：请先在 Step 3 生成视频"
        missing = [i for i, x in enumerate(s.shots, start=1) if not x.playable_videos]
        tip = f"（镜头 {'、'.join(map(str, missing))} 还没有视频，暂不加入剪辑）" if missing else ""
        return f"已有 {len(ready)}/{len(s.shots)} 个分镜视频{tip}，点「🧠 生成剪辑方案」"
    tl = plan.timeline()
    enabled = len(tl.clips)
    fades = sum(1 for j in tl.joins if j.kind != "cut")
    heads = sum(1 for c in plan.enabled_clips if c.headline)
    bgm = f"背景音乐 {plan.bgm_path.name}" if plan.bgm_path and plan.bgm_path.exists() else "无背景音乐"
    subs = "烧录" if plan.burn_subtitles else "不烧录"
    lines = [f"**《{plan.title}》**　｜　成片约 **{tl.total / plan.fps:.1f} 秒**"
             f"　｜　启用 {enabled}/{len(plan.clips)} 镜　｜　转场 {fades} 处　｜　花字 {heads} 条　｜　字幕{subs}"
             f"　｜　{bgm}　｜　{plan.width}×{plan.height} {plan.fps}fps"]
    if plan.edit_summary:
        lines.append(f"✂️ {plan.edit_summary}")
    stale = stale_shots(plan, s) if plan.script_id == s.script_id else []
    if plan.script_id != s.script_id:
        lines.append("⚠️ 分镜剧本已重新生成，这个剪辑方案对应的是旧剧本：请点「🧠 生成剪辑方案」重新剪辑")
    elif stale:
        lines.append(f"⚠️ 镜头 {'、'.join(map(str, stale))} 有了新视频（或新生成），剪辑方案还在用旧版本："
                     "点「🧠 生成剪辑方案」即可换成最新视频")
    if (cut := plan.latest_render) is not None:
        lines.append(f"🎞️ 最近一次成片：{cut.file_path.name}（{cut.duration_seconds:g} 秒，"
                     f"{cut.created_at.astimezone():%m-%d %H:%M}）")
    return "\n\n".join(lines)


# ---------------------------------------------------------------- 事件


def wire_edit_panel(ws: Workspace, header: Header, panel: EditPanel, logs: gr.Textbox, after: list) -> None:
    rt = ws.rt
    engine = EditEngine(rt)
    outputs = [panel.status, panel.table, panel.final_video, panel.burn_subs, panel.burn_heads, panel.bgm_file,
               panel.bgm_volume, panel.duck]
    options = [panel.burn_subs, panel.burn_heads, panel.bgm_volume, panel.duck]

    def refresh(pid):
        p = ws.get(pid) if pid else None
        plan = p.video_edit if p else None
        cut = plan.latest_render if plan else None
        bgm = str(plan.bgm_path) if plan and plan.bgm_path and plan.bgm_path.exists() else None
        return [
            edit_status_md(p), edit_to_rows(plan),
            gr.update(visible=cut is not None, value=str(cut.file_path) if cut else None),
            plan.burn_subtitles if plan else True, plan.burn_headlines if plan else True, bgm,
            plan.bgm_volume if plan else 0.18, plan.duck_bgm if plan else True,
        ]

    def _log(prev, msg):
        return (prev + "\n" if prev else "") + log_line(msg)

    def _plan(pid) -> tuple[Project, EditPlan]:
        p = require_project(ws, pid)
        if p.video_edit is None:
            raise gr.Error("请先点「🧠 生成剪辑方案」")
        return p, p.video_edit

    def _apply(plan: EditPlan, table, subs, heads, vol, duck) -> int:
        n = apply_edit_rows(plan, table)
        plan.set_options(burn_subtitles=bool(subs), burn_headlines=bool(heads), bgm_volume=float(vol),
                         duck_bgm=bool(duck))
        return n

    # ------------------------------------------------ 生成剪辑方案
    async def do_plan(pid, skill_id, idea, prev):
        p = require_project(ws, pid)
        s = p.video_script
        if s is None or s.plan_status != "confirmed":
            raise gr.Error("请先确认分镜剧本，并在 Step 3 生成视频")
        found = latest_videos(s)
        if not found:
            raise gr.Error("还没有分镜视频：请先在 Step 3 生成视频")
        log = _log(prev, f"🧠 剪辑大师开始分析 {len(found)} 个分镜视频（时长、台词与声音位置）…")
        missing = [i for i, x in enumerate(s.shots, start=1) if not x.playable_videos]
        if missing:
            log = _log(log, f"⚠️ 镜头 {'、'.join(map(str, missing))} 还没有视频，本次不加入剪辑")
        yield [log, *refresh(pid)]
        try:
            plan, mode, sources = await engine.generate(p, s, skill_id, idea)
            async with ws.lock(pid):
                p.video_edit = plan
                ws.save(p)
        except Exception as e:
            yield [_log(log, friendly(e)), *refresh(pid)]
            return
        raw = sum(src.info.duration for src in sources)
        log = _log(log, f"✅ 剪辑方案完成（{mode} · Skill {plan.editor_skill_id}）：素材共 {raw:.1f} 秒 → "
                        f"成片约 {plan.total_seconds:g} 秒")
        for c in plan.clips:
            tag = "" if c.enabled else "（停用）"
            head = f" · 花字「{c.headline}」" if c.headline else ""
            log = _log(log, f"　镜头 {c.shot_index}{tag}：用 {c.source_in:g}–{c.source_out:g} 秒"
                            f"（{c.length_seconds:g} 秒）· {TRANSITION_LABELS[c.transition_out]}{head}")
        yield [_log(log, "可在表格里微调后点「🎞️ 一键成片」，或直接导出剪映 / 达芬奇工程"), *refresh(pid)]

    panel.plan_btn.click(do_plan, [header.project_id, header.skill_dropdowns["video_editor"], panel.idea, logs],
                         [logs, *outputs], concurrency_id="video_edit", api_name="video_edit_plan")

    # ------------------------------------------------ 保存修改
    def do_save(pid, table, subs, heads, vol, duck, prev):
        p, plan = _plan(pid)
        try:
            n = _apply(plan, table, subs, heads, vol, duck)
            ws.save(p)
            msg = f"💾 已保存剪辑修改（{n} 处变化），成片约 {plan.total_seconds:g} 秒"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *refresh(pid)]

    panel.save_btn.click(do_save, [header.project_id, panel.table, *options, logs], [logs, *outputs],
                         api_name="video_edit_save")

    # ------------------------------------------------ 背景音乐
    async def on_bgm(pid, path, prev):
        p, plan = _plan(pid)
        if not path:
            return [prev, *refresh(pid)]
        src = Path(path)
        try:
            if src.suffix.lower() not in BGM_TYPES:
                raise UIError(f"只支持 {' / '.join(BGM_TYPES)} 格式的背景音乐")
            saved = await asyncio.to_thread(rt.store.import_file, p.project_id, src, "edits",
                                            dest_name=f"bgm_{src.stem[:24]}{src.suffix.lower()}")
            info = await analyze_media(saved)
            if not info.has_audio:
                raise UIError("这个文件里没有音轨，请换一个音乐文件")
            plan.set_bgm(saved, info.duration)
            ws.save(p)
            msg = f"🎵 已添加背景音乐：{src.name}（{info.duration:.1f} 秒，比成片短时自动循环）"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *refresh(pid)]

    def on_bgm_clear(pid, prev):
        p = ws.get(pid) if pid else None
        if p is None or p.video_edit is None:
            return [prev, *refresh(pid)]
        p.video_edit.set_bgm(None)
        ws.save(p)
        return [_log(prev, "🎵 已移除背景音乐"), *refresh(pid)]

    panel.bgm_file.upload(on_bgm, [header.project_id, panel.bgm_file, logs], [logs, *outputs])
    panel.bgm_file.clear(on_bgm_clear, [header.project_id, logs], [logs, *outputs])

    # ------------------------------------------------ 一键成片
    async def do_render(pid, table, subs, heads, vol, duck, prev):
        p, plan = _plan(pid)
        try:
            _apply(plan, table, subs, heads, vol, duck)
            ws.save(p)
        except Exception as e:
            yield [_log(prev, friendly(e)), *refresh(pid)]
            return
        # 渲染用快照：合成过程中用户继续改表格，不影响本次成片
        snapshot = plan.model_copy(deep=True)
        out_dir = rt.store.asset_dir(p.project_id, "edits", plan.edit_id)
        log = prev
        try:
            async for ev in render_final(snapshot, out_dir):
                log = _log(log, ev.message)
                if ev.video_path:
                    plan.add_render(ev.video_path, snapshot.total_seconds)
                    ws.save(p)
                    yield [log, *refresh(pid)]
                else:
                    yield [log, *[gr.skip()] * len(outputs)]
        except Exception as e:
            yield [_log(log, friendly(e)), *refresh(pid)]

    panel.render_btn.click(do_render, [header.project_id, panel.table, *options, logs], [logs, *outputs],
                           concurrency_id="video_cut", concurrency_limit=1, api_name="video_edit_render")

    # ------------------------------------------------ 导出剪映 / 达芬奇
    def _exports_dir(p: Project) -> Path:
        d = rt.store.project_dir(p.project_id) / "exports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def do_jianying(pid, table, subs, heads, vol, duck, jy_dir, prev):
        p, plan = _plan(pid)
        root = Path(jy_dir.strip().strip('"')) if jy_dir and jy_dir.strip() else None
        try:
            _apply(plan, table, subs, heads, vol, duck)
            ws.save(p)
            if root is not None and not root.is_dir():
                raise UIError(f"剪映草稿文件夹不存在：{root}（请在剪映 → 全局设置 → 草稿位置 查看，或留空只打包下载）")
            snapshot = plan.model_copy(deep=True)
            zip_path, installed = await asyncio.to_thread(export_jianying, snapshot, _exports_dir(p), root)
        except Exception as e:
            return [_log(prev, friendly(e)), None, ""]
        if installed is not None:
            info = (f"✅ 已写入剪映草稿文件夹：`{installed}`\n\n**重新打开剪映专业版**，在「本地草稿」里就能看到"
                    f"「{installed.name}」。压缩包可拷到其他电脑使用（解压到剪映草稿文件夹）。")
        else:
            info = ("✅ 剪映草稿已打包。下载后把压缩包里的文件夹**整个解压到剪映草稿文件夹**"
                    "（剪映 → 设置 → 全局设置 → 草稿位置），再重新打开剪映即可。")
        msg = f"🎬 剪映草稿已导出：{zip_path.name}（{snapshot.total_seconds:g} 秒）"
        return [_log(prev, msg), str(zip_path), info]

    async def do_resolve(pid, table, subs, heads, vol, duck, prev):
        p, plan = _plan(pid)
        try:
            _apply(plan, table, subs, heads, vol, duck)
            ws.save(p)
            snapshot = plan.model_copy(deep=True)
            zip_path, bundle = await asyncio.to_thread(export_resolve, snapshot, _exports_dir(p))
        except Exception as e:
            return [_log(prev, friendly(e)), None, ""]
        info = (f"✅ 达芬奇工程已导出。本机可直接导入工程文件夹 `{bundle}` 里的 `.fcpxml`："
                "DaVinci Resolve → 文件 → 导入 → 时间线；字幕用 文件 → 导入 → 字幕 导入 `台词字幕.srt`。"
                f"\n\n导入前把项目设置为 {snapshot.width}×{snapshot.height}、{snapshot.fps} 帧/秒，"
                "详细步骤见压缩包里的「导入说明.txt」。")
        return [_log(prev, f"🎨 达芬奇工程已导出：{zip_path.name}"), str(zip_path), info]

    export_out = [logs, panel.export_file, panel.export_info]
    panel.jy_btn.click(do_jianying, [header.project_id, panel.table, *options, panel.jy_dir, logs], export_out,
                       concurrency_id="video_export", api_name="video_export_jianying")
    panel.resolve_btn.click(do_resolve, [header.project_id, panel.table, *options, logs], export_out,
                            concurrency_id="video_export", api_name="video_export_resolve")

    # Step 3 生成视频后、加载 / 切换项目时刷新 Step 4
    for event in after:
        event.then(refresh, header.project_id, outputs)
    header.load_event.then(refresh, header.project_id, outputs)
    header.project_id.change(refresh, header.project_id, outputs)
