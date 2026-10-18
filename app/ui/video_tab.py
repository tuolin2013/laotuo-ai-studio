"""【带货视频】选项卡：Step 1 分镜剧本（原创 / 克隆参考爆款视频 二选一）-> Step 2 首帧锁定 -> Step 3 H3 与视频。

两种策划方式产出同一个 VideoScript，填入同一个可编辑分镜表，之后的确认、首帧、H3、视频流程完全共用：
- 📝 自己策划原创剧本：一键生成分镜；或在「进阶」里走 创意大纲 → 完整剧本 → 分镜脚本 三步式专业编剧；
- 📹 克隆参考爆款视频：本地抽取关键帧 → hypit_viral_clone 卡带拆解原片 → 按原片结构替换为本品。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import gradio as gr

from app.engine.concept_engine import ConceptEngine
from app.engine.project_service import reuse_references
from app.engine.prompt_compiler import PromptCompiler, global_continuous_prompt, image_skill_for_model
from app.engine.render_engine import RenderEngine
from app.engine.script_engine import CLONE_VIDEO_TYPE, ScriptEngine
from app.engine.story_engine import StoryEngine
from app.engine.video_cloner import (
    MAX_FRAMES,
    MIN_FRAMES,
    VIDEO_EXTENSIONS,
    VideoCloneError,
    extract_keyframes,
    validate_video_file,
)
from app.providers.image_models import choices as model_choices
from app.schemas.common import new_id
from app.schemas.video import (
    CLONE_STRATEGY_LABELS,
    H3_MODE_LABELS,
    VIDEO_TYPE_LABELS,
    ReferenceVideoSource,
    VideoScript,
)
from app.settings import IMAGE_RESOLUTIONS
from app.ui.header import Header, collect_info, require_project
from app.ui.image_tab import MAX_REF_ROLES
from app.ui.state import (
    AUDIO_CHOICES,
    SHOT_HEADERS,
    Workspace,
    apply_shot_rows,
    friendly,
    log_line,
    new_blank_shot,
    shots_to_rows,
)

VIDEO_RATIOS = ["9:16", "16:9", "1:1"]
SCRIPT_MODES = [("📝 自己策划原创剧本", "original"), ("📹 克隆参考爆款视频 (Hypit Clone)", "clone")]
CLONE_STRATEGIES = [(label, key) for key, label in CLONE_STRATEGY_LABELS.items()]
H3_MODE_CHOICES = [("模式 A：I2VA / FL2VA 首帧模式（默认 · base-en.txt 三段式）", "i2va"),
                   ("模式 B：Ref2VA 全参考模式（ref-en.txt 六段式 · <Picture N> 标签）", "ref2va"),
                   ("A + B 同时生成（视频生成使用模式 A）", "both")]
PROVIDERS = [("Modal MiniMax H3", "modal_minimax_h3"), ("fal.ai（预留）", "fal_ai"),
             ("ComfyUI 手动桥接", "manual_comfyui")]


def _script(ws: Workspace, pid) -> VideoScript:
    p = require_project(ws, pid)
    if p.video_script is None:
        raise gr.Error("请先在 Step 1 生成分镜剧本（原创策划或克隆参考视频）")
    return p.video_script


def _concept_md(ws: Workspace, pid) -> str:
    p = ws.get(pid) if pid else None
    if p is None or p.video_concept is None:
        return "📝 尚未生成创意大纲"
    c = p.video_concept
    vtype = VIDEO_TYPE_LABELS.get(c.video_type, c.video_type)
    return (f"### 创意大纲\n\n"
            f"**视频类型**: {vtype}  \n"
            f"**比例**: {c.aspect_ratio}  \n"
            f"**时长**: {c.target_duration_seconds}秒\n\n"
            f"**钩子设计**: {c.hook_design}\n\n"
            f"**核心信息**: {c.core_message}\n\n"
            f"**情感曲线**: {c.emotional_arc}\n\n"
            f"**行动号召**: {c.call_to_action}\n\n"
            f"**场景规划** ({c.scenes_count}个场景):\n" +
            "\n".join(f"{i}. {s}" for i, s in enumerate(c.scenes_outline, start=1)))


def _story_md(ws: Workspace, pid) -> str:
    p = ws.get(pid) if pid else None
    if p is None or p.video_story is None:
        return "尚未生成剧本"
    s = p.video_story
    status = "🔒 **已确认**" if s.plan_status == "confirmed" else "📝 **草稿（可编辑）**"
    return (f"{status}\n\n"
            f"**标题**: {s.title}  \n"
            f"**梗概**: {s.logline}\n\n"
            f"**人物**: {' / '.join(s.characters) if s.characters else '无'}  \n"
            f"**场景**: {' / '.join(s.locations) if s.locations else '无'}  \n"
            f"**道具**: {' / '.join(s.props) if s.props else '无'}")


def _source_line(s: VideoScript) -> str:
    if s.script_mode == "clone" and s.reference_video is not None:
        label = CLONE_STRATEGY_LABELS.get(s.clone_strategy or "", "")
        return f"来源：📹 克隆「{s.reference_video.file_name}」（{label}）"
    return "来源：📝 原创策划"


def _status_md(s: VideoScript | None) -> str:
    if s is None:
        return "尚未生成分镜剧本"
    head = (f"{_source_line(s)}　｜　共 {len(s.shots)} 镜，约 {s.total_duration_seconds:g} 秒，"
            f"比例 **{s.aspect_ratio}**　｜　{s.script_summary}")
    if s.plan_status == "draft":
        return (f"📝 **分镜剧本草稿（可编辑）**　{head}\n\n声音模式可填：{' / '.join(AUDIO_CHOICES)}。"
                "MiniMax H3 原生带声音：说话请写进「人物同期声对白」，**不要写画外音**。")
    locked = sum(1 for x in s.shots if x.confirmed_first_frame_id)
    prompts = sum(1 for x in s.shots if x.h3_prompt)
    modes = sorted({H3_MODE_LABELS[x.h3_prompt_mode] for x in s.shots if x.h3_prompt and x.h3_prompt_variants})
    return (f"🔒 **分镜剧本已确认**　{head}\n\n首帧已锁定 {locked}/{len(s.shots)}　｜　"
            f"H3 提示词已生成 {prompts}/{len(s.shots)}" + (f"（视频生成使用：{'、'.join(modes)}）" if modes else ""))


def _checklist_md(s: VideoScript | None) -> str:
    if s is None or s.plan_status != "confirmed":
        return "确认分镜剧本后，这里会显示首帧所需参考图汇总清单"
    if not s.reference_checklist:
        return "📋 所有首帧均为文生图，无需上传参考图"
    uploaded = {a.role for a in s.reference_assets}
    index = {x.shot_id: i for i, x in enumerate(s.shots, start=1)}
    lines = ["| 状态 | 参考图角色 | 拍摄要求 | 用于镜头 |", "|---|---|---|---|"]
    for c in s.reference_checklist:
        mark = "✅ 已上传" if c.role in uploaded else "⬜ 待上传"
        lines.append(f"| {mark} | `{c.role}` | {c.description or '-'} | "
                     f"{'、'.join(f'镜头{index[i]}' for i in c.slot_ids if i in index)} |")
    return "\n".join(lines)


def _shot_choices(s: VideoScript | None) -> list[tuple[str, str]]:
    if s is None:
        return []
    out = []
    for i, x in enumerate(s.shots, start=1):
        lock = "🔒" if x.confirmed_first_frame_id else "⬜"
        out.append((f"{lock} 镜头 {i}（{x.duration_seconds:g}s · {x.audio_mode}）", x.shot_id))
    return out


def _frames_gallery(s: VideoScript | None) -> list[tuple[str, str]]:
    if s is None:
        return []
    out = []
    for i, x in enumerate(s.shots, start=1):
        for j, f in enumerate(x.first_frame_candidates, start=1):
            if f.file_path.exists():
                mark = "🔒 已锁定 · " if f.frame_id == x.confirmed_first_frame_id else ""
                out.append((str(f.file_path), f"{mark}镜头 {i} · 首帧候选 {j} · {f.aspect_ratio}"))
    return out


def _locked_frame_choices(s: VideoScript | None) -> tuple[list[tuple[str, str]], list[str]]:
    """所有首帧候选的 CheckboxGroup 选项（shot_id|frame_id）与已锁定项。"""
    if s is None:
        return [], []
    choices, selected = [], []
    for i, shot in enumerate(s.shots, start=1):
        for j, f in enumerate(shot.first_frame_candidates, start=1):
            key = f"{shot.shot_id}|{f.frame_id}"
            choices.append((f"镜头 {i} · 候选 {j}（{f.model}）", key))
            if f.frame_id == shot.confirmed_first_frame_id:
                selected.append(key)
    return choices, selected


def _clone_gallery(source: ReferenceVideoSource | None) -> list[tuple[str, str]]:
    if source is None:
        return []
    return [(str(f.file_path), f"⏱ {f.timestamp} · 关键帧 {f.index}") for f in source.frames if f.file_path.exists()]


def _clone_md(s: VideoScript | None, source: ReferenceVideoSource | None) -> str:
    if source is None:
        return ""
    head = (f"**原片**：{source.file_name}　｜　时长 {source.duration_seconds:g} 秒　｜　"
            f"{source.width}×{source.height}（≈{source.aspect_ratio}）　｜　关键帧 {len(source.frames)} 张："
            + "、".join(f.timestamp for f in source.frames))
    if s is None or s.script_mode != "clone" or s.clone_analysis is None:
        return head + "\n\n⏳ 正在拆解原片并生成克隆分镜…"
    mapping = "、".join(f"镜头 {i} ← {x.source_timestamp}" for i, x in enumerate(s.shots, start=1)
                       if x.source_timestamp)
    parts = [head, s.clone_analysis.to_markdown()]
    if mapping:
        parts.append(f"**克隆分镜对应原片时间点**：{mapping}")
    return "\n\n".join(parts)


def build_video_tab(ws: Workspace, header: Header) -> None:
    rt = ws.rt
    concept_engine, story_engine = ConceptEngine(rt), StoryEngine(rt)
    writer, compiler, renderer = ScriptEngine(rt), PromptCompiler(rt), RenderEngine(rt)

    with gr.Row():
        with gr.Column(scale=7):
            # ========================================================== Step 1: 分镜剧本
            gr.Markdown("## Step 1　带货视频分镜剧本")
            script_mode = gr.Radio(SCRIPT_MODES, value="original", label="策划方式（二选一）",
                                   info="两种方式生成的剧本都填入下方同一个可编辑分镜表，"
                                        "之后的确认剧本 → 锁首帧 → H3 提示词 → 生成视频流程完全一样")
            with gr.Column(visible=True) as original_box, gr.Row():
                vtype = gr.Dropdown([(v, k) for k, v in VIDEO_TYPE_LABELS.items()], value="product_showcase",
                                    label="视频类型（内容形式）")
                shots_n = gr.Slider(3, 8, value=5, step=1, label="分镜数量")
            with gr.Column(visible=False) as clone_box:
                clone_file = gr.File(label="📥 上传参考爆款视频（.mp4 / .mov / .webm）", type="filepath",
                                     file_types=list(VIDEO_EXTENSIONS))
                with gr.Row():
                    clone_strategy = gr.Radio(CLONE_STRATEGIES, value="replicate", label="克隆策略")
                    clone_frames = gr.Slider(MIN_FRAMES, MAX_FRAMES, value=6, step=1,
                                             label="抽取关键帧数量（= 克隆分镜数）")
            vratio = gr.Dropdown(VIDEO_RATIOS, value="9:16", label="视频比例（首帧强制同步）")
            idea = gr.Textbox(label="💡 我的视频策划/克隆想法（选填）", lines=2,
                              placeholder="例如：办公室女生冲一杯，看到白霜惊喜说话；"
                                          "克隆时可写：人设换成宝妈、保留原片的开箱钩子")
            gen_btn = gr.Button("✨ 生成带货视频剧本", variant="primary")

            with gr.Column(visible=False) as clone_result:
                gr.Markdown("#### 🎞️ 原视频关键帧时间轴预览")
                clone_gallery = gr.Gallery(show_label=False, columns=4, height=260, object_fit="contain")
                gr.Markdown("#### 🔍 原爆款视频拆解分析")
                clone_md = gr.Markdown()

            with gr.Accordion("🧭 进阶（原创）：三步式专业编剧　创意大纲 → 完整剧本 → 分镜脚本", open=False) as pro_box:
                gr.Markdown("使用上方的视频类型、分镜数量、视频比例与想法；第 ③ 步编译出的分镜同样填入下方分镜表。")
                vdur = gr.Slider(5, 60, value=15, step=1, label="目标时长(秒)")
                concept_btn = gr.Button("① 生成创意大纲（30 秒电梯演讲）")
                concept_display = gr.Markdown(_concept_md(ws, None))
                concept_confirm = gr.Button("② ✅ 确认创意大纲，生成完整剧本")
                story_status = gr.Markdown(_story_md(ws, None))
                story_content = gr.Textbox(label="📜 剧本正文（Markdown，可编辑）", lines=12, max_lines=30)
                with gr.Row():
                    story_save = gr.Button("💾 保存剧本修改")
                    story_regen = gr.Button("🔄 重新生成剧本")
                    story_confirm = gr.Button("✅ 确认剧本")
                    story_unlock = gr.Button("🔓 解锁剧本")
                storyboard_btn = gr.Button("③ 🎬 把已确认剧本编译为分镜（填入下方分镜表）", variant="primary")

            status = gr.Markdown("尚未生成分镜剧本")
            table = gr.Dataframe(headers=SHOT_HEADERS, interactive=True, wrap=True, static_columns=[0],
                                 datatype=["number", "number", *["str"] * 10],
                                 label="可编辑分镜表（含人物同期声对白与原生环境音，无画外音）")
            with gr.Row():
                save_edit = gr.Button("💾 保存分镜修改")
                shot_pick = gr.Dropdown(label="选择分镜", choices=[], scale=2)
                regen_shot = gr.Button("🎲 单独重写该分镜")
            with gr.Row():
                add_shot = gr.Button("➕ 新增分镜")
                del_shot = gr.Button("🗑️ 删除所选分镜")
                up_shot = gr.Button("⬆️ 上移")
                down_shot = gr.Button("⬇️ 下移")
            with gr.Row():
                confirm_btn = gr.Button("✅ 确认分镜剧本", variant="primary")
                unlock_btn = gr.Button("🔓 解锁返回修改分镜剧本")

            # ========================================================== Step 2: 首帧锁定
            gr.Markdown("## Step 2　首帧定妆图生成与防变形锁死")
            gr.Markdown("**工作流**：① 按汇总清单上传参考图 → ② 生成候选首帧 → ③ 逐镜勾选商品未变形的首帧并锁定")
            with gr.Accordion("2.1　按汇总清单上传首帧所需参考图", open=True):
                checklist = gr.Markdown(_checklist_md(None))
                reuse_btn = gr.Button("♻️ 一键复用项目已上传的同角色参考图（主图 / 详情页）")
                uploads = []
                with gr.Row():
                    for i in range(MAX_REF_ROLES):
                        uploads.append(gr.Image(type="filepath", visible=False, height=170, key=f"video_up_{i}",
                                                sources=["upload", "clipboard"]))
            with gr.Accordion("2.2　批量生成所有分镜的静态首帧候选图", open=True):
                with gr.Row():
                    frame_model = gr.Dropdown(model_choices(rt.settings.image_models),
                                              value=rt.settings.image_models[0], label="首帧生图模型")
                    frame_res = gr.Dropdown(list(IMAGE_RESOLUTIONS), value=rt.settings.image_resolution,
                                            label="分辨率")
                    frame_n = gr.Slider(1, 4, value=2, step=1, label="每镜生成几张候选")
                frame_btn = gr.Button("🖼️ 开始批量生成各分镜首帧候选图", variant="primary", size="lg")
            with gr.Accordion("2.3　预览候选首帧，逐镜勾选并锁定商品未变形的图", open=True):
                gr.Markdown("**防变形检查**：仔细查看商品包装、文字、LOGO 是否保持原样，无扭曲或错字。"
                            "锁定后该首帧会作为视频起始帧送给 H3 模型")
                frames = gr.Gallery(label="所有分镜的首帧候选（🔒 = 已锁定）", columns=4, height=360,
                                    object_fit="contain")
                locked_frames = gr.CheckboxGroup(label="🔒 勾选锁定各分镜的首帧", choices=[])
                with gr.Row():
                    save_locked = gr.Button("🔒 保存首帧锁定勾选", variant="primary")
                    lock_all_btn = gr.Button("🔒 全部分镜自动锁定最新首帧（快捷操作）", variant="secondary")

            # ========================================================== Step 3: H3 提示词 + 视频生成
            gr.Markdown("## Step 3　MiniMax H3 音画提示词 · ComfyUI 协同 · 视频生成")
            h3_mode = gr.Radio(H3_MODE_CHOICES, value="i2va", label="官方 h3-prompt-writing 输入模式",
                               info="I2VA：以 Step 2 锁定首帧为 <Picture 1> 起始帧；Ref2VA：首帧 + 商品参考图"
                                    "按 <Picture 1>、<Picture 2>… 全参考输入。"
                                    "画面/声音描述为英文，中文台词与包装文字保留原文")
            h3_btn = gr.Button("🪄 运行 H3 Skill 生成专用音画提示词（全部已锁定首帧的分镜）", variant="primary")
            with gr.Row():
                edit_shot = gr.Dropdown(label="微调哪个分镜的 H3 提示词", choices=[])
                edit_mode = gr.Radio(H3_MODE_CHOICES[:2], value="i2va", label="查看 / 微调哪个模式的版本")
                save_h3 = gr.Button("💾 保存微调并设为视频生成使用的版本")
            h3_edit = gr.Textbox(label="H3 提示词（可手动微调）", lines=5, buttons=["copy"])

            with gr.Group(elem_classes=["comfy-zone"]):
                gr.Markdown("#### 🌟 Modal ComfyUI MiniMax All-in-One 协同专区")
                comfy_link = gr.Markdown()
                shot1_prompt = gr.Textbox(label="① 第 1 分镜 H3 专用提示词（点右上角一键复制）", lines=5,
                                          buttons=["copy"], interactive=False)
                global_prompt = gr.Textbox(label="② All-in-One 全局连续 H3 提示词（基于第 1 分镜首帧，一键复制）",
                                           lines=8, buttons=["copy"], interactive=False)
                with gr.Row():
                    frame1_dl = gr.DownloadButton("⬇️ 下载第 1 分镜锁定首帧原图", value=None)
                    all_prompts_dl = gr.DownloadButton("⬇️ 下载全部分镜 H3 提示词（txt）", value=None)
                with gr.Row():
                    back_shot = gr.Dropdown(label="回传绑定到分镜", choices=[])
                    back_video = gr.File(label="📤 上传从 ComfyUI 生成的视频", file_types=["video"])
                back_btn = gr.Button("📤 回传绑定视频")

            gr.Markdown("#### 🎬 直接生成视频")
            with gr.Row():
                provider = gr.Dropdown(PROVIDERS, value="modal_minimax_h3", label="视频后端")
                video_shot = gr.Dropdown(label="生成哪个分镜", choices=[])
            with gr.Row():
                warm_btn = gr.Button("🔎 检查 Modal H3 服务（不启动 GPU）")
                video_btn = gr.Button("🎬 开始生成带声视频", variant="primary")
                video_all_btn = gr.Button("🎬 全部分镜依次生成")

        with gr.Column(scale=5):
            logs = gr.Textbox(label="📜 运行日志 Running Logs", lines=16, max_lines=16, autoscroll=True,
                              interactive=False)
            player = gr.Video(label="🎞️ 视频预览（带原生声音）", height=420, autoplay=False)
            video_list = gr.Markdown()

    # ================================================================ 刷新
    def _refresh(pid, sel_shot=None, preview: ReferenceVideoSource | None = None):
        """preview：克隆过程中刚抽完帧、剧本还没生成时，先展示这段参考视频的关键帧。"""
        p = ws.get(pid) if pid else None
        s = p.video_script if p else None
        ch = _shot_choices(s)
        default = sel_shot if sel_shot in [c[1] for c in ch] else (ch[0][1] if ch else None)
        locked_ch, locked_sel = _locked_frame_choices(s)
        items = s.reference_checklist if s is not None and s.plan_status == "confirmed" else []
        assets = {a.role: a for a in (s.reference_assets if s else [])}
        up = []
        for i in range(MAX_REF_ROLES):
            if i < len(items):
                a = assets.get(items[i].role)
                up.append(gr.update(visible=True, label=f"📷 {items[i].role}：{items[i].description}",
                                    value=str(a.file_path) if a else None))
            else:
                up.append(gr.update(visible=False, value=None))
        first = s.shots[0] if s and s.shots else None
        frame1 = first.confirmed_first_frame if first else None
        prompts_file = None
        if s and any(x.h3_prompt for x in s.shots):
            prompts_file = rt.store.asset_dir(p.project_id, "prompts") / "h3_prompts_all.txt"
            prompts_file.write_text(s.combined_h3_prompts() + "\n\n【All-in-One 全局连续提示词】\n" +
                                    global_continuous_prompt(s), encoding="utf-8")
        latest_video = None
        vids = []
        for i, x in enumerate(s.shots if s else [], start=1):
            for v in x.videos:
                vids.append(f"- 镜头 {i}：`{v.file_path.name}`（{v.provider} · "
                            f"{'外部回传' if v.source == 'external_upload' else '生成'}）")
                latest_video = v.file_path
        url = rt.settings.comfyui_url
        link = (f"🔗 [打开 ComfyUI MiniMax All-in-One]({url})" if url else
                "未配置 COMFYUI_URL（可在「⚙️ API 配置」填写）。操作：下载首帧 → 粘贴提示词 → 生成 → 回传视频")
        shot_up = gr.update(choices=ch, value=default)
        source = preview or (s.reference_video if s is not None and s.script_mode == "clone" else None)
        story_text = p.video_story.content if p and p.video_story else ""
        return [
            _status_md(s), shots_to_rows(s), shot_up, _checklist_md(s), *up, _frames_gallery(s),
            gr.update(choices=locked_ch, value=locked_sel),
            shot_up,
            (s.get_shot(default).h3_prompt or "") if s and default else "",
            link, (first.h3_prompt or "（请先锁定第 1 分镜首帧并运行 H3 Skill）") if first else "",
            global_continuous_prompt(s) if s and s.plan_status == "confirmed" else "",
            gr.update(value=str(frame1.file_path) if frame1 else None),
            gr.update(value=str(prompts_file) if prompts_file else None),
            shot_up, shot_up,
            str(latest_video) if latest_video and latest_video.exists() else None,
            "\n".join(vids) or "尚无视频",
            _concept_md(ws, pid), _story_md(ws, pid), story_text,
            gr.update(visible=source is not None), _clone_gallery(source),
            _clone_md(None if preview else s, source),
        ]

    # 新增输出统一追加在末尾，保持前面各输出的位置不变（端到端测试按位置读取）
    refresh_outputs = [status, table, shot_pick, checklist, *uploads, frames, locked_frames, edit_shot, h3_edit,
                       comfy_link, shot1_prompt, global_prompt, frame1_dl, all_prompts_dl, back_shot, video_shot,
                       player, video_list, concept_display, story_status, story_content, clone_result,
                       clone_gallery, clone_md]

    def _log(prev, msg):
        return (prev + "\n" if prev else "") + log_line(msg)

    def _idea_line(text):
        return "我的视频策划/克隆想法：" + (text.strip() if text and text.strip() else "（未填写）")

    # ================================================================ Step 1: 二选一
    def on_mode(m):
        original = m != "clone"
        return [gr.update(visible=original), gr.update(visible=not original), gr.update(visible=original)]

    script_mode.change(on_mode, script_mode, [original_box, clone_box, pro_box])

    async def _original_stream(p, cid, pfid, vt, vr, n, idea_text, info, skill_id, log):
        pid = p.project_id
        sid = writer.resolve_skill_id("original", skill_id)
        log = _log(log, f"📝 开始原创策划：{VIDEO_TYPE_LABELS[vt]} · {vr} · {int(n)} 镜 · Skill {sid}")
        yield [log, *_refresh(pid)]
        try:
            async with ws.lock(pid):
                script, mode = await writer.generate(
                    category_id=cid, platform_id=pfid, video_type=vt, aspect_ratio=vr, shot_count=int(n),
                    user_creative_idea=idea_text, product_info=info, skill_id=sid, existing=p.video_script)
                p.video_script = script
                ws.save(p)
        except Exception as e:
            yield [_log(log, friendly(e)), *_refresh(pid)]
            return
        talk = sum(1 for x in script.shots if x.character_dialogue)
        log = _log(log, f"✅ 原创剧本完成（{mode}）：{len(script.shots)} 镜，{talk} 镜含人物同期声对白，"
                        "其余为原生环境音；全片无画外音")
        yield [log, *_refresh(pid)]

    async def _clone_stream(p, cid, pfid, vr, idea_text, info, skill_id, video_file, strategy, n_frames, log):
        pid = p.project_id
        if not video_file:
            raise gr.Error("请先上传参考爆款视频（.mp4 / .mov / .webm）")
        src = Path(video_file if isinstance(video_file, str) else video_file.name)
        try:
            validate_video_file(src)
        except VideoCloneError as e:
            raise gr.Error(str(e)) from e
        sid = writer.resolve_skill_id("clone", skill_id)
        log = _log(log, f"📥 已接收参考视频：{src.name}（{src.stat().st_size / 1024 / 1024:.1f} MB）"
                        f"　｜　克隆策略：{CLONE_STRATEGY_LABELS[strategy]}")
        log = _log(log, f"🎞️ 正在本地抽取 {int(n_frames)} 张关键帧…")
        yield [log, *_refresh(pid)]
        try:
            vid = new_id("refvid")
            saved = await asyncio.to_thread(rt.store.import_file, pid, src, "reference_video_frames",
                                            sub_id=vid, dest_name=f"source{src.suffix.lower()}")
            source = await extract_keyframes(saved, rt.store.asset_dir(pid, "reference_video_frames", vid),
                                             int(n_frames), file_name=src.name)
            source.video_id = vid
            p.video_reference = source
            ws.save(p)
        except Exception as e:
            yield [_log(log, friendly(e)), *_refresh(pid)]
            return
        log = _log(log, f"✅ 抽帧完成：时长 {source.duration_seconds:g} 秒 · {source.width}×{source.height}"
                        f"（≈{source.aspect_ratio}）· 关键帧 " + "、".join(f.timestamp for f in source.frames))
        if source.aspect_ratio != vr:
            log = _log(log, f"⚠️ 原片约为 {source.aspect_ratio}，输出视频比例为 {vr}：分镜构图按 {vr} 重新安排")
        log = _log(log, f"🔍 正在拆解原片钩子 / 切镜节奏 / A-B Roll，并替换为本品（Skill {sid}）…")
        yield [log, *_refresh(pid, preview=source)]
        try:
            async with ws.lock(pid):
                script, mode = await writer.generate(
                    category_id=cid, platform_id=pfid, video_type=CLONE_VIDEO_TYPE, aspect_ratio=vr,
                    shot_count=len(source.frames), user_creative_idea=idea_text, product_info=info,
                    skill_id=sid, existing=p.video_script, mode="clone", reference_video=source,
                    clone_strategy=strategy)
                p.video_script = script
                ws.save(p)
        except Exception as e:
            yield [_log(log, friendly(e)), *_refresh(pid, preview=source)]
            return
        talk = sum(1 for x in script.shots if x.character_dialogue)
        log = _log(log, f"✅ 克隆剧本完成（{mode}）：{len(script.shots)} 镜，{talk} 镜含人物同期声对白，"
                        "全片无画外音；已填入下方分镜表，可直接修改后确认")
        yield [log, *_refresh(pid)]

    async def do_generate(pid, cid, pfid, mode, vt, vr, n, idea_text, skill_id, video_file, strategy, n_frames,
                          prev, *info_vals):
        p = require_project(ws, pid)
        info = collect_info(ws, cid, info_vals)
        p.category_id, p.platform_id, p.product_info = cid, pfid, info
        if p.video_script is not None and p.video_script.plan_status == "confirmed":
            raise gr.Error("分镜剧本已确认锁定，请先点「🔓 解锁返回修改分镜剧本」")
        log = _log(prev, _idea_line(idea_text))
        if mode == "clone":
            stream = _clone_stream(p, cid, pfid, vr, idea_text, info, skill_id, video_file, strategy, n_frames, log)
        else:
            stream = _original_stream(p, cid, pfid, vt, vr, n, idea_text, info, skill_id, log)
        async for out in stream:
            yield out

    gen_btn.click(do_generate,
                  [header.project_id, header.category, header.platform, script_mode, vtype, vratio, shots_n, idea,
                   header.skill_dropdowns["video_scriptwriter"], clone_file, clone_strategy, clone_frames, logs,
                   *header.info_fields],
                  [logs, *refresh_outputs], concurrency_id="video_tab", concurrency_limit=2,
                  api_name="video_generate")

    # ---------------------------------------------------------------- 进阶：创意大纲 → 剧本 → 分镜
    async def do_concept(pid, cid, pfid, vt, vr, vd, idea_text, skill_id, prev, *info_vals):
        p = require_project(ws, pid)
        info = collect_info(ws, cid, info_vals)
        p.category_id, p.platform_id, p.product_info = cid, pfid, info
        log = _log(prev, f"① 开始生成创意大纲：{VIDEO_TYPE_LABELS[vt]} · {vr} · {int(vd)}秒")
        log = _log(log, _idea_line(idea_text))
        yield [log, *_refresh(pid)]
        try:
            async with ws.lock(p.project_id):
                concept, mode = await concept_engine.generate(
                    category_id=cid, platform_id=pfid, video_type=vt, aspect_ratio=vr,
                    target_duration_seconds=int(vd), user_creative_idea=idea_text,
                    product_info=info, skill_id=skill_id or "default_concept_writer")
                p.video_concept = concept
                ws.save(p)
        except Exception as e:
            yield [_log(log, friendly(e)), *_refresh(pid)]
            return
        yield [_log(log, f"✅ 创意大纲完成（{mode}），确认后点「② 生成完整剧本」"), *_refresh(pid)]

    concept_btn.click(do_concept, [header.project_id, header.category, header.platform, vtype, vratio, vdur, idea,
                                   header.skill_dropdowns["concept_writer"], logs, *header.info_fields],
                      [logs, *refresh_outputs], concurrency_id="video_tab", api_name="video_concept")

    async def do_story(pid, cid, skill_id, prev, *info_vals):
        p = require_project(ws, pid)
        if p.video_concept is None:
            raise gr.Error("请先生成创意大纲")
        if p.video_story is not None and p.video_story.plan_status == "confirmed":
            raise gr.Error("剧本已确认，请先点「🔓 解锁剧本」再重新生成")
        info = collect_info(ws, cid, info_vals)
        log = _log(prev, f"② 已确认创意大纲，开始剧本创作：{VIDEO_TYPE_LABELS[p.video_concept.video_type]}")
        yield [log, *_refresh(pid)]
        try:
            async with ws.lock(p.project_id):
                story, mode = await story_engine.generate(
                    concept=p.video_concept, category_id=cid, platform_id=p.platform_id,
                    product_info=info, skill_id=skill_id)
                p.video_story = story
                ws.save(p)
        except Exception as e:
            yield [_log(log, friendly(e)), *_refresh(pid)]
            return
        log = _log(log, f"✅ 剧本创作完成（{mode}）：《{story.title}》")
        log = _log(log, f"人物：{'、'.join(story.characters) or '无'}　场景：{'、'.join(story.locations) or '无'}")
        yield [log, *_refresh(pid)]

    story_in = [header.project_id, header.category, header.skill_dropdowns["story_scriptwriter"], logs,
                *header.info_fields]
    concept_confirm.click(do_story, story_in, [logs, *refresh_outputs], concurrency_id="video_tab",
                          api_name="video_story")
    story_regen.click(do_story, story_in, [logs, *refresh_outputs], concurrency_id="video_tab")

    def do_story_save(pid, content, prev):
        p = require_project(ws, pid)
        if p.video_story is None:
            raise gr.Error("请先生成剧本")
        if p.video_story.plan_status == "confirmed":
            raise gr.Error("剧本已确认，请先解锁")
        p.video_story.content = content
        ws.save(p)
        return [_log(prev, "💾 已保存剧本修改"), *_refresh(pid)]

    story_save.click(do_story_save, [header.project_id, story_content, logs], [logs, *refresh_outputs])

    def _story_state(confirm: bool):
        def handler(pid, prev):
            p = require_project(ws, pid)
            if p.video_story is None:
                raise gr.Error("请先生成剧本")
            try:
                if confirm:
                    p.video_story.confirm()
                else:
                    p.video_story.unlock()
                ws.save(p)
                msg = "✅ 剧本已确认，可以点「③ 编译为分镜」" if confirm else "🔓 已解锁剧本，可以继续修改"
            except ValueError as e:
                msg = friendly(e)
            return [_log(prev, msg), *_refresh(pid)]
        return handler

    story_confirm.click(_story_state(True), [header.project_id, logs], [logs, *refresh_outputs],
                        api_name="video_story_confirm")
    story_unlock.click(_story_state(False), [header.project_id, logs], [logs, *refresh_outputs])

    async def do_storyboard(pid, cid, pfid, n, skill_id, prev, *info_vals):
        p = require_project(ws, pid)
        if p.video_concept is None or p.video_story is None or p.video_story.plan_status != "confirmed":
            raise gr.Error("请先确认剧本")
        if p.video_script is not None and p.video_script.plan_status == "confirmed":
            raise gr.Error("分镜剧本已确认锁定，请先点「🔓 解锁返回修改分镜剧本」")
        info = collect_info(ws, cid, info_vals)
        p.category_id, p.platform_id, p.product_info = cid, pfid, info
        sid = writer.resolve_skill_id("original", skill_id)
        log = _log(prev, f"③ 开始把剧本编译为分镜：{int(n)} 镜 · Skill {sid}")
        yield [log, *_refresh(pid)]
        try:
            async with ws.lock(p.project_id):
                script, mode = await writer.generate(
                    category_id=cid, platform_id=pfid, video_type=p.video_concept.video_type,
                    aspect_ratio=p.video_concept.aspect_ratio, shot_count=int(n),
                    user_creative_idea=p.video_story.content, product_info=info, skill_id=sid,
                    existing=p.video_script, target_duration_seconds=p.video_concept.target_duration_seconds)
                p.video_script = script
                ws.save(p)
        except Exception as e:
            yield [_log(log, friendly(e)), *_refresh(pid)]
            return
        talk = sum(1 for x in script.shots if x.character_dialogue)
        yield [_log(log, f"✅ 分镜编译完成（{mode}）：{len(script.shots)} 镜，{talk} 镜含人物同期声对白"),
               *_refresh(pid)]

    storyboard_btn.click(do_storyboard, [header.project_id, header.category, header.platform, shots_n,
                                         header.skill_dropdowns["video_scriptwriter"], logs, *header.info_fields],
                         [logs, *refresh_outputs], concurrency_id="video_tab", api_name="video_storyboard")

    # ---------------------------------------------------------------- 同一个分镜表：编辑 / 确认
    def _apply(s, tbl):
        if s.plan_status == "draft":
            return apply_shot_rows(s, tbl, rt.category(s.category_id).reference_image_roles)
        return 0

    def do_save_edit(pid, tbl, sel, prev):
        s = _script(ws, pid)
        try:
            n = _apply(s, tbl)
            ws.save(ws.get(pid))
            msg = f"💾 已保存分镜修改（{n} 个镜头有变化）"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *_refresh(pid, sel)]

    save_edit.click(do_save_edit, [header.project_id, table, shot_pick, logs], [logs, *refresh_outputs])

    async def do_regen_shot(pid, sid, tbl, prev, cid, *info_vals):
        s = _script(ws, pid)
        if not sid:
            raise gr.Error("请先选择分镜")
        log = _log(prev, "🎲 正在重写所选分镜…")
        yield [log, *_refresh(pid, sid)]
        try:
            _apply(s, tbl)
            shot = await writer.regenerate_shot(s, sid, collect_info(ws, cid, info_vals))
            ws.save(ws.get(pid))
            log = _log(log, f"✅ 已重写：{shot.scene_description[:30]}…（{shot.audio_mode}）")
        except Exception as e:
            log = _log(log, friendly(e))
        yield [log, *_refresh(pid, sid)]

    regen_shot.click(do_regen_shot, [header.project_id, shot_pick, table, logs, header.category,
                                     *header.info_fields], [logs, *refresh_outputs], concurrency_id="video_tab")

    def _structural(op):
        def handler(pid, sid, tbl, prev):
            s = _script(ws, pid)
            try:
                _apply(s, tbl)
                msg = op(s, sid)
                ws.save(ws.get(pid))
            except Exception as e:
                msg = friendly(e)
            return [_log(prev, msg), *_refresh(pid, sid)]
        return handler

    def _add(s, sid):
        s.add_shot(new_blank_shot())
        return "➕ 已新增 1 个分镜，请在表格中填写"

    def _del(s, sid):
        if not sid:
            raise ValueError("请先选择分镜")
        s.remove_shot(sid)
        return "🗑️ 已删除分镜"

    def _move(delta):
        def op(s, sid):
            if not sid:
                raise ValueError("请先选择分镜")
            i = s._index_of(sid)
            s.move_shot(sid, max(0, min(len(s.shots) - 1, i + delta)))
            return "↕️ 已调整分镜顺序"
        return op

    st_in = [header.project_id, shot_pick, table, logs]
    add_shot.click(_structural(_add), st_in, [logs, *refresh_outputs])
    del_shot.click(_structural(_del), st_in, [logs, *refresh_outputs])
    up_shot.click(_structural(_move(-1)), st_in, [logs, *refresh_outputs])
    down_shot.click(_structural(_move(1)), st_in, [logs, *refresh_outputs])

    def do_confirm(pid, tbl, prev):
        s = _script(ws, pid)
        try:
            _apply(s, tbl)
            items = s.confirm_plan()
            ws.save(ws.get(pid))
            msg = f"✅ 分镜剧本已确认，首帧所需参考图：{'、'.join(c.role for c in items) or '无'}"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *_refresh(pid)]

    confirm_btn.click(do_confirm, [header.project_id, table, logs], [logs, *refresh_outputs],
                      api_name="video_confirm")

    def do_unlock(pid, prev):
        s = _script(ws, pid)
        try:
            s.unlock_plan()
            ws.save(ws.get(pid))
            msg = "🔓 已解锁分镜剧本。注意：修改某个分镜后，该分镜已生成的首帧与 H3 提示词会失效"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *_refresh(pid)]

    unlock_btn.click(do_unlock, [header.project_id, logs], [logs, *refresh_outputs])

    # ================================================================ Step 2: 首帧锁定
    def make_upload(i):
        def on_upload(pid, path, prev):
            if not pid or not path:
                return [prev, *_refresh(pid)]
            p = ws.get(pid)
            s = p.video_script
            if s is None or s.plan_status != "confirmed" or i >= len(s.reference_checklist):
                return [prev, *_refresh(pid)]
            role = s.reference_checklist[i].role
            cur = {a.role: a for a in s.reference_assets}.get(role)
            if cur is not None and str(cur.file_path) == str(path):
                return [prev, *_refresh(pid)]
            saved = rt.store.import_file(p.project_id, path, "references",
                                         dest_name=f"video_{role}{Path(path).suffix or '.png'}")
            s.upload_reference(role, saved)
            ws.save(p)
            return [_log(prev, f"📷 已上传「{role}」，绑定到 {len(s.reference_checklist[i].slot_ids)} 个镜头"),
                    *_refresh(pid)]
        return on_upload

    for i, up in enumerate(uploads):
        up.upload(make_upload(i), [header.project_id, up, logs], [logs, *refresh_outputs])

    def do_reuse(pid, prev):
        p = require_project(ws, pid)
        try:
            roles = reuse_references(p, "video_script")
            ws.save(p)
            msg = f"♻️ 已复用：{'、'.join(roles)}" if roles else "没有可复用的参考图"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *_refresh(pid)]

    reuse_btn.click(do_reuse, [header.project_id, logs], [logs, *refresh_outputs], api_name="video_reuse")

    async def do_frames(pid, m, n, r, prev):
        p = require_project(ws, pid)
        s = p.video_script
        if s is None or s.plan_status != "confirmed":
            raise gr.Error("请先确认分镜剧本，再生成首帧")
        log = prev
        try:
            async for ev in renderer.render_first_frames(p, s, m, image_skill_for_model(m), count=int(n),
                                                         resolution=r):
                log = _log(log, ev.message)
                if ev.image_path or ev.done:
                    yield [log, *_refresh(pid)]
                else:
                    yield [log, *[gr.skip()] * len(refresh_outputs)]
        except Exception as e:
            log = _log(log, friendly(e))
        yield [log, *_refresh(pid)]

    frame_btn.click(do_frames, [header.project_id, frame_model, frame_n, frame_res, logs], [logs, *refresh_outputs],
                    concurrency_id="video_render", concurrency_limit=2, api_name="video_first_frames")

    def do_save_locked(pid, locked_keys, prev):
        s = _script(ws, pid)
        try:
            locked_by_shot = dict(k.split("|", 1) for k in locked_keys or [] if "|" in k)
            changed = 0
            for shot in s.shots:
                new_lock = locked_by_shot.get(shot.shot_id)
                if new_lock != shot.confirmed_first_frame_id:
                    if new_lock:
                        s.confirm_first_frame(shot.shot_id, new_lock)
                    else:
                        shot.confirmed_first_frame_id = None
                    changed += 1
            ws.save(ws.get(pid))
            msg = f"🔒 已保存首帧锁定勾选（{changed} 个分镜有变化，共锁定 {len(locked_by_shot)} 个分镜）"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *_refresh(pid)]

    save_locked.click(do_save_locked, [header.project_id, locked_frames, logs], [logs, *refresh_outputs])

    def do_lock_all(pid, prev):
        s = _script(ws, pid)
        n = 0
        for x in s.shots:
            if x.first_frame_candidates and not x.confirmed_first_frame_id:
                s.confirm_first_frame(x.shot_id, x.first_frame_candidates[-1].frame_id)
                n += 1
        ws.save(ws.get(pid))
        return [_log(prev, f"🔒 已为 {n} 个分镜锁定最新首帧（请确认商品未变形）"), *_refresh(pid)]

    lock_all_btn.click(do_lock_all, [header.project_id, logs], [logs, *refresh_outputs], api_name="video_lock_all")

    # ================================================================ Step 3: H3 提示词 + 视频生成
    async def do_h3(pid, skill_id, mode, prev):
        p = require_project(ws, pid)
        s = p.video_script
        if s is None or s.plan_status != "confirmed":
            raise gr.Error("请先确认分镜剧本并锁定首帧")
        ready = [x for x in s.shots if x.confirmed_first_frame_id]
        if not ready:
            raise gr.Error("还没有锁定任何首帧：请先在 Step 2 勾选并锁定首帧图")
        modes = ["i2va", "ref2va"] if mode == "both" else [mode or "i2va"]
        names = "、".join(H3_MODE_LABELS[m] for m in modes)
        log = _log(prev, f"🪄 运行 H3 Skill（{skill_id}｜{names}）：{len(ready)} 个已锁定首帧的分镜")
        yield [log, *_refresh(pid)]
        for x in ready:
            i = s._index_of(x.shot_id) + 1
            for k, m in enumerate(modes):
                try:
                    prompt = await compiler.h3_prompt(s, x, skill_id, mode=m, activate=k == 0)
                    suffix = f"_{m}" if x.h3_prompt_variants else ""
                    (rt.store.asset_dir(p.project_id, "prompts") / f"h3_{x.shot_id}{suffix}.txt").write_text(
                        prompt, "utf-8")
                    label = H3_MODE_LABELS[m] if x.h3_prompt_variants else "H3"
                    log = _log(log, f"✅ 镜头 {i} {label} 提示词完成：{prompt[:48]}…")
                except Exception as e:
                    log = _log(log, f"镜头 {i}：{friendly(e)}")
                ws.save(p)
                yield [log, *_refresh(pid)]
        skipped = len(s.shots) - len(ready)
        if skipped:
            log = _log(log, f"⚠️ 另有 {skipped} 个分镜未锁定首帧，已跳过")
        yield [log, *_refresh(pid)]

    h3_btn.click(do_h3, [header.project_id, header.skill_dropdowns["video_prompt"], h3_mode, logs],
                 [logs, *refresh_outputs], concurrency_id="video_tab", api_name="video_h3")

    def on_edit_shot(pid, sid, m):
        s = ws.get(pid).video_script if pid else None
        if not (s and sid):
            return ""
        shot = s.get_shot(sid)
        return shot.h3_prompt_variants.get(m) or ("" if shot.h3_prompt_variants else shot.h3_prompt or "")

    edit_shot.change(on_edit_shot, [header.project_id, edit_shot, edit_mode], h3_edit)
    edit_mode.change(on_edit_shot, [header.project_id, edit_shot, edit_mode], h3_edit)

    def do_save_h3(pid, sid, m, text, prev):
        s = _script(ws, pid)
        try:
            if not sid:
                raise ValueError("请选择分镜")
            shot = s.get_shot(sid)
            s.set_h3_prompt(sid, text.strip(), mode=m if shot.h3_prompt_variants else None)
            ws.save(ws.get(pid))
            msg = f"💾 已保存 H3 提示词微调（视频生成使用：{H3_MODE_LABELS[s.get_shot(sid).h3_prompt_mode]}）"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *_refresh(pid, sid)]

    save_h3.click(do_save_h3, [header.project_id, edit_shot, edit_mode, h3_edit, logs], [logs, *refresh_outputs])

    def do_back(pid, sid, file, prev):
        s = _script(ws, pid)
        try:
            if not sid or not file:
                raise ValueError("请选择分镜并上传视频文件")
            src = Path(file if isinstance(file, str) else file.name)
            saved = rt.store.import_file(pid, src, "videos", sub_id=sid)
            s.bind_video(sid, saved, provider="comfyui_external", source="external_upload")
            ws.save(ws.get(pid))
            msg = f"📤 已把 ComfyUI 视频回传绑定到镜头 {s._index_of(sid) + 1}"
        except Exception as e:
            msg = friendly(e)
        return [_log(prev, msg), *_refresh(pid, sid)]

    back_btn.click(do_back, [header.project_id, back_shot, back_video, logs], [logs, *refresh_outputs])

    async def do_warm(provider_id, prev):
        prov = rt.video_provider(provider_id)
        log = _log(prev, f"🔎 正在检查：{prov.display_name}…")
        yield log
        res = await prov.warmup()
        yield _log(log, f"{'✅' if res.ready else '❌'} {res.message}（耗时 {res.elapsed_seconds:.1f}s）")

    warm_btn.click(do_warm, [provider, logs], logs, concurrency_id="video_warm")

    async def _video_stream(pid, prov_id, shot_ids, prev):
        p = require_project(ws, pid)
        s = p.video_script
        log = prev
        for sid in shot_ids:
            i = s._index_of(sid) + 1
            try:
                async for ev in renderer.render_video(p, s, sid, prov_id):
                    log = _log(log, f"镜头 {i}｜{ev.message}")
                    if ev.video_path:
                        yield [log, *_refresh(pid, sid)]
                    else:
                        yield [log, *[gr.skip()] * len(refresh_outputs)]
            except Exception as e:
                log = _log(log, f"镜头 {i}：{friendly(e)}")
                yield [log, *[gr.skip()] * len(refresh_outputs)]
        yield [log, *_refresh(pid, shot_ids[-1] if shot_ids else None)]

    async def do_video(pid, prov_id, sid, prev):
        if not sid:
            raise gr.Error("请选择要生成的分镜")
        async for out in _video_stream(pid, prov_id, [sid], prev):
            yield out

    async def do_video_all(pid, prov_id, prev):
        s = _script(ws, pid)
        ids = [x.shot_id for x in s.shots if x.h3_prompt and x.confirmed_first_frame_id]
        if not ids:
            raise gr.Error("没有可生成的分镜：需要先锁定首帧并生成 H3 提示词")
        async for out in _video_stream(pid, prov_id, ids, prev):
            yield out

    video_btn.click(do_video, [header.project_id, provider, video_shot, logs], [logs, *refresh_outputs],
                    concurrency_id="video_gen", concurrency_limit=2)
    video_all_btn.click(do_video_all, [header.project_id, provider, logs], [logs, *refresh_outputs],
                        concurrency_id="video_gen", concurrency_limit=2, api_name="video_generate_all")

    header.load_event.then(_refresh, header.project_id, refresh_outputs)
    header.project_id.change(_refresh, header.project_id, refresh_outputs)
