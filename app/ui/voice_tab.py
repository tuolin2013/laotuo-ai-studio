"""【配音】选项卡：VoxCPM2 带货口播配音。

四步：① 配音文案（AI 撰写 / 粘贴自动分段 / 导入视频台词，逐段编辑）→ ② 音色（声音设计 / 声音克隆 / 音色库，
试听后锁定）→ ③ 逐段生成（串行排队，每段可多版本切换、单段重做）→ ④ 合成完整配音（wav / mp3 / srt / zip）。

配音是独立成品，不写入视频分镜：H3 视频原生带声音，项目不设画外音轨道。
长任务都是 async 生成器，使用独立 concurrency_id，与其他选项卡互不阻塞。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import gradio as gr

from app.engine.compliance import find_violations
from app.engine.voice_engine import VoiceEngine
from app.schemas.voice import (
    LANGUAGES,
    PURPOSES,
    VOICE_PRESETS,
    VoiceStudio,
    clean_control,
    compose_control,
    display_text,
    estimate_seconds,
    preview_text_for,
    segment_warnings,
    split_leading_note,
)
from app.ui.header import Header, require_project
from app.ui.state import UIError, Workspace, _rows, friendly, log_line
from app.ui.theme import step_header

SEGMENT_HEADERS = ["段", "配音文案", "本段风格（选填）", "段后停顿(秒)", "预计(秒)"]
MAX_PLAYERS = 12
REFERENCE_TYPES = [".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".mp4", ".mov", ".webm", ".mkv"]
MODE_CHOICES = [("🎨 声音设计（用文字描述音色）", "design"), ("🧬 声音克隆（上传 / 录制参考声音）", "clone"),
                ("📚 音色库（复用保存过的音色）", "library")]
WAY_CHOICES = [("① 生成未完成的段落（新段、改过文案或换了音色的段）", "pending"), ("② 选段生成", "pick"),
               ("③ 全部重新生成", "all")]
# 刷新输出的顺序（运行日志之后）；端到端测试按名字取位置
REFRESH_KEYS = [
    "script_status", "table", "seg_pick", "source_md", "ref_audio", "preview_audio", "library_pick",
    "library_audio", "shot_pick", "gen_status", "gen_pick", *[f"player_{i}" for i in range(MAX_PLAYERS)],
    "ver_seg", "ver_pick", "ver_audio", "mix_audio", "mix_files", "mix_info",
]

TIPS_MD = """
**正文怎么写**
- 直接用目标语言写；方言要用地道说法（粤语写「唔該」，不写「麻烦」），系统会自动在指令里加上方言名
- 一段 8~40 个字、一两句话；要停顿就拆成两段，用「段后停顿」控制，不要堆标点；一两个字的段落听起来会发虚
- 价格、规格照常写阿拉伯数字（99元、500ml、3.5折）：开着「数字 / 单位转读法」会自动读成「九十九元」，可点「🔢 预览」核对

**多音字**：字后面紧跟带声调数字的拼音，如 `重{chong2}新`、`长{zhang3}大`、`行{hang2}业`，字幕仍显示原字

**非语言标签**（一段最多 1~2 个，整条 2~3 个就够）：`[laughing]` 笑　`[sigh]` 叹气　`[Uhm]` 迟疑　`[Shh]` 嘘
`[Question-ah]` `[Question-ei]` `[Question-en]` `[Question-oh]` 疑问　`[Surprise-wa]` `[Surprise-yo]` 惊讶
`[Dissatisfaction-hnn]` 不满。大小写照抄，不要自己编新标签

**不要写**「（笑）」「（停顿）」这类括号说明：括号里的字会被读出来。语气写到「本段风格」（如「语速稍快，热情」），
男女老少等音色在 Step 2 设定——风格指令不能换人
"""


# ---------------------------------------------------------------- 表格 <-> 模型


def segments_to_rows(st: VoiceStudio | None) -> list[list[Any]]:
    if st is None:
        return []
    return [[i, s.text, s.control, s.pause_after, estimate_seconds(s.text)] for i, s in enumerate(st.segments, 1)]


def apply_segment_rows(st: VoiceStudio, table: Any) -> int:
    """把表格修改写回配音文案。段首括号里的说明自动搬进「本段风格」。返回修改的段数。"""
    rows = _rows(table)
    if not st.segments:
        return 0  # 还没有文案时表格可能带一行空白占位
    if len(rows) != len(st.segments):
        raise UIError("表格行数与段落数不一致：增删段落请使用表格下方的按钮")
    changed = 0
    for row, seg in zip(rows, list(st.segments), strict=True):
        row = [*row, *[""] * (len(SEGMENT_HEADERS) - len(row))]
        body, lead = split_leading_note(str(row[1] or ""))
        try:
            pause = round(float(row[3]), 2)
        except (TypeError, ValueError) as e:
            raise UIError(f"第 {row[0]} 段「段后停顿」必须是数字") from e
        new = {"text": body, "control": compose_control(lead, clean_control(str(row[2] or ""))), "pause_after": pause}
        if new != {"text": seg.text, "control": seg.control, "pause_after": seg.pause_after}:
            try:
                st.update_segment(seg.segment_id, **new)
            except ValueError as e:
                raise UIError(f"第 {row[0]} 段修改无效：{friendly(e).removeprefix('❌ ')}") from e
            changed += 1
    return changed


def _apply_source(st: VoiceStudio, mode, desc, style, hifi, denoise, prompt, lib_id) -> None:
    fields = {"mode": mode or "design", "description": desc or "", "style": style or "", "hifi": bool(hifi),
              "denoise": bool(denoise), "prompt_text": prompt or ""}
    if mode == "library" and lib_id:
        fields["voice_id"] = lib_id
    st.set_source(**fields)


def _apply_params(st: VoiceStudio, cfg, steps, normalize, seed, trim) -> None:
    st.set_params(cfg_value=float(cfg), inference_timesteps=int(steps), normalize=bool(normalize),
                  seed=None if seed is None or seed == "" else int(seed), trim_silence=bool(trim))


# ---------------------------------------------------------------- 状态文案


def script_status_md(st: VoiceStudio | None, category, platform) -> str:
    if st is None or not st.segments:
        return "还没有配音文案：点「✍️ AI 撰写配音文案」，或展开下方面板粘贴现成文案 / 导入视频台词"
    total, target = st.total_estimated_seconds, st.target_duration_seconds
    gap = "（偏长：可删减段落或让 AI 重写）" if total > target * 1.25 else "（偏短）" if total < target * 0.75 else ""
    lines = [f"📝 **{st.title or '配音文案'}**　共 {len(st.segments)} 段　｜　预计 **{total:g} 秒**（目标 {target} 秒）"
             f"{gap}　｜　{st.language_label} · {PURPOSES[st.purpose]}"]
    if st.notes:
        lines.append(f"💬 {st.notes}")
    hits = find_violations(" ".join(display_text(s.text) for s in st.segments), category, platform)
    if hits:
        lines.append(f"⚠️ **合规检查**：命中违禁词 {'、'.join(hits)}，请修改后再生成")
    tips = [f"- 第 {i} 段：{'；'.join(w)}" for i, s in enumerate(st.segments, 1) if (w := segment_warnings(s.text))]
    if tips:
        lines.append("**写法提醒**\n" + "\n".join(tips))
    lines.append("在表格里改文案、本段风格和段后停顿，改完点「💾 保存表格修改」；增删、调整顺序用表格下方的按钮。")
    return "\n\n".join(lines)


def source_status_md(st: VoiceStudio | None, engine: VoiceEngine) -> str:
    if st is None:
        return "先在顶部新建或加载一个项目"
    src = st.source
    if src.mode == "design":
        if not src.description:
            return "🎨 **声音设计**：填写音色描述（或选一个预设），点「🔊 试听」听听效果"
        if src.design_locked:
            return (f"🔒 **已锁定音色**：「{src.description}」\n\n"
                    "之后每段都以上面这段样音为参考，整条配音是同一个声音。"
                    "想换声音：重新试听，满意后再点「🔒 锁定」，然后选「③ 全部重新生成」。")
        return (f"🎨 **声音设计**：「{src.description}」\n\n"
                "每次试听都是一个新的声音（VoxCPM2 不给参考音频时会随机音色）。"
                "听到满意的就点「🔒 锁定这个音色」；不锁定直接生成时，系统会先自动生成一段样音并锁定。")
    if src.mode == "clone":
        if not (src.reference_path and src.reference_path.exists()):
            return "🧬 **声音克隆**：上传或录制一段 5~30 秒的清晰人声（只有一个人在说话、没有背景音乐最好）"
        way = "高保真克隆（参考音频 + 逐字文本，音色最像）" if src.hifi and src.prompt_text \
            else "可控克隆（可用「整体风格」调语速、情绪）"
        tips = engine.reference_tips(src)
        if src.hifi and not src.prompt_text:
            tips.append("勾选了高保真克隆但还没有逐字文本：点「🎧 自动识别」或手动填写，否则按可控克隆处理")
        lines = [f"🧬 **声音克隆**：「{src.reference_name}」{src.reference_seconds:.1f} 秒　｜　{way}"
                 + ("　｜　克隆前降噪" if src.denoise else "")]
        lines += [f"- ⚠️ {t}" for t in tips]
        return "\n\n".join(lines)
    try:
        profile = engine.library.get(src.voice_id)
    except ValueError:
        return "📚 **音色库**：从下拉框选一个保存过的音色（声音设计锁定后、或克隆后，都可以「📚 存入音色库」）"
    way = "高保真克隆" if src.hifi and profile.prompt_text else "可控克隆"
    return (f"📚 **音色库**：「{profile.name}」（{profile.kind_label}，样音 {profile.sample_seconds:.1f} 秒）"
            f"　｜　{way}"
            + (f"\n\n{profile.description}" if profile.description else ""))


def gen_status_md(st: VoiceStudio | None, provider_name: str) -> str:
    if st is None or not st.segments:
        return "Step 1 写好配音文案后，在这里逐段生成"
    key = st.source.voice_key
    done = sum(1 for s in st.segments if s.active_take)
    stale = st.stale_segments()
    other = [i for i, s in enumerate(st.segments, 1) if s.active_take and s.active_take.voice_key != key]
    lines = [f"🎙️ 已生成 **{done}/{len(st.segments)}** 段　｜　配音后端：{provider_name}"]
    if stale:
        lines.append(f"✏️ 第 {'、'.join(map(str, stale))} 段生成后改过文案")
    if other:
        lines.append(f"🔄 第 {'、'.join(map(str, other))} 段是用之前的音色生成的")
    if done == len(st.segments) and not stale and not other:
        lines.append("✅ 全部就绪，可以到 Step 4 合成完整配音")
    else:
        lines.append("「① 生成未完成的段落」只处理没生成、改过文案或换了音色的段，已经满意的段不会重做")
    return "\n\n".join(lines)


def mix_md(st: VoiceStudio | None, workspace_root: Path) -> str:
    mixes = [m for m in st.mixes if m.wav_path.exists()] if st else []
    if not mixes:
        return ("全部段落生成后点「🎧 合成完整配音」：自动去掉每段首尾静音、按段后停顿衔接、统一响度到 -16 LUFS，"
                "并导出与配音时间完全对齐的 srt 字幕")
    m = mixes[-1]
    lines = [f"🎉 第 {len(mixes)} 版成品：**{m.duration_seconds:.1f} 秒**，{m.segment_count} 段"
             f"（{m.created_at.astimezone():%m-%d %H:%M}）",
             "mp3 可以直接拖进剪映 / 达芬奇当配音轨；srt 字幕与配音时间对齐；zip 里还有每段单独的音频和配音文案。"]
    if len(mixes) > 1:
        older = [f"第 {i} 版 {x.duration_seconds:.1f} 秒（{x.created_at.astimezone():%m-%d %H:%M}）"
                 for i, x in enumerate(mixes[:-1], 1)]
        lines.append("历史成品：" + "；".join(older))
    try:
        where = m.wav_path.parent.relative_to(workspace_root.parent)
    except ValueError:
        where = m.wav_path.parent
    lines.append(f"保存位置：`{where.as_posix()}`")
    return "\n\n".join(lines)


# ---------------------------------------------------------------- 选项卡


def build_voice_tab(ws: Workspace, header: Header) -> None:
    rt = ws.rt
    engine = VoiceEngine(rt)

    with gr.Row(elem_classes=["workbench"]):
        with gr.Column(scale=7):
            # ========================================================== Step 1：配音文案
            with gr.Column(elem_classes=["step-card"]):
                step_header(1, "配音文案：AI 撰写 / 粘贴分段 / 导入视频台词")
                gr.Markdown("配音按段生成再拼接：官方建议长文切成短段分别合成，比一次读完整段更稳，"
                            "不会越读越快或发糊。每段都可以单独改、单独重做。")
                with gr.Row():
                    language = gr.Dropdown([(v[0], k) for k, v in LANGUAGES.items()], value="mandarin",
                                           label="配音语言 / 方言")
                    purpose = gr.Dropdown([(v, k) for k, v in PURPOSES.items()], value="live_selling", label="用途")
                    duration = gr.Slider(5, 120, value=30, step=5, label="目标时长（秒）")
                with gr.Row():
                    idea = gr.Textbox(label="🎙️ 我的配音想法（选填）", lines=1, scale=4,
                                      placeholder="例如：开头用反问抓人；重点讲买二送一；结尾催单")
                    write_btn = gr.Button("✍️ AI 撰写配音文案", variant="primary", scale=1)
                with gr.Accordion("📋 已有文案？粘贴后自动分段 / 导入视频剧本台词", open=False):
                    paste = gr.Textbox(label="粘贴整段口播稿（按句号、问号、感叹号自动分段，太短的句子自动合并）",
                                       lines=5)
                    with gr.Row():
                        split_btn = gr.Button("✂️ 按句自动分段")
                        import_btn = gr.Button("🎬 导入视频剧本里的人物台词")
                script_status = gr.Markdown(elem_classes=["status-note"])
                table = gr.Dataframe(headers=SEGMENT_HEADERS, interactive=True, wrap=True, static_columns=[0, 4],
                                     datatype=["number", "str", "str", "number", "number"],
                                     column_widths=[56, 420, 200, 104, 88],
                                     label="配音文案（可编辑：文案、本段风格、段后停顿）", elem_classes=["plan-table"])
                with gr.Row():
                    seg_pick = gr.Dropdown(label="选中段落", scale=3)
                    up_btn = gr.Button("⬆️ 上移", scale=1, min_width=80)
                    down_btn = gr.Button("⬇️ 下移", scale=1, min_width=80)
                    add_btn = gr.Button("➕ 在后面插入", scale=1, min_width=110)
                    del_btn = gr.Button("🗑️ 删除", scale=1, min_width=80)
                with gr.Row():
                    save_btn = gr.Button("💾 保存表格修改")
                    norm_btn = gr.Button("🔢 预览数字 / 单位读法")
                norm_md = gr.Markdown()
                with gr.Accordion("💡 写法速查（VoxCPM2 官方最佳实践）", open=False):
                    gr.Markdown(TIPS_MD)

            # ========================================================== Step 2：音色
            with gr.Column(elem_classes=["step-card"]):
                step_header(2, "音色：声音设计 / 声音克隆 / 音色库")
                mode = gr.Radio(MODE_CHOICES, value="design", label="音色来源")
                with gr.Column(visible=True) as design_box, gr.Row():
                    preset = gr.Dropdown(list(VOICE_PRESETS), value=None, label="快速预设", scale=1)
                    description = gr.Textbox(
                        label="音色描述（身份设定 + 声音质感 + 表达状态，中英文都可以）", lines=2, scale=3,
                        placeholder="例如：中年男性，声音浑厚淳朴，语速适中，亲切踏实，像邻家大叔推荐自家好货")
                with gr.Column(visible=False) as clone_box:
                    with gr.Row():
                        ref_file = gr.File(label="上传参考声音（音频或视频，5~30 秒最佳）", file_types=REFERENCE_TYPES,
                                           type="filepath", height=110, scale=3)
                        ref_mic = gr.Audio(sources=["microphone"], type="filepath", label="🎤 或直接录一段", scale=2)
                    with gr.Row():
                        ref_start = gr.Number(value=0, label="从第几秒开始截取", precision=1, minimum=0, scale=1)
                        recut_btn = gr.Button("✂️ 按起始秒重新截取", scale=1)
                        shot_pick = gr.Dropdown(label="或：用某个分镜视频里的人声", scale=2)
                        shot_btn = gr.Button("🎬 用这个分镜的人声", scale=1)
                    with gr.Row():
                        prompt_text = gr.Textbox(label="参考音频的逐字文本（高保真克隆用，要与录音一字不差）", lines=2,
                                                 scale=4)
                        asr_btn = gr.Button("🎧 自动识别", scale=1)
                    denoise = gr.Checkbox(
                        label="参考音频降噪（有背景音乐 / 环境噪声时打开；录音很干净时关闭，保留原声特点）")
                with gr.Column(visible=False) as library_box:
                    with gr.Row():
                        library_pick = gr.Dropdown(label="音色库（所有项目共用）", scale=3)
                        lib_del_btn = gr.Button("🗑️ 从音色库删除", scale=1)
                    library_audio = gr.Audio(label="样音", type="filepath", interactive=False)
                with gr.Column(visible=False) as style_box:
                    style = gr.Textbox(label="整体风格（选填，可控克隆时生效）", max_lines=1,
                                       placeholder="例如：语速稍快，热情。只能调情绪、语速，不能把男声变成女声")
                hifi = gr.Checkbox(value=True, label="高保真克隆：参考音频 + 逐字文本续写，音色最像；"
                                                     "开启后「整体风格」「本段风格」不生效")
                ref_audio = gr.Audio(label="当前参考音色", type="filepath", interactive=False)
                source_md = gr.Markdown(elem_classes=["status-note"])
                with gr.Row():
                    preview_text = gr.Textbox(label="试听文字（留空用默认样句，约 8 秒）", max_lines=2, scale=4,
                                              placeholder=preview_text_for("mandarin"))
                    preview_btn = gr.Button("🔊 试听", variant="primary", scale=1)
                preview_audio = gr.Audio(label="试听结果", type="filepath", interactive=False)
                with gr.Row():
                    lock_btn = gr.Button("🔒 锁定这个音色（声音设计）", scale=2)
                    lib_name = gr.Textbox(label="存入音色库的名称", placeholder="例如：莓茶大叔", max_lines=1, scale=2)
                    lib_save_btn = gr.Button("📚 存入音色库", scale=1)

            # ========================================================== Step 3：逐段生成
            with gr.Column(elem_classes=["step-card"]):
                step_header(3, "逐段生成配音（每段可多版本切换、单独重做）")
                with gr.Accordion("⚙️ 合成参数（一般不用改）", open=False):
                    with gr.Row():
                        cfg = gr.Slider(1.0, 3.0, value=2.0, step=0.1, label="CFG 引导强度",
                                        info="默认 2.0；越高越贴文本但更易出瑕疵，长段发糊 / 发嗡时降到 1.5~1.6")
                        steps = gr.Slider(4, 30, value=10, step=1, label="推理步数", info="默认 10；越多越细腻越慢")
                    with gr.Row():
                        normalize = gr.Checkbox(value=True, label="数字 / 单位转读法（99元 → 九十九元）")
                        trim = gr.Checkbox(value=True, label="合成完整配音时去掉每段首尾静音")
                        seed = gr.Number(value=42, precision=0,
                                         label="随机种子（留空 = 每次随机；同一段重做时自动换种子）")
                gen_status = gr.Markdown(elem_classes=["status-note"])
                way = gr.Radio(WAY_CHOICES, value="pending", label="生成方式")
                gen_pick = gr.CheckboxGroup(label="要生成的段落", visible=False)
                with gr.Row():
                    gen_btn = gr.Button("🎙️ 开始生成配音", variant="primary", size="lg", scale=3, elem_classes=["cta"])
                    check_btn = gr.Button("🔎 检查配音服务（不启动 GPU）", scale=1)
                players: list[gr.Audio] = []
                for r in range(MAX_PLAYERS // 2):
                    with gr.Row():
                        for c in range(2):
                            players.append(gr.Audio(type="filepath", interactive=False, visible=False,
                                                    label=f"第 {r * 2 + c + 1} 段"))
                with gr.Accordion("🗂️ 版本管理：试听旧版本 / 换用 / 单段重做", open=False):
                    with gr.Row():
                        ver_seg = gr.Dropdown(label="段落", scale=2)
                        ver_pick = gr.Dropdown(label="版本", scale=3)
                    ver_audio = gr.Audio(type="filepath", interactive=False, label="所选版本")
                    with gr.Row():
                        use_ver_btn = gr.Button("✅ 用这一版")
                        redo_btn = gr.Button("🔁 重新生成这一段")

            # ========================================================== Step 4：合成完整配音
            with gr.Column(elem_classes=["step-card"]):
                step_header(4, "合成完整配音 · 导出 wav / mp3 / 字幕")
                mix_btn = gr.Button("🎧 合成完整配音", variant="primary", size="lg", elem_classes=["cta"])
                mix_audio = gr.Audio(label="完整配音", type="filepath", interactive=False)
                mix_files = gr.File(label="下载（wav 无损 / mp3 / srt 字幕 / zip 含分段音频与配音文案）",
                                    file_count="multiple", interactive=False)
                mix_info = gr.Markdown()

        with gr.Column(scale=5, elem_classes=["log-dock"]):
            logs = gr.Textbox(label="📜 运行日志 Running Logs", lines=20, max_lines=20, autoscroll=True,
                              interactive=False, elem_classes=["terminal-log"])

    # ================================================================ 刷新
    refresh_outputs = [script_status, table, seg_pick, source_md, ref_audio, preview_audio, library_pick,
                       library_audio, shot_pick, gen_status, gen_pick, *players, ver_seg, ver_pick, ver_audio,
                       mix_audio, mix_files, mix_info]
    assert len(refresh_outputs) == len(REFRESH_KEYS)
    source_group = {source_md, ref_audio, preview_audio, library_pick, library_audio, gen_status, *players}
    gen_group = {gen_status, gen_pick, *players, ver_seg, ver_pick, ver_audio}
    mix_group = {mix_audio, mix_files, mix_info, gen_status}
    skips = [gr.skip()] * len(refresh_outputs)

    def _ref_path(st: VoiceStudio) -> Path | None:
        src = st.source
        if src.mode == "design":
            return src.locked_sample_path if src.design_locked else None
        if src.mode == "clone":
            return src.reference_path if src.reference_path and src.reference_path.exists() else None
        try:
            return engine.library.get(src.voice_id).sample_path
        except ValueError:
            return None

    def _refresh(pid, sel=None, ver_sel=None, picked=None) -> list:
        p = ws.get(pid) if pid else None
        st = p.voice_studio if p else None
        segs = st.segments if st else []
        key = st.source.voice_key if st else ""
        seg_ch = [(f"第 {i} 段：{display_text(s.text)[:14]}", s.segment_id) for i, s in enumerate(segs, 1)]
        ids = [c[1] for c in seg_ch]
        cat = rt.category(p.category_id) if p else None
        plat = rt.platform(p.platform_id) if p else None
        # 音色库
        profiles = engine.library.list()
        lib_ch = [(f"{x.name}（{x.kind_label} · {x.sample_seconds:.0f} 秒）", x.voice_id) for x in profiles]
        lib_ids = [c[1] for c in lib_ch]
        lib_val = st.source.voice_id if st and st.source.voice_id in lib_ids else (lib_ids[0] if lib_ids else None)
        lib_sample = next((str(x.sample_path) for x in profiles if x.voice_id == lib_val), None)
        # 每段播放器
        ups = []
        for i in range(MAX_PLAYERS):
            if i < len(segs):
                s, t = segs[i], segs[i].active_take
                ups.append(gr.update(visible=True, value=str(t.file_path) if t else None,
                                     label=f"第 {i + 1} 段 · {s.status_label(key)} · {display_text(s.text)[:16]}"))
            else:
                ups.append(gr.update(visible=False, value=None))
        # 版本管理
        vsel = ver_sel if ver_sel in ids else (ids[0] if ids else None)
        seg = st.get_segment(vsel) if st and vsel else None
        active = seg.active_take if seg else None
        ver_ch = []
        for k, t in enumerate(seg.playable_takes if seg else [], 1):
            using = "（使用中）" if active and t.take_id == active.take_id else ""
            ver_ch.append((f"第 {k} 版 · {t.created_at.astimezone():%m-%d %H:%M} · {t.duration_seconds:.1f} 秒 · "
                           f"{t.voice_label}{using}", t.take_id))
        mix = st.latest_mix if st else None
        ref = _ref_path(st) if st else None
        preview = st.source.preview_path if st and st.source.preview_path and st.source.preview_path.exists() else None
        provider_name = rt.voice_provider().display_name
        return [
            script_status_md(st, cat, plat) if p else "先在顶部新建或加载一个项目",
            segments_to_rows(st),
            gr.update(choices=seg_ch, value=sel if sel in ids else (ids[0] if ids else None)),
            source_status_md(st, engine),
            gr.update(value=str(ref) if ref else None,
                      label="已锁定的音色样音" if st and st.source.mode == "design" else "当前参考音色"),
            str(preview) if preview else None,
            gr.update(choices=lib_ch, value=lib_val),
            lib_sample,
            gr.update(choices=engine.shot_video_choices(p) if p else []),
            gen_status_md(st, provider_name),
            gr.update(choices=seg_ch, value=[v for v in (picked or []) if v in ids]),
            *ups,
            gr.update(choices=seg_ch, value=vsel),
            gr.update(choices=ver_ch, value=active.take_id if active else None),
            str(active.file_path) if active else None,
            str(mix.wav_path) if mix else None,
            [str(f) for f in mix.files] if mix else None,
            mix_md(st, rt.settings.workspace_dir),
        ]

    def _partial(pid, keep: set, **kw) -> list:
        full = _refresh(pid, **kw)
        return [v if c in keep else gr.skip() for c, v in zip(refresh_outputs, full, strict=True)]

    load_outputs = [language, purpose, duration, mode, description, style, hifi, denoise, prompt_text, cfg, steps,
                    normalize, seed, trim]

    def _load_values(pid) -> list:
        p = ws.get(pid) if pid else None
        st = (p.voice_studio if p else None) or VoiceStudio()
        src = st.source
        return [st.language, st.purpose, st.target_duration_seconds, src.mode, src.description, src.style, src.hifi,
                src.denoise, src.prompt_text, st.cfg_value, st.inference_timesteps, st.normalize, st.seed,
                st.trim_silence]

    def _log(prev, msg):
        return (prev + "\n" if prev else "") + log_line(msg)

    src_inputs = [mode, description, style, hifi, denoise, prompt_text, library_pick]
    param_inputs = [cfg, steps, normalize, seed, trim]

    # ================================================================ Step 1：文案
    async def on_meta(pid, lang, purp, dur):
        if not pid:
            return gr.skip()
        p = ws.get(pid)
        engine.studio(p).set_meta(language=lang, purpose=purp, target_duration_seconds=int(dur))
        engine.save(p)
        return gr.update(placeholder=preview_text_for(lang))

    meta_in = [header.project_id, language, purpose, duration]
    language.input(on_meta, meta_in, preview_text)
    purpose.input(on_meta, meta_in, preview_text)
    duration.release(on_meta, meta_in, preview_text)

    async def do_write(pid, lang, purp, dur, idea_text, skill_id, prev):
        p = require_project(ws, pid)
        st = engine.studio(p)
        st.set_meta(language=lang, purpose=purp, target_duration_seconds=int(dur))
        log = _log(prev, f"✍️ 正在撰写配音文案（{st.language_label}｜{PURPOSES[st.purpose]}｜约 "
                         f"{st.target_duration_seconds} 秒｜Skill {skill_id}）…")
        yield [log, *skips, gr.skip()]
        try:
            res = await engine.write_script(p, skill_id=skill_id, idea=idea_text)
            async with ws.lock(pid):
                engine.apply_script(p, res, skill_id)
        except Exception as e:
            yield [_log(log, friendly(e)), *skips, gr.skip()]
            return
        log = _log(log, f"✅ 配音文案完成（{res.mode}）：{len(st.segments)} 段，预计 {st.total_estimated_seconds:g} 秒")
        if res.voice_description:
            used = st.source.description == res.voice_description
            log = _log(log, f"🎨 建议音色：{res.voice_description}"
                            + ("（已填入 Step 2）" if used else "（Step 2 已有音色描述，未覆盖）"))
        for w in res.warnings:
            log = _log(log, f"⚠️ {w}")
        yield [log, *_refresh(pid), st.source.description]

    write_btn.click(do_write, [header.project_id, language, purpose, duration, idea,
                               header.skill_dropdowns["voice_writer"], logs],
                    [logs, *refresh_outputs, description], concurrency_id="voice_tab", concurrency_limit=2,
                    api_name="voice_write")

    async def do_split(pid, text, prev):
        p = require_project(ws, pid)
        try:
            async with ws.lock(pid):
                n = engine.split_pasted(p, text)
        except Exception as e:
            return [_log(prev, friendly(e)), *skips]
        st = p.voice_studio
        return [_log(prev, f"✂️ 已按句分成 {n} 段，预计 {st.total_estimated_seconds:g} 秒"), *_refresh(pid)]

    split_btn.click(do_split, [header.project_id, paste, logs], [logs, *refresh_outputs], api_name="voice_split")

    async def do_import(pid, prev):
        p = require_project(ws, pid)
        try:
            async with ws.lock(pid):
                n = engine.import_video_lines(p)
        except Exception as e:
            return [_log(prev, friendly(e)), *skips]
        return [_log(prev, f"🎬 已导入视频剧本里 {n} 个分镜的人物台词"), *_refresh(pid)]

    import_btn.click(do_import, [header.project_id, logs], [logs, *refresh_outputs], api_name="voice_import_video")

    async def do_save(pid, tbl, sel, prev):
        p = require_project(ws, pid)
        try:
            async with ws.lock(pid):
                n = apply_segment_rows(engine.studio(p), tbl)
                engine.save(p)
        except Exception as e:
            return [_log(prev, friendly(e)), *skips]
        return [_log(prev, f"💾 已保存表格修改（{n} 段有变化）"), *_refresh(pid, sel=sel)]

    save_btn.click(do_save, [header.project_id, table, seg_pick, logs], [logs, *refresh_outputs],
                   api_name="voice_save")

    def _seg_op(op):
        async def handler(pid, sid, tbl, prev):
            p = require_project(ws, pid)
            st = engine.studio(p)
            try:
                async with ws.lock(pid):
                    apply_segment_rows(st, tbl)
                    msg, new_sel = op(st, sid)
                    engine.save(p)
            except Exception as e:
                return [_log(prev, friendly(e)), *skips]
            return [_log(prev, msg), *_refresh(pid, sel=new_sel, ver_sel=new_sel)]

        return handler

    def _need(sid):
        if not sid:
            raise UIError("请先在「选中段落」里选一段")
        return sid

    def _add(st, sid):
        seg = st.add_segment(sid if sid in [s.segment_id for s in st.segments] else None)
        return f"➕ 已插入第 {st.index_of(seg.segment_id) + 1} 段，请在表格里填写文案", seg.segment_id

    def _del(st, sid):
        i = st.index_of(_need(sid))
        st.delete_segment(sid)
        nxt = st.segments[min(i, len(st.segments) - 1)].segment_id
        return f"🗑️ 已删除第 {i + 1} 段", nxt

    def _move(delta):
        def op(st, sid):
            st.move_segment(_need(sid), delta)
            return f"↕️ 已移到第 {st.index_of(sid) + 1} 段", sid

        return op

    st_in = [header.project_id, seg_pick, table, logs]
    up_btn.click(_seg_op(_move(-1)), st_in, [logs, *refresh_outputs])
    down_btn.click(_seg_op(_move(1)), st_in, [logs, *refresh_outputs])
    add_btn.click(_seg_op(_add), st_in, [logs, *refresh_outputs])
    del_btn.click(_seg_op(_del), st_in, [logs, *refresh_outputs])

    async def do_norm(pid, tbl, norm_on, prev):
        p = require_project(ws, pid)
        st = engine.studio(p)
        try:
            async with ws.lock(pid):
                apply_segment_rows(st, tbl)
                engine.save(p)
            yield [_log(prev, "🔢 正在预览数字 / 单位的读法（只用 CPU，不启动 GPU）…"), gr.skip()]
            rows = await engine.normalize_preview(p)
        except Exception as e:
            yield [_log(prev, friendly(e)), gr.skip()]
            return
        if rows is None:
            md = "演示模式无法预览读法：登录 Modal 后由 VoxCPM2 服务端按真实规则转换"
        elif not rows:
            md = "✅ 没有需要转换读法的数字或单位"
        else:
            md = "**数字 / 单位的实际读法**（核对价格、规格是否读对；读得不对就把原文改成想要的写法）\n\n" + "\n".join(
                f"- 第 {i} 段：{display_text(a)} → **{display_text(b)}**" for i, a, b in rows)
        if not norm_on:
            md = "⚠️ Step 3「数字 / 单位转读法」已关闭，生成时数字会按原样交给模型。\n\n" + md
        yield [_log(prev, "🔢 读法预览完成"), md]

    norm_btn.click(do_norm, [header.project_id, table, normalize, logs], [logs, norm_md], concurrency_id="voice_norm")

    # ================================================================ Step 2：音色
    def on_mode(m):
        return [gr.update(visible=m == "design"), gr.update(visible=m == "clone"), gr.update(visible=m == "library"),
                gr.update(visible=m != "design")]

    mode.change(on_mode, mode, [design_box, clone_box, library_box, style_box])

    async def do_source(pid, *vals):
        """音色设定改动后立即保存（只刷新音色与生成状态，不动文案表格）。"""
        if not pid:
            return skips
        p = ws.get(pid)
        try:
            async with ws.lock(pid):
                _apply_source(engine.studio(p), *vals)
                engine.save(p)
        except Exception:
            return skips
        return _partial(pid, source_group)

    src_save_in = [header.project_id, *src_inputs]
    mode.input(do_source, src_save_in, refresh_outputs, api_name="voice_source")
    for comp in (description, style, prompt_text):
        comp.blur(do_source, src_save_in, refresh_outputs)
    for comp in (hifi, denoise, library_pick):
        comp.input(do_source, src_save_in, refresh_outputs)
    preset.input(lambda name: VOICE_PRESETS.get(name, gr.skip()), preset, description).then(
        do_source, src_save_in, refresh_outputs)

    async def _ref_stream(pid, path, start, name, prev, tip=""):
        p = require_project(ws, pid)
        if not path:
            yield [prev, gr.skip(), gr.skip(), *skips]
            return
        log = _log(prev, f"🎧 正在处理参考声音：{name}（转成 16kHz 单声道，最长截取 30 秒）…")
        yield [log, gr.skip(), gr.skip(), *skips]
        try:
            async with ws.lock(pid):
                src = await engine.prepare_reference(p, Path(path), start=float(start or 0), name=name)
        except Exception as e:
            yield [_log(log, friendly(e)), gr.skip(), gr.skip(), *skips]
            return
        log = _log(log, f"✅ 参考声音已就绪：{src.reference_seconds:.1f} 秒")
        for t in [*engine.reference_tips(src), *([tip] if tip else [])]:
            log = _log(log, f"⚠️ {t}")
        log = _log(log, "下一步：点「🎧 自动识别」得到逐字文本（高保真克隆用），再点「🔊 试听」")
        yield [log, "", "clone", *_partial(pid, source_group)]

    ref_out = [logs, prompt_text, mode, *refresh_outputs]

    async def do_ref_file(pid, path, start, prev):
        async for out in _ref_stream(pid, path, start, Path(path).name if path else "", prev):
            yield out

    ref_file.upload(do_ref_file, [header.project_id, ref_file, ref_start, logs], ref_out,
                    concurrency_id="voice_ref", api_name="voice_ref_upload")
    recut_btn.click(do_ref_file, [header.project_id, ref_file, ref_start, logs], ref_out, concurrency_id="voice_ref")

    async def do_ref_mic(pid, path, prev):
        async for out in _ref_stream(pid, path, 0, "现场录音", prev):
            yield out

    ref_mic.stop_recording(do_ref_mic, [header.project_id, ref_mic, logs], ref_out, concurrency_id="voice_ref")

    async def do_ref_shot(pid, path, start, prev):
        p = require_project(ws, pid)
        name = next((label for label, value in engine.shot_video_choices(p) if value == path), "分镜视频")
        async for out in _ref_stream(pid, path, start, name, prev,
                                     tip="分镜视频常带环境音 / 背景音乐，建议勾选「参考音频降噪」"):
            yield out

    shot_btn.click(do_ref_shot, [header.project_id, shot_pick, ref_start, logs], ref_out, concurrency_id="voice_ref")

    async def do_asr(pid, prev):
        p = require_project(ws, pid)
        log = prev
        try:
            async for ev in engine.transcribe_reference(p):
                log = _log(log, ev.message)
                yield [log, gr.skip(), *skips]
        except Exception as e:
            yield [_log(log, friendly(e)), gr.skip(), *skips]
            return
        yield [log, p.voice_studio.source.prompt_text, *_partial(pid, source_group)]

    asr_btn.click(do_asr, [header.project_id, logs], [logs, prompt_text, *refresh_outputs],
                  concurrency_id="voice_gen", concurrency_limit=1, api_name="voice_transcribe")

    async def _prepare(pid, src_vals, param_vals):
        p = require_project(ws, pid)
        st = engine.studio(p)
        async with ws.lock(pid):
            _apply_source(st, *src_vals)
            _apply_params(st, *param_vals)
            engine.save(p)
        return p, st

    async def do_preview(pid, text, *vals):
        src_vals, param_vals, prev = vals[:7], vals[7:12], vals[12]
        log = prev
        try:
            p, _ = await _prepare(pid, src_vals, param_vals)
            async for ev in engine.preview(p, text):
                log = _log(log, ev.message)
                yield [log, *(_partial(pid, source_group) if ev.final else skips)]
        except Exception as e:
            yield [_log(log, friendly(e)), *_partial(pid, source_group)]

    preview_btn.click(do_preview, [header.project_id, preview_text, *src_inputs, *param_inputs, logs],
                      [logs, *refresh_outputs], concurrency_id="voice_gen", concurrency_limit=1,
                      api_name="voice_preview")

    async def do_lock(pid, *vals):
        src_vals, prev = vals[:7], vals[7]
        try:
            p = require_project(ws, pid)
            async with ws.lock(pid):
                _apply_source(engine.studio(p), *src_vals)
                engine.lock_preview(p)
        except Exception as e:
            return [_log(prev, friendly(e)), *_partial(pid, source_group)]
        return [_log(prev, "🔒 已锁定这个音色：之后每段都以这段样音为参考，整条配音是同一个声音"),
                *_partial(pid, source_group)]

    lock_btn.click(do_lock, [header.project_id, *src_inputs, logs], [logs, *refresh_outputs], api_name="voice_lock")

    async def do_lib_save(pid, name, *vals):
        src_vals, prev = vals[:7], vals[7]
        try:
            p = require_project(ws, pid)
            async with ws.lock(pid):
                _apply_source(engine.studio(p), *src_vals)
                engine.save(p)
            profile = engine.save_to_library(p, name or "")
        except Exception as e:
            return [_log(prev, friendly(e)), *_partial(pid, source_group)]
        msg = f"📚 已存入音色库：「{profile.name}」（{profile.kind_label}），所有项目都能在「音色库」里选用"
        return [_log(prev, msg),
                *_partial(pid, source_group)]

    lib_save_btn.click(do_lib_save, [header.project_id, lib_name, *src_inputs, logs], [logs, *refresh_outputs],
                       api_name="voice_library_save")

    async def do_lib_delete(pid, voice_id, prev):
        try:
            if not voice_id:
                raise UIError("请先选择要删除的音色")
            gone = engine.library.delete(voice_id)
        except Exception as e:
            return [_log(prev, friendly(e)), *skips]
        return [_log(prev, f"🗑️ 已从音色库删除：「{gone.name}」"), *_partial(pid, source_group)]

    lib_del_btn.click(do_lib_delete, [header.project_id, library_pick, logs], [logs, *refresh_outputs])

    # ================================================================ Step 3：逐段生成
    way.change(lambda w: gr.update(visible=w == "pick"), way, gen_pick)

    async def _render_stream(pid, tbl, way_v, picked, src_vals, param_vals, prev, ver_sel=None):
        p = require_project(ws, pid)
        st = engine.studio(p)
        try:
            async with ws.lock(pid):
                changed = apply_segment_rows(st, tbl) if tbl is not None else 0
                _apply_source(st, *src_vals)
                _apply_params(st, *param_vals)
                engine.save(p)
        except Exception as e:
            yield [_log(prev, friendly(e)), *skips]
            return
        log = _log(prev, f"💾 已保存表格修改（{changed} 段）") if changed else prev
        keep = gen_group | ({script_status, table, seg_pick} if changed else set())
        try:
            async for ev in engine.render(p, way_v, picked):
                log = _log(log, ev.message)
                yield [log, *(_partial(pid, keep, ver_sel=ver_sel, picked=picked) if ev.audio_path else skips)]
        except Exception as e:
            log = _log(log, friendly(e))
        yield [log, *_partial(pid, keep | source_group | mix_group, ver_sel=ver_sel, picked=picked)]

    async def do_render(pid, tbl, way_v, picked, *vals):
        src_vals, param_vals, prev = vals[:7], vals[7:12], vals[12]
        async for out in _render_stream(pid, tbl, way_v, picked, src_vals, param_vals, prev):
            yield out

    gen_btn.click(do_render, [header.project_id, table, way, gen_pick, *src_inputs, *param_inputs, logs],
                  [logs, *refresh_outputs], concurrency_id="voice_gen", concurrency_limit=1, api_name="voice_render")

    async def do_redo(pid, sid, *vals):
        src_vals, param_vals, prev = vals[:7], vals[7:12], vals[12]
        if not sid:
            yield [_log(prev, "⚠️ 请先选择要重新生成的段落"), *skips]
            return
        async for out in _render_stream(pid, None, "pick", [sid], src_vals, param_vals, prev, ver_sel=sid):
            yield out

    redo_btn.click(do_redo, [header.project_id, ver_seg, *src_inputs, *param_inputs, logs], [logs, *refresh_outputs],
                   concurrency_id="voice_gen", concurrency_limit=1)

    async def do_check(prev):
        prov = rt.voice_provider()
        log = _log(prev, f"🔎 正在检查：{prov.display_name}…")
        yield log
        res = await prov.warmup()
        yield _log(log, f"{'✅' if res.ready else '❌'} {res.message}（耗时 {res.elapsed_seconds:.1f}s）")

    check_btn.click(do_check, logs, logs, concurrency_id="voice_check")

    def on_ver_seg(pid, sid):
        return _partial(pid, {ver_pick, ver_audio}, ver_sel=sid) if pid else skips

    ver_seg.input(on_ver_seg, [header.project_id, ver_seg], refresh_outputs)

    def on_ver_pick(pid, sid, take_id):
        if not (pid and sid and take_id):
            return gr.skip()
        st = ws.get(pid).voice_studio
        try:
            take = next(t for t in st.get_segment(sid).playable_takes if t.take_id == take_id)
        except (StopIteration, ValueError, AttributeError):
            return gr.skip()
        return str(take.file_path)

    ver_pick.input(on_ver_pick, [header.project_id, ver_seg, ver_pick], ver_audio)

    async def do_use_ver(pid, sid, take_id, prev):
        p = require_project(ws, pid)
        try:
            if not (sid and take_id):
                raise UIError("请先选择段落和版本")
            async with ws.lock(pid):
                st = engine.studio(p)
                st.choose_take(sid, take_id)
                engine.save(p)
            i = st.index_of(sid) + 1
        except Exception as e:
            return [_log(prev, friendly(e)), *skips]
        return [_log(prev, f"✅ 第 {i} 段改用所选版本"), *_partial(pid, gen_group | mix_group, ver_sel=sid)]

    use_ver_btn.click(do_use_ver, [header.project_id, ver_seg, ver_pick, logs], [logs, *refresh_outputs])

    # ================================================================ Step 4：合成完整配音
    async def do_mix(pid, prev):
        p = require_project(ws, pid)
        log = prev
        try:
            async for ev in engine.mix(p):
                log = _log(log, ev.message)
                yield [log, *(_partial(pid, mix_group) if ev.final else skips)]
        except Exception as e:
            yield [_log(log, friendly(e)), *skips]

    mix_btn.click(do_mix, [header.project_id, logs], [logs, *refresh_outputs], concurrency_id="voice_mix",
                  api_name="voice_mix")

    # 加载 / 切换项目时刷新全部内容并回填设定
    for event in (header.load_event, header.project_id.change(_refresh, header.project_id, refresh_outputs)):
        event.then(_load_values, header.project_id, load_outputs)
    header.load_event.then(_refresh, header.project_id, refresh_outputs)
