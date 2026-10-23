"""【主图】与【详情页】选项卡（同一套三步式工作流，参数不同）。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import gradio as gr

from app.engine.planner_engine import PlannerEngine
from app.engine.project_service import export_winners, reuse_references, winners_in_order
from app.engine.prompt_compiler import image_skill_for_model
from app.engine.render_engine import RenderEngine
from app.providers.demo.content import auto_detail_screens
from app.providers.image_models import choices as model_choices
from app.schemas.plan import AESTHETIC_STYLE_NAMES, ImagePlan
from app.settings import IMAGE_QUALITIES, IMAGE_RESOLUTIONS
from app.ui.header import Header, collect_info, require_project
from app.ui.state import (
    AESTHETIC_CHOICES,
    MODE_CHOICES,
    SLOT_HEADERS,
    Workspace,
    apply_slot_rows,
    design_hint,
    friendly,
    log_line,
    mode_hint,
    new_blank_slot,
    slots_to_rows,
)
from app.ui.theme import step_header

MAX_REF_ROLES = 8


@dataclass(frozen=True)
class TabSpec:
    key: str  # main_image / detail_page
    attr: str  # Project 上的字段名
    label: str
    ratios: tuple[str, ...]
    default_ratio: str


MAIN_SPEC = TabSpec("main_image", "main_image_plan", "主图", ("1:1", "3:4", "9:16", "16:9"), "1:1")
DETAIL_SPEC = TabSpec("detail_page", "detail_page_plan", "详情页", ("3:4", "9:16", "1:1"), "3:4")


def _plan(ws: Workspace, pid, spec: TabSpec) -> ImagePlan:
    p = require_project(ws, pid)
    plan = getattr(p, spec.attr)
    if plan is None:
        raise gr.Error(f"请先点击「✨ 生成{spec.label}策划案」")
    return plan


def _status_md(plan: ImagePlan | None, spec: TabSpec) -> str:
    if plan is None:
        return f"尚未生成{spec.label}策划案"
    if plan.plan_status == "draft":
        style = AESTHETIC_STYLE_NAMES.get(plan.aesthetic_style, "自动匹配")
        return (f"📝 **草稿（可编辑）**　共 {len(plan.slots)} 张　｜　全局美学：{style}　｜　{plan.plan_summary}\n\n"
                f"直接在表格中修改文字、生图模式、美学流派、构图模式或参考图角色，完成后点「✅ 确认」。"
                f"{mode_hint()}\n\n{design_hint()}")
    return f"🔒 **已确认定稿**　共 {len(plan.slots)} 张　｜　如需修改请点「🔓 解锁返回修改」"


def _checklist_md(plan: ImagePlan | None) -> str:
    if plan is None or plan.plan_status != "confirmed":
        return "确认策划案后，这里会显示去重汇总后的《所需参考图清单》"
    if not plan.reference_checklist:
        return "📋 本方案全部为文生图，**无需上传参考图**，可直接开始生成"
    uploaded = {a.role for a in plan.reference_assets}
    lines = ["| 状态 | 参考图角色 | 拍摄要求 | 用于槽位 |", "|---|---|---|---|"]
    index = {s.slot_id: i for i, s in enumerate(plan.slots, start=1)}
    for c in plan.reference_checklist:
        mark = "✅ 已上传" if c.role in uploaded else "⬜ 待上传"
        slots = "、".join(f"#{index[s]}" for s in c.slot_ids if s in index)
        lines.append(f"| {mark} | `{c.role}` | {c.description or '-'} | {slots} |")
    return "\n".join(lines)


def _upload_updates(plan: ImagePlan | None) -> list:
    """每个参考图角色一个上传框（最多 MAX_REF_ROLES 个）。"""
    items = plan.reference_checklist if plan is not None and plan.plan_status == "confirmed" else []
    by_role = {a.role: a for a in (plan.reference_assets if plan else [])}
    ups = []
    for i in range(MAX_REF_ROLES):
        if i < len(items):
            c = items[i]
            asset = by_role.get(c.role)
            ups.append(gr.update(visible=True, label=f"📷 {c.role}：{c.description or ''}",
                                 value=str(asset.file_path) if asset else None))
        else:
            ups.append(gr.update(visible=False, value=None))
    return ups


def _slot_choices(plan: ImagePlan | None) -> list[tuple[str, str]]:
    if plan is None:
        return []
    return [(f"#{i} {s.title}（{s.generation_mode}）", s.slot_id) for i, s in enumerate(plan.slots, start=1)]


def _gallery(plan: ImagePlan | None) -> list[tuple[str, str]]:
    if plan is None:
        return []
    out = []
    for i, s in enumerate(plan.slots, start=1):
        for j, c in enumerate(s.candidates, start=1):
            if c.file_path.exists():
                out.append((str(c.file_path), f"{'⭐ ' if c.selected else ''}#{i} {s.title} · 候选{j}"))
    return out


def _winner_choices(plan: ImagePlan | None) -> tuple[list[tuple[str, str]], list[str]]:
    if plan is None:
        return [], []
    choices, selected = [], []
    for i, s in enumerate(plan.slots, start=1):
        for j, c in enumerate(s.candidates, start=1):
            key = f"{s.slot_id}|{c.candidate_id}"
            choices.append((f"#{i} {s.title} · 候选{j}", key))
            if c.selected:
                selected.append(key)
    return choices, selected


def build_image_tab(ws: Workspace, header: Header, spec: TabSpec) -> None:
    rt = ws.rt
    planner, renderer = PlannerEngine(rt), RenderEngine(rt)
    is_detail = spec.key == "detail_page"

    with gr.Row(elem_classes=["workbench"]):
        # ---------------------------------------------------------------- 左侧：工作区
        with gr.Column(scale=7):
            with gr.Column(elem_classes=["step-card"]):
                step_header(1, f"生成与编辑{spec.label}策划案")
                with gr.Row():
                    if is_detail:
                        count_mode = gr.Radio([("AI 按卖点自动规划屏数", "auto"), ("手动指定屏数", "manual")],
                                              value="auto", label="屏数模式")
                        count = gr.Slider(4, 15, value=8, step=1, label="手动屏数（4~15）")
                    else:
                        count_mode = gr.State("manual")
                        count = gr.Slider(1, 10, value=5, step=1, label="主图数量（随平台默认，可改）")
                    ratio = gr.Dropdown(list(spec.ratios), value=spec.default_ratio, label=f"{spec.label}比例")
                aesthetic = gr.Dropdown(
                    AESTHETIC_CHOICES, value="auto", label="🎨 全局视觉设计美学流派",
                    info="选「自动匹配」时 AI 为每张图分别挑选流派；选定某个流派则整套统一使用，"
                         "生成后仍可在表格中逐张修改",
                )
                idea = gr.Textbox(
                    label="💡 我的策划想法（选填）", lines=2,
                    placeholder="不填：按类目与平台自动策划；填写：优先按您的想法定制。"
                                "例如：中秋主题、暖金色调、重点突出白霜对比",
                )
                with gr.Row():
                    gen_btn = gr.Button(f"✨ 生成{spec.label}策划案", variant="primary")
                    regen_btn = gr.Button("🔄 按新想法重新生成")
                status = gr.Markdown(f"尚未生成{spec.label}策划案", elem_classes=["status-note"])
                # 列宽用像素：总宽超过卡片时横向滚动，标题、画面描述、美学 / 构图列不再被压窄
                table = gr.Dataframe(
                    headers=SLOT_HEADERS, datatype=["number", "str", "str", "str", "str", "str", "str", "str", "str"],
                    interactive=True, wrap=True, static_columns=[0],
                    column_widths=[64, 170, 160, 200, 380, 160, 150, 150, 190],
                    label=f"可编辑策划区（生图模式可填：{' / '.join(MODE_CHOICES)}；"
                          "美学流派、构图模式填中文名或「自动」）",
                    elem_classes=["plan-table"],
                )
                with gr.Row():
                    save_edit = gr.Button("💾 保存表格修改")
                    slot_pick = gr.Dropdown(label="选择槽位", choices=[], scale=2)
                    regen_slot = gr.Button("🎲 单独重新生成该槽位")
                with gr.Row():
                    add_slot = gr.Button("➕ 在末尾新增槽位")
                    del_slot = gr.Button("🗑️ 删除所选槽位")
                    up_slot = gr.Button("⬆️ 上移")
                    down_slot = gr.Button("⬇️ 下移")
                with gr.Row():
                    confirm_btn = gr.Button(f"✅ 确认{spec.label}策划案，生成传图清单", variant="primary")
                    unlock_btn = gr.Button("🔓 解锁返回修改策划")

            with gr.Column(elem_classes=["step-card"]):
                step_header(2, "按清单传图与并发渲染")
                checklist = gr.Markdown(_checklist_md(None), elem_classes=["status-note"])
                with gr.Row():
                    reuse_btn = gr.Button("♻️ 一键复用项目内其他模块已上传的同角色参考图")
                uploads = []
                with gr.Row():
                    for i in range(MAX_REF_ROLES):
                        uploads.append(gr.Image(type="filepath", visible=False, height=180,
                                                key=f"{spec.key}_up_{i}", sources=["upload", "clipboard"]))
                with gr.Accordion("🎯 为特定槽位单独上传 / 替换参考图", open=False):
                    with gr.Row():
                        single_slot = gr.Dropdown(label="槽位", choices=[])
                        single_img = gr.Image(type="filepath", label="单独参考图（只绑定到该槽位）", height=160)
                    single_btn = gr.Button("📎 绑定到该槽位")
                with gr.Row():
                    model = gr.Dropdown(model_choices(rt.settings.image_models), value=rt.settings.image_models[0],
                                        label="生图模型（【高质量】/【普通】为 RightAPI 渠道）", scale=3)
                    res = gr.Dropdown(list(IMAGE_RESOLUTIONS), value=rt.settings.image_resolution, label="分辨率")
                    quality = gr.Dropdown(list(IMAGE_QUALITIES), value=rt.settings.image_quality,
                                          label="GPT 质量档（xhigh/max 仅 2.5）")
                    cand = gr.Slider(1, 4, value=2, step=1, label="每槽位候选图数量")
                render_btn = gr.Button(f"🚀 开始并发生成{spec.label}", variant="primary", size="lg",
                                       elem_classes=["cta"])

        # ---------------------------------------------------------------- 右侧：日志与结果
        with gr.Column(scale=5):
            logs = gr.Textbox(label="📜 运行日志 Running Logs", lines=12, max_lines=12, autoscroll=True,
                              interactive=False, elem_classes=["terminal-log"])
            with gr.Column(elem_classes=["step-card"]):
                step_header(3, "勾选定稿并打包导出")
                gallery = gr.Gallery(label="候选图（生成一张回显一张）", columns=3, height=420, object_fit="contain")
                winners = gr.CheckboxGroup(label="⭐ 勾选定稿图（Winner）", choices=[])
                with gr.Row():
                    save_winner = gr.Button("⭐ 保存定稿勾选")
                    export_btn = gr.Button("📦 打包导出定稿")
                export_file = gr.File(label="导出文件", interactive=False)
                if is_detail:
                    long_preview = gr.Gallery(label="📜 详情页长图序列预览（按屏顺序，每屏定稿图）", columns=1,
                                              height=520, object_fit="contain")

    # ================================================================ 刷新函数
    def _refresh(pid):
        p = ws.get(pid) if pid else None
        plan = getattr(p, spec.attr) if p else None
        ch = _slot_choices(plan)
        wch, wsel = _winner_choices(plan)
        outs = [
            _status_md(plan, spec), slots_to_rows(plan),
            gr.update(choices=ch, value=ch[0][1] if ch else None),
            gr.update(choices=ch, value=ch[0][1] if ch else None),
            _checklist_md(plan), *_upload_updates(plan), _gallery(plan),
            gr.update(choices=wch, value=wsel),
        ]
        if is_detail:
            outs.append([str(x) for x in winners_in_order(p, spec.attr)] if p else [])
        return outs

    refresh_outputs = [status, table, slot_pick, single_slot, checklist, *uploads, gallery, winners]
    if is_detail:
        refresh_outputs.append(long_preview)

    def _log(prev: str, msg: str) -> str:
        return (prev + "\n" if prev else "") + log_line(msg)

    # ================================================================ Step 1
    async def do_generate(pid, cid, pfid, cmode, n, r, style, idea_text, skill_id, prev_log, *info_vals):
        p = require_project(ws, pid)
        info = collect_info(ws, cid, info_vals)
        p.category_id, p.platform_id, p.product_info = cid, pfid, info
        existing = getattr(p, spec.attr)
        if existing is not None and existing.plan_status == "confirmed":
            raise gr.Error("当前策划案已确认锁定，请先点「🔓 解锁返回修改策划」再重新生成")
        count_val = None if cmode == "auto" else int(n)
        plat = rt.platform(pfid)
        hint = (f"AI 自动规划 {auto_detail_screens(rt.category(cid), plat)} 屏" if count_val is None
                else f"{count_val} 张")
        log = _log(prev_log, f"开始{spec.label}策划：{rt.category(cid).name} · {plat.name} · {hint} · {r}")
        log = _log(log, "我的策划想法：" + (idea_text.strip() if idea_text and idea_text.strip()
                                        else "（未填写，交由 AI 按类目与平台自动策划）"))
        log = _log(log, "全局美学流派：" + AESTHETIC_STYLE_NAMES.get(style, "自动匹配（AI 按每张图的作用选择）"))
        log = _log(log, f"执行 Skill：{skill_id}（{'真实 LLM' if rt.is_live('llm') else '演示模式'}）")
        yield [log, *_refresh(pid)]
        try:
            async with ws.lock(p.project_id):
                plan, mode = await planner.generate(
                    deliverable_type=spec.key, category_id=cid, platform_id=pfid, aspect_ratio=r, count=count_val,
                    user_creative_idea=idea_text, product_info=info, skill_id=skill_id, existing=existing,
                    aesthetic_style=style or "auto",
                )
                setattr(p, spec.attr, plan)
                ws.save(p)
        except Exception as e:  # 交给界面展示
            yield [_log(log, friendly(e)), *_refresh(pid)]
            return
        counts = {m: sum(1 for s in plan.slots if s.generation_mode == m) for m in MODE_CHOICES}
        styles = Counter(AESTHETIC_STYLE_NAMES.get(s.aesthetic_style, "自动") for s in plan.slots)
        log = _log(log, f"✅ 策划完成（{mode}）：共 {len(plan.slots)} 张；文生图 {counts['text_to_image']}、"
                        f"单图编辑 {counts['single_image_edit']}、多图融合 {counts['multi_image_fusion']}；"
                        "🎨 美学流派：" + "、".join(f"{k} {v}" for k, v in styles.items()))
        yield [log, *_refresh(pid)]

    gen_inputs = [header.project_id, header.category, header.platform, count_mode, count, ratio, aesthetic, idea,
                  header.skill_dropdowns["ecommerce_planner"], logs, *header.info_fields]
    for btn in (gen_btn, regen_btn):
        btn.click(do_generate, gen_inputs, [logs, *refresh_outputs], concurrency_id=f"{spec.key}_tab",
                  concurrency_limit=2, api_name=f"{spec.key}_generate" if btn is gen_btn else False)

    def do_save_edit(pid, tbl, prev):
        plan = _plan(ws, pid, spec)
        try:
            n = apply_slot_rows(plan, tbl, rt.category(plan.category_id).reference_image_roles)
            ws.save(ws.get(pid))
            msg = f"💾 已保存表格修改（{n} 个槽位有变化）"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *_refresh(pid)]

    save_edit.click(do_save_edit, [header.project_id, table, logs], [logs, *refresh_outputs])

    async def do_regen_slot(pid, sid, tbl, prev, cid, *info_vals):
        plan = _plan(ws, pid, spec)
        if not sid:
            raise gr.Error("请先选择一个槽位")
        log = _log(prev, "🎲 正在单独重新生成所选槽位…")
        yield [log, *_refresh(pid)]
        try:
            apply_slot_rows(plan, tbl, rt.category(plan.category_id).reference_image_roles)
            new = await planner.regenerate_slot(plan, sid, collect_info(ws, cid, info_vals))
            ws.save(ws.get(pid))
            log = _log(log, f"✅ 已重新生成：「{new.title}」（{new.generation_mode}）")
        except Exception as e:
            log = _log(log, friendly(e))
        yield [log, *_refresh(pid)]

    regen_slot.click(do_regen_slot, [header.project_id, slot_pick, table, logs, header.category,
                                     *header.info_fields], [logs, *refresh_outputs],
                     concurrency_id=f"{spec.key}_tab")

    def _structural(op):
        def handler(pid, sid, tbl, prev):
            plan = _plan(ws, pid, spec)
            try:
                apply_slot_rows(plan, tbl, rt.category(plan.category_id).reference_image_roles)
                msg = op(plan, sid)
                ws.save(ws.get(pid))
            except Exception as e:
                msg = friendly(e)
            return [_log(prev, msg), *_refresh(pid)]
        return handler

    def _add(plan, sid):
        plan.add_slot(new_blank_slot(plan.default_aspect_ratio))
        return "➕ 已在末尾新增 1 个槽位，请在表格中填写内容"

    def _del(plan, sid):
        if not sid:
            raise ValueError("请先选择要删除的槽位")
        title = plan.get_slot(sid).title
        plan.remove_slot(sid)
        return f"🗑️ 已删除槽位「{title}」"

    def _move(delta):
        def op(plan, sid):
            if not sid:
                raise ValueError("请先选择槽位")
            i = plan._index_of(sid)
            plan.move_slot(sid, max(0, min(len(plan.slots) - 1, i + delta)))
            return "↕️ 已调整槽位顺序"
        return op

    struct_in = [header.project_id, slot_pick, table, logs]
    add_slot.click(_structural(_add), struct_in, [logs, *refresh_outputs])
    del_slot.click(_structural(_del), struct_in, [logs, *refresh_outputs])
    up_slot.click(_structural(_move(-1)), struct_in, [logs, *refresh_outputs])
    down_slot.click(_structural(_move(1)), struct_in, [logs, *refresh_outputs])

    def do_confirm(pid, tbl, prev):
        plan = _plan(ws, pid, spec)
        try:
            if plan.plan_status == "draft":
                apply_slot_rows(plan, tbl, rt.category(plan.category_id).reference_image_roles)
            checklist_items = plan.confirm_plan()
            ws.save(ws.get(pid))
            roles = "、".join(c.role for c in checklist_items) or "无（全部文生图）"
            msg = f"✅ 策划案已确认锁定，所需参考图：{roles}"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *_refresh(pid)]

    confirm_btn.click(do_confirm, [header.project_id, table, logs], [logs, *refresh_outputs],
                      api_name=f"{spec.key}_confirm")

    def do_unlock(pid, prev):
        plan = _plan(ws, pid, spec)
        try:
            plan.unlock_plan()
            ws.save(ws.get(pid))
            msg = "🔓 已解锁，可继续编辑策划案（已上传的参考图会保留）"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *_refresh(pid)]

    unlock_btn.click(do_unlock, [header.project_id, logs], [logs, *refresh_outputs])

    # ================================================================ Step 2
    def make_upload(i):
        def on_upload(pid, path, prev):
            if not pid or not path:
                return [prev, *_refresh(pid)]
            p = ws.get(pid)
            plan = getattr(p, spec.attr)
            if plan is None or plan.plan_status != "confirmed" or i >= len(plan.reference_checklist):
                return [prev, *_refresh(pid)]
            role = plan.reference_checklist[i].role
            current = {a.role: a for a in plan.reference_assets}.get(role)
            if current is not None and str(current.file_path) == str(path):
                return [prev, *_refresh(pid)]
            suffix = Path(path).suffix or ".png"
            saved = rt.store.import_file(p.project_id, path, "references", dest_name=f"{spec.key}_{role}{suffix}")
            plan.upload_reference(role, saved)
            ws.save(p)
            n = len(plan.reference_checklist[i].slot_ids)
            return [_log(prev, f"📷 已上传「{role}」，自动绑定到 {n} 个槽位"), *_refresh(pid)]
        return on_upload

    for i, up in enumerate(uploads):
        up.upload(make_upload(i), [header.project_id, up, logs], [logs, *refresh_outputs],
                  api_name=f"{spec.key}_upload_ref_{i}")

    def do_reuse(pid, prev):
        p = require_project(ws, pid)
        try:
            roles = reuse_references(p, spec.attr)
            ws.save(p)
            msg = f"♻️ 已复用：{'、'.join(roles)}" if roles else "没有可复用的参考图（其他模块尚未上传同角色图片）"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *_refresh(pid)]

    reuse_btn.click(do_reuse, [header.project_id, logs], [logs, *refresh_outputs], api_name=f"{spec.key}_reuse")

    def do_single(pid, sid, path, prev):
        plan = _plan(ws, pid, spec)
        if not sid or not path:
            raise gr.Error("请选择槽位并上传图片")
        try:
            saved = rt.store.import_file(pid, path, "references", sub_id=sid)
            plan.add_slot_reference(sid, saved, role="custom")
            ws.save(ws.get(pid))
            msg = f"📎 已为槽位绑定单独参考图（该槽位实际参考图 {len(plan.references_for(sid))} 张）"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *_refresh(pid)]

    single_btn.click(do_single, [header.project_id, single_slot, single_img, logs], [logs, *refresh_outputs])

    async def do_render(pid, m, n, r, q, prev):
        p = require_project(ws, pid)
        plan = getattr(p, spec.attr)
        if plan is None:
            raise gr.Error(f"请先生成并确认{spec.label}策划案")
        if plan.plan_status != "confirmed":
            raise gr.Error("请先点击「✅ 确认策划案」后再开始生成")
        for s in plan.slots:
            plan.set_candidate_count(s.slot_id, int(n))
        log = prev
        skill = image_skill_for_model(m)
        try:
            async for ev in renderer.render_plan(p, plan, m, skill, resolution=r, quality=q):
                log = _log(log, ev.message)
                # 有新图时整体刷新（回显画廊），否则只刷新日志
                if ev.image_path or ev.done:
                    yield [log, *_refresh(pid)]
                else:
                    yield [log, *[gr.skip()] * len(refresh_outputs)]
        except Exception as e:
            log = _log(log, friendly(e))
        yield [log, *_refresh(pid)]

    render_btn.click(do_render, [header.project_id, model, cand, res, quality, logs], [logs, *refresh_outputs],
                     concurrency_id=f"{spec.key}_render", concurrency_limit=2, api_name=f"{spec.key}_render")

    def do_save_winner(pid, selected, prev):
        plan = _plan(ws, pid, spec)
        chosen = set(selected or [])
        for s in plan.slots:
            for c in s.candidates:
                c.selected = f"{s.slot_id}|{c.candidate_id}" in chosen
        ws.save(ws.get(pid))
        return [_log(prev, f"⭐ 已保存定稿勾选（{len(chosen)} 张）"), *_refresh(pid)]

    save_winner.click(do_save_winner, [header.project_id, winners, logs], [logs, *refresh_outputs])

    def do_export(pid, prev):
        p = require_project(ws, pid)
        try:
            path = export_winners(rt, p, spec.attr)
            return _log(prev, f"📦 已导出：{path.name}"), str(path)
        except Exception as e:
            return _log(prev, friendly(e)), None

    export_btn.click(do_export, [header.project_id, logs], [logs, export_file])

    # 加载 / 新建项目后刷新本选项卡
    header.load_event.then(_refresh, header.project_id, refresh_outputs)
    header.project_id.change(_refresh, header.project_id, refresh_outputs)
