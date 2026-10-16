"""顶部全局控制栏：项目管理、类目/平台联动、商品信息动态表单、Skill 管理、API 配置。"""

from __future__ import annotations

from dataclasses import dataclass

import gradio as gr

from app.engine.compliance import compliance_report
from app.engine.diagnostics import diagnose
from app.engine.project_service import create_project
from app.schemas.skill import SKILL_SLOTS
from app.settings import IMAGE_QUALITIES, IMAGE_RESOLUTIONS, save_env
from app.ui.state import UIError, Workspace, friendly

MAX_FORM_FIELDS = 8
SLOT_LABELS = {
    "ecommerce_planner": "电商策划",
    "video_scriptwriter": "视频编剧",
    "image_prompt": "生图提示词",
    "video_prompt": "视频提示词（H3）",
}


@dataclass
class Header:
    project_id: gr.State
    project_info: gr.Markdown
    category: gr.Dropdown
    platform: gr.Dropdown
    info_fields: list[gr.Textbox]
    skill_dropdowns: dict[str, gr.Dropdown]
    mode_status: gr.Markdown
    load_event: object  # 「加载项目」事件，各选项卡用 .then() 刷新自己的内容


def _field_updates(ws: Workspace, category_id: str, values: dict[str, str] | None = None) -> list:
    fields = ws.rt.category(category_id).product_form_fields
    values = values or {}
    ups = []
    for i in range(MAX_FORM_FIELDS):
        if i < len(fields):
            f = fields[i]
            ups.append(gr.update(visible=True, label=f.label, placeholder=f.placeholder, value=values.get(f.key, "")))
        else:
            ups.append(gr.update(visible=False, value=""))
    return ups


def collect_info(ws: Workspace, category_id: str, values: tuple) -> dict[str, str]:
    fields = ws.rt.category(category_id).product_form_fields
    return {f.key: (values[i] or "").strip() for i, f in enumerate(fields) if i < len(values)}


def build_header(ws: Workspace, demo: gr.Blocks) -> Header:
    rt = ws.rt
    cat_choices = [(c.name, cid) for cid, c in rt.categories.items()]
    plat_choices = [(p.name, pid) for pid, p in rt.platforms.items()]
    default_cat = "zhangjiajie_berry_tea" if "zhangjiajie_berry_tea" in rt.categories else cat_choices[0][1]
    default_plat = "taobao" if "taobao" in rt.platforms else plat_choices[0][1]
    project_id = gr.State(None)

    gr.Markdown("# 🏭 电商 AIGC 内容工厂\n主图 · 详情页 · 带货短视频（MiniMax H3 原生音画一体）")
    mode_status = gr.Markdown(rt.mode_summary())

    with gr.Row(equal_height=True):
        with gr.Column(scale=3):
            with gr.Group():
                with gr.Row():
                    project_name = gr.Textbox(label="项目名称", placeholder="例如：莓茶中秋大促", scale=2)
                    new_btn = gr.Button("➕ 新建项目", variant="primary", scale=1)
                with gr.Row():
                    history = gr.Dropdown(choices=ws.project_choices(), label="历史项目（自动保存）", scale=3)
                    refresh_hist = gr.Button("🔄", scale=0, min_width=50)
                    load_btn = gr.Button("📂 加载", scale=1)
            project_info = gr.Markdown(
                "👉 请先 **新建项目** 或 **加载历史项目**，所有内容会自动保存到 workspace/projects/")
        with gr.Column(scale=4):
            with gr.Row():
                category = gr.Dropdown(cat_choices, value=default_cat, label="类目")
                platform = gr.Dropdown(plat_choices, value=default_plat, label="目标平台")
            platform_hint = gr.Markdown()
            with gr.Accordion("📝 商品信息（随类目切换）", open=True):
                info_fields = []
                with gr.Row():
                    for i in range(MAX_FORM_FIELDS):
                        info_fields.append(gr.Textbox(visible=False, scale=1, min_width=160, key=f"info_{i}"))
                compliance = gr.Markdown()

    # ---------------- Skill 管理
    skill_dropdowns: dict[str, gr.Dropdown] = {}
    with gr.Accordion("🔌 插卡式 Skill 管理与在线微调", open=False):
        with gr.Row():
            for slot in SKILL_SLOTS:
                choices = rt.skills.choices(slot)
                skill_dropdowns[slot] = gr.Dropdown(choices, value=choices[0][1] if choices else None,
                                                    label=SLOT_LABELS[slot])
        with gr.Row():
            rescan = gr.Button("🔄 重新扫描本地 .md 卡带")
            preview_slot = gr.Radio([(SLOT_LABELS[s], s) for s in SKILL_SLOTS], value="ecommerce_planner",
                                    label="预览 / 微调哪个卡槽")
        skill_body = gr.Code(language="markdown", lines=16, label="当前 Skill 指令（可临时修改，不写回 .md 文件）")
        with gr.Row():
            apply_override = gr.Button("✍️ 应用临时覆盖")
            reset_override = gr.Button("↩️ 恢复原始指令")
        skill_status = gr.Markdown()

    # ---------------- API 配置
    s = rt.settings
    with gr.Accordion("⚙️ API 密钥与接口配置", open=False):
        gr.Markdown("Key 只保存在本机 `.env` 文件中。未填写的通道会自动使用 **演示模式**"
                    "（本地生成逼真预览，不消耗额度）。")
        demo_toggle = gr.Checkbox(value=s.demo_mode, label="🧪 本地演示测试模式（勾选后即使填了 Key 也不调用真实接口）")
        with gr.Row():
            llm_key = gr.Textbox(value=s.llm_key, label="RightAPI LLM 密钥 (RIGHTAPI_LLM_KEY)",
                                 info="填入 Claude AwsQ 或 Claude 官渠 Key", type="password")
            llm_fmt = gr.Dropdown([("自动探测", "auto"), ("Anthropic /v1/messages", "anthropic"),
                                   ("OpenAI /v1/chat/completions", "openai")], value=s.llm_api_format,
                                  label="LLM 接口格式")
        with gr.Row():
            img_key = gr.Textbox(value=s.image_key, label="RightAPI 画图通用密钥 (RIGHTAPI_IMAGE_KEY)",
                                 info="生产请填「image 高质量」渠道 Key：GPT Image 与 Nano Banana 共用，"
                                      "全模型 4K、异步",
                                 type="password", scale=3)
            img_res = gr.Dropdown(list(IMAGE_RESOLUTIONS), value=s.image_resolution, label="默认分辨率")
            img_quality = gr.Dropdown(list(IMAGE_QUALITIES), value=s.image_quality, label="GPT 默认质量档",
                                      info="xhigh/max 仅 gpt-image-2.5-*，gpt-image-2 自动降为 high")
        base_url = gr.Textbox(value=s.rightapi_base_url, label="RIGHTAPI_BASE_URL",
                              info="官方文档地址为 https://www.rightapi.ai（填 rightapi.ai 会自动补 www）")
        gr.Markdown(
            "**模型 → 接口对照（系统按模型自动选择，不会用错）**\n\n"
            "| 模型 | 协议 | 接口 |\n|---|---|---|\n"
            "| gpt-image-2.5-flare / gpt-image-2.5-sunburst / gpt-image-2 | OpenAI 生成 | "
            "无参考图 `/v1/images/generations`，有参考图 `/v1/images/edits` |\n"
            "| gemini-3-pro-image-preview / gemini-nano-banana-2.1 | Gemini 生成 | "
            "`/v1beta/models/{model}:generateContent`（文生图与编辑同一接口） |\n\n"
            "全部带 `\"async\": true` 提交，轮询 `/v1/tasks/{task_id}` 取图片 URL 后下载保存。")
        with gr.Row():
            modal_url = gr.Textbox(value=s.modal_h3_url, label="MODAL_H3_ENDPOINT_URL",
                                   placeholder="https://xxx.modal.run")
            comfy_url = gr.Textbox(value=s.comfyui_url, label="COMFYUI_URL",
                                   placeholder="https://xxx.modal.run（ComfyUI MiniMax All-in-One）")
        with gr.Row():
            save_btn = gr.Button("💾 保存配置到 .env", variant="primary")
            diag_btn = gr.Button("🩺 连通性诊断（不消耗画图额度）")
        save_status = gr.Markdown()

    # ================= 事件
    def on_category(cid, *vals):
        return [*_field_updates(ws, cid), "✅ 合规检查：未发现广告法 / 类目违禁词"]

    def on_platform(pid):
        p = rt.platform(pid)
        w = "、".join(f"{k} {v:.0%}" for k, v in p.visual_style_weights.items())
        return (f"**{p.name}**：主图默认 {p.main_image.default_count} 张（{p.main_image.default_aspect_ratio}），"
                f"详情页默认 {p.detail_page.default_screens} 屏；视觉权重：{w}")

    def on_info_change(cid, pid, *vals):
        fields = rt.category(cid).product_form_fields
        texts = {fields[i].label: v for i, v in enumerate(vals) if i < len(fields) and v}
        return compliance_report(texts, rt.category(cid), rt.platform(pid))

    category.change(on_category, [category, *info_fields], [*info_fields, compliance])
    platform.change(on_platform, platform, platform_hint)
    for f in info_fields:
        f.change(on_info_change, [category, platform, *info_fields], compliance, show_progress="hidden")

    def on_new(name, cid, pid, *vals):
        p = ws.put(create_project(rt, name, cid, pid, collect_info(ws, cid, vals)))
        return (p.project_id, _project_md(ws, p), gr.update(choices=ws.project_choices(), value=p.project_id))

    new_btn.click(on_new, [project_name, category, platform, *info_fields], [project_id, project_info, history],
                  api_name="new_project")
    refresh_hist.click(lambda: gr.update(choices=ws.project_choices()), None, history)

    def on_load(pid):
        if not pid:
            raise gr.Error("请先在下拉框选择一个历史项目")
        ws._projects.pop(pid, None)
        p = ws.get(pid)
        return (p.project_id, _project_md(ws, p), gr.update(value=p.category_id), gr.update(value=p.platform_id),
                p.name)

    load_evt = load_btn.click(on_load, history, [project_id, project_info, category, platform, project_name])
    # 加载后回填商品信息（等类目 change 完成后再填）
    load_evt = load_evt.then(lambda pid: _field_updates(ws, ws.get(pid).category_id, ws.get(pid).product_info),
                  project_id, info_fields)

    # 商品信息变化时同步到当前项目
    def sync_info(pid, cid, pfid, *vals):
        if not pid:
            return
        p = ws.get(pid)
        p.product_info = collect_info(ws, cid, vals)
        p.category_id, p.platform_id = cid, pfid
        ws.save(p)

    sync_inputs = [project_id, category, platform, *info_fields]
    for f in info_fields:
        f.blur(sync_info, sync_inputs, None)
    category.change(sync_info, sync_inputs, None)
    platform.change(sync_info, sync_inputs, None)

    # Skill 管理
    def show_skill(slot, *ids):
        sid = dict(zip(SKILL_SLOTS, ids, strict=True))[slot]
        return rt.skills.get(slot, sid).body if sid else ""

    def do_rescan(*ids):
        rt.skills.refresh()
        ups = []
        for slot, cur in zip(SKILL_SLOTS, ids, strict=True):
            ch = rt.skills.choices(slot)
            vals = [c[1] for c in ch]
            ups.append(gr.update(choices=ch, value=cur if cur in vals else (vals[0] if vals else None)))
        errs = "\n".join(f"- ⚠️ {e}" for e in rt.skills.errors.values())
        total = sum(len(rt.skills.list(s)) for s in SKILL_SLOTS)
        return [*ups, f"✅ 已扫描到 {total} 张卡带" + (f"\n{errs}" if errs else "")]

    def do_apply(slot, body, *ids):
        sid = dict(zip(SKILL_SLOTS, ids, strict=True))[slot]
        rt.skills.set_override(slot, sid, body)
        return f"✅ 已对「{sid}」应用临时覆盖（本次运行有效）"

    def do_reset(slot, *ids):
        sid = dict(zip(SKILL_SLOTS, ids, strict=True))[slot]
        rt.skills.clear_override(slot, sid)
        return rt.skills.get(slot, sid).body, "↩️ 已恢复原始指令"

    dd = list(skill_dropdowns.values())
    preview_slot.change(show_skill, [preview_slot, *dd], skill_body)
    for d in dd:
        d.change(show_skill, [preview_slot, *dd], skill_body)
    rescan.click(do_rescan, dd, [*dd, skill_status])
    apply_override.click(do_apply, [preview_slot, skill_body, *dd], skill_status)
    reset_override.click(do_reset, [preview_slot, *dd], [skill_body, skill_status])

    def do_save(demo, k1, fmt, k2, res, q, burl, murl, curl):
        try:
            new = save_env({
                "RIGHTAPI_LLM_KEY": k1, "RIGHTAPI_LLM_API_FORMAT": fmt, "RIGHTAPI_IMAGE_KEY": k2,
                "RIGHTAPI_IMAGE_RESOLUTION": res, "RIGHTAPI_IMAGE_QUALITY": q, "RIGHTAPI_BASE_URL": burl,
                "MODAL_H3_ENDPOINT_URL": murl, "COMFYUI_URL": curl,
                "DEMO_MODE": "true" if demo else "false",
            })
        except (ValueError, OSError) as e:
            return friendly(e), rt.mode_summary()
        rt.update_settings(new)
        rt.force_demo = bool(demo)
        return f"✅ 已保存到 .env 并立即生效（Base URL：`{new.rightapi_base_url}`）", rt.mode_summary()

    save_btn.click(do_save, [demo_toggle, llm_key, llm_fmt, img_key, img_res, img_quality, base_url, modal_url,
                             comfy_url], [save_status, mode_status])

    async def do_diag():
        yield "🩺 正在检测连通性…"
        report = await diagnose(rt.settings)
        yield report

    diag_btn.click(do_diag, None, save_status, concurrency_id="diagnose")

    def toggle_demo(demo):
        rt.force_demo = bool(demo)
        return rt.mode_summary()

    demo_toggle.change(toggle_demo, demo_toggle, mode_status)

    # 页面打开时初始化动态表单、平台提示与 Skill 预览
    demo.load(on_category, [category, *info_fields], [*info_fields, compliance])
    demo.load(on_platform, platform, platform_hint)
    demo.load(show_skill, [preview_slot, *dd], skill_body)
    demo.load(lambda: (rt.mode_summary(), gr.update(choices=ws.project_choices())), None, [mode_status, history])

    return Header(project_id, project_info, category, platform, info_fields, skill_dropdowns, mode_status,
                  load_evt)


def _project_md(ws: Workspace, p) -> str:
    cat, plat = ws.rt.category(p.category_id), ws.rt.platform(p.platform_id)
    done = []
    for attr, label in (("main_image_plan", "主图"), ("detail_page_plan", "详情页"), ("video_script", "视频")):
        plan = getattr(p, attr)
        if plan is not None:
            done.append(f"{label}（{'已确认' if plan.plan_status == 'confirmed' else '草稿'}）")
    status = "、".join(done) or "尚未开始"
    return (f"📁 当前项目：**{p.name}**　`{p.project_id}`　｜　{cat.name} · {plat.name}　｜　进度：{status}\n\n"
            f"保存位置：`workspace/projects/{p.project_id}/`")


def require_project(ws: Workspace, project_id: str | None):
    try:
        return ws.get(project_id)
    except UIError as e:
        raise gr.Error(str(e)) from e
