"""工作台视觉主题：克制的石板灰卡片 + 青松绿主操作 + 深色运行日志监控台。

色板（只在这里定义，界面代码通过 elem_classes 引用）：
- 页面底 #F8FAFC / 卡片 #FFFFFF / 卡片边框 #E2E8F0 / 输入框边框 #CBD5E1
- 正文 #1E293B / 标签 #334155 / 次要说明 #64748B
- 主操作 #0D9488 → #0F766E（仅 variant="primary" 按钮）/ 浅青底 #F0FDFA
- 日志台 #0F172A，标题 #38BDF8，正文 #4ADE80

字体只用本机字体（Windows 微软雅黑 UI / macOS 苹方），不从 Google Fonts 下载，国内网络下首屏不卡。
"""

from __future__ import annotations

from html import escape

import gradio as gr

FONT = ["Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "HarmonyOS Sans SC", "Noto Sans SC",
        "system-ui", "sans-serif"]
FONT_MONO = ["Consolas", "Cascadia Mono", "Menlo", "monospace"]


def _build_theme() -> gr.themes.Base:
    theme = gr.themes.Base(
        primary_hue=gr.themes.colors.teal,
        secondary_hue=gr.themes.colors.teal,
        neutral_hue=gr.themes.colors.slate,
        radius_size=gr.themes.sizes.radius_md,
        font=FONT,
        font_mono=FONT_MONO,
    ).set(
        body_background_fill="#F8FAFC",
        body_text_color="#1E293B",
        body_text_color_subdued="#64748B",
        body_text_size="14px",
        background_fill_primary="#FFFFFF",
        background_fill_secondary="#F8FAFC",
        border_color_primary="#E2E8F0",
        border_color_accent="#5EEAD4",
        border_color_accent_subdued="#99F6E4",
        color_accent="#0D9488",
        color_accent_soft="#F0FDFA",
        link_text_color="#0F766E",
        link_text_color_hover="#115E59",
        link_text_color_visited="#0F766E",
        code_background_fill="#F1F5F9",
        prose_header_text_weight="600",
        section_header_text_weight="600",
        shadow_drop="none",
        shadow_spread="3px",
        # 卡片：白底 + 1px 柔和边框，不加阴影；外层卡片 10px 圆角，内层组件 8px
        block_background_fill="#FFFFFF",
        block_border_color="#E2E8F0",
        block_border_width="1px",
        block_radius="8px",
        block_shadow="none",
        block_padding="10px 12px",
        container_radius="10px",
        panel_background_fill="#FFFFFF",
        panel_border_color="#E2E8F0",
        panel_border_width="1px",
        layout_gap="12px",
        form_gap_width="12px",
        # 输入框 / 下拉框的小标签：透明底、石板灰、13px 600
        block_title_background_fill="transparent",
        block_title_border_width="0px",
        block_title_padding="0",
        block_title_radius="0",
        block_title_text_color="#334155",
        block_title_text_size="13px",
        block_title_text_weight="600",
        block_info_text_color="#64748B",
        block_info_text_size="12px",
        # 画廊 / 图片 / 文件左上角的悬浮标签：白底细边，不用彩色色块
        block_label_background_fill="#FFFFFF",
        block_label_border_color="#E2E8F0",
        block_label_border_width="1px",
        block_label_text_color="#334155",
        block_label_text_size="12.5px",
        block_label_text_weight="600",
        block_label_padding="3px 8px",
        block_label_margin="6px",
        block_label_radius="6px",
        block_label_right_radius="6px",
        block_label_shadow="none",
        accordion_text_color="#0F172A",
        input_background_fill="#FFFFFF",
        input_background_fill_focus="#FFFFFF",
        input_background_fill_hover="#FFFFFF",
        input_border_color="#CBD5E1",
        input_border_color_hover="#94A3B8",
        input_border_color_focus="#0D9488",
        input_border_width="1px",
        input_padding="8px 10px",
        input_radius="6px",
        input_shadow="none",
        input_shadow_focus="0 0 0 3px rgba(13, 148, 136, 0.18)",
        input_text_size="14px",
        input_placeholder_color="#94A3B8",
        checkbox_label_background_fill="#FFFFFF",
        checkbox_label_background_fill_hover="#F8FAFC",
        checkbox_label_background_fill_selected="#F0FDFA",
        checkbox_label_border_color="#E2E8F0",
        checkbox_label_border_color_hover="#CBD5E1",
        checkbox_label_border_color_selected="#0D9488",
        checkbox_label_border_width="1px",
        checkbox_label_text_color="#334155",
        checkbox_label_text_color_selected="#0F766E",
        checkbox_label_shadow="none",
        checkbox_label_padding="6px 10px",
        checkbox_background_color_selected="#0D9488",
        checkbox_border_color="#94A3B8",
        checkbox_border_color_focus="#0D9488",
        checkbox_border_color_selected="#0D9488",
        checkbox_shadow="none",
        slider_color="#0D9488",
        loader_color="#0D9488",
        table_border_color="#E2E8F0",
        table_even_background_fill="#FFFFFF",
        table_odd_background_fill="#F8FAFC",
        table_radius="8px",
        table_row_focus="#F0FDFA",
        # 按钮：主操作青松绿渐变，次级白底灰框
        button_border_width="1px",
        button_transition="background 0.12s ease, border-color 0.12s ease, color 0.12s ease",
        button_large_padding="8px 16px",
        button_large_radius="8px",
        button_large_text_size="14px",
        button_large_text_weight="600",
        button_medium_padding="6px 14px",
        button_medium_radius="8px",
        button_medium_text_size="14px",
        button_medium_text_weight="600",
        button_small_padding="4px 10px",
        button_small_radius="6px",
        button_small_text_size="13px",
        button_small_text_weight="600",
        button_primary_background_fill="linear-gradient(180deg, #0D9488 0%, #0F766E 100%)",
        button_primary_background_fill_hover="linear-gradient(180deg, #0F766E 0%, #115E59 100%)",
        button_primary_border_color="#0F766E",
        button_primary_border_color_hover="#115E59",
        button_primary_text_color="#FFFFFF",
        button_primary_text_color_hover="#FFFFFF",
        button_primary_shadow="none",
        button_primary_shadow_hover="none",
        button_primary_shadow_active="inset 0 1px 2px rgba(15, 23, 42, 0.25)",
        button_secondary_background_fill="#FFFFFF",
        button_secondary_background_fill_hover="#F1F5F9",
        button_secondary_border_color="#CBD5E1",
        button_secondary_border_color_hover="#94A3B8",
        button_secondary_text_color="#334155",
        button_secondary_text_color_hover="#0F172A",
        button_secondary_shadow="none",
        button_secondary_shadow_hover="none",
        button_secondary_shadow_active="inset 0 1px 2px rgba(15, 23, 42, 0.08)",
    )
    # 工作台只做浅色版：系统切到深色模式时沿用同一套颜色，避免自定义 CSS 与深色变量混搭
    for name in list(vars(theme)):
        light = name.removesuffix("_dark")
        if name.endswith("_dark") and hasattr(theme, light):
            setattr(theme, name, getattr(theme, light))
    return theme


THEME = _build_theme()


def step_header(step: int, title: str) -> gr.HTML:
    """步骤卡片标题：「Step N」徽章 + 标题。Step 1 → 2 → 3 是真实的先后顺序，所以才用序号。"""
    return gr.HTML(f'<div class="step-head"><span class="step-no">Step {step}</span>'
                   f'<h3 class="step-title">{escape(title)}</h3></div>', js_on_load=None)


CSS = """
/* Gradio 默认 overflow: hidden 会让 position: sticky 失效；clip 裁剪效果相同，但不阻止停靠 */
.gradio-container { color-scheme: light; overflow: clip !important; }

/* ---------- 输入框 / 下拉框小标签：去掉彩色底块 ---------- */
.gradio-container [data-testid="block-info"] {
  background: transparent !important;
  color: #334155;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0;
}

/* 相邻输入组件不再拼成一整块灰底，每个字段独立成行 */
.gradio-container .form {
  background: transparent;
  border: 0;
  box-shadow: none;
}

/* ---------- 卡片层次：外层卡片 10px，折叠面板同级 ---------- */
.gradio-container .gr-accordion {
  border-radius: 10px;
  padding: 10px 14px;
}
.gradio-container .gr-accordion > .label-wrap {
  font-size: 14px;
  font-weight: 600;
  min-height: 24px;
  align-items: center;
}
.gradio-container .gr-accordion .gr-accordion { background: #FFFFFF; }

/* ---------- 卡片底板：项目面板与步骤卡片是同一级的白色卡片 ---------- */
.gradio-container .column.project-panel,
.gradio-container .column.step-card {
  background: #FFFFFF;
  border: 1px solid #E2E8F0;
  border-radius: 10px;
}

/* ---------- 顶栏：品牌、通道状态、项目操作 ---------- */
.app-header { gap: 12px !important; }
.gradio-container .column.project-panel { gap: 10px !important; padding: 14px 16px !important; }
.gradio-container .app-brand h1 {
  margin: 0;
  font-size: 20px;
  font-weight: 700;
  line-height: 1.3;
  color: #0F172A;
}
.gradio-container .app-brand p { margin: 2px 0 0; font-size: 12.5px; color: #64748B; }

/* ---------- 品牌栏：产品名 + 右侧「👑 老庹 AI 工坊」小徽章（琥珀色，与青松绿主操作区分开） ---------- */
.top-bar {
  align-items: center !important;
  justify-content: space-between;
  flex-wrap: wrap !important;
  gap: 8px 12px !important;
  padding: 2px 2px 0;
}
/* 品牌块按 0 起算宽度：否则 Markdown 的 width: 100% 会占满一行，把徽章挤到下一行 */
.top-bar > .app-brand { flex: 1 1 0% !important; min-width: 0 !important; width: auto !important; }
@media (max-width: 640px) { .top-bar > .app-brand { flex-basis: 100% !important; } }
.gradio-container button.pro-badge {
  flex: 0 0 auto;
  height: 30px;
  padding: 0 14px;
  border: 1px solid #FCD34D;
  border-radius: 999px;
  background: #FFFBEB;
  color: #92400E;
  font-size: 12.5px;
  font-weight: 600;
  white-space: nowrap;
}
.gradio-container button.pro-badge:hover { background: #FEF3C7; border-color: #F59E0B; }
.gradio-container .column.pro-panel {
  background: #FFFFFF;
  border: 1px solid #FDE68A;
  border-radius: 10px;
  padding: 12px 16px !important;
  gap: 8px !important;
}
.pro-panel-head { align-items: center !important; justify-content: space-between; flex-wrap: nowrap !important; }
.pro-panel-head > .pro-title { flex: 1 1 0% !important; min-width: 0 !important; width: auto !important; }
.gradio-container button.pro-close { flex: 0 0 auto; height: 28px; padding: 0 12px; white-space: nowrap; }
.gradio-container .pro-title h3 { margin: 0; font-size: 15px; font-weight: 600; color: #0F172A; }
.pro-grid { gap: 12px 24px !important; align-items: flex-start !important; }
.pro-grid > .block { flex: 1 1 0; min-width: min(380px, 100%) !important; }
.gradio-container .pro-panel h4 { margin: 0 0 6px; font-size: 13.5px; font-weight: 600; color: #0F172A; }
.gradio-container .pro-panel p,
.gradio-container .pro-panel li,
.gradio-container .pro-panel td,
.gradio-container .pro-panel th { font-size: 12.5px; line-height: 1.6; color: #334155; }
.gradio-container .mode-status p,
.gradio-container .project-info p {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.65;
  color: #475569;
}
.gradio-container .project-info p + p { margin-top: 2px; }
/* 通道状态与项目操作之间一条分隔线（Gradio 的 hide-container 会清掉边框，所以要 !important） */
.gradio-container .block.mode-status {
  padding-bottom: 8px !important;
  border-width: 0 0 1px 0 !important;
  border-color: #E2E8F0 !important;
  border-radius: 0 !important;
}

/* 紧凑表单：标签在左、输入框在右，同一面板内标签列宽一致 */
.compact-form .block.padded,
.project-panel .block.padded { padding: 0; }
.compact-form .form, .project-panel .form { gap: 8px 16px; }
.compact-form .block .container,
.project-panel .block .container {
  display: flex;
  align-items: center;
  gap: 10px;
}
.compact-form .block .container > [data-testid="block-info"],
.project-panel .block .container > [data-testid="block-info"] {
  flex: 0 0 var(--label-col, 6.6em);
  margin: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.project-panel { --label-col: 4.6em; }
.compact-form .block .container > :not([data-testid="block-info"]),
.project-panel .block .container > :not([data-testid="block-info"]) {
  flex: 1 1 auto;
  min-width: 0;
}
/* 文本框与下拉框统一 36px 高，同一行完全齐平 */
.compact-form input, .project-panel input { height: 36px; }
.compact-form .wrap-inner, .project-panel .wrap-inner { padding: 0 10px; min-height: 34px; }
.compact-form input[role="combobox"], .project-panel input[role="combobox"] { height: 34px; padding: 0; margin: 0; }

/* 🔄 与 📂 加载 等按钮与输入框等高，同一行紧凑排列 */
.toolbar-row { gap: 8px !important; align-items: center !important; flex-wrap: nowrap !important; }
.toolbar-row > button {
  align-self: center;
  height: 36px;
  min-height: 36px;
  padding: 0 14px;
  white-space: nowrap;
}

/* 类目 / 平台 / 商品信息：每行 3 个字段的网格 */
.info-grid {
  display: grid !important;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px 16px !important;
  align-items: center;
}
.info-grid > .form { display: contents; }
.info-grid > .block { min-width: 0 !important; }
.info-grid .platform-hint p, .info-grid .compliance p, .info-grid .compliance li {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.55;
  color: #64748B;
}
/* 平台提示占两格（补齐最后一行的空位），合规检查独占一行 */
.info-grid .platform-hint { grid-column: span 2; }
.info-grid .compliance { grid-column: 1 / -1; }
@media (max-width: 1100px) { .info-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
/* 手机：一列，标签回到输入框上方 */
@media (max-width: 640px) {
  .info-grid { grid-template-columns: minmax(0, 1fr); }
  /* 单列时 span 2 会多撑出一列隐式网格，改为占满整行 */
  .info-grid .platform-hint { grid-column: 1 / -1; }
  .compact-form .block .container,
  .project-panel .block .container { display: block; }
  .compact-form .block .container > [data-testid="block-info"],
  .project-panel .block .container > [data-testid="block-info"] { display: block; margin-bottom: 4px; }
  .toolbar-row { align-items: flex-end !important; }
}

/* Skill 管理 与 API 配置 左右两列 */
.config-row { gap: 12px !important; }
/* Gradio 在折叠面板上写了内联 min-width: 0，需要 !important 才能在窄屏换行 */
.config-row > .gr-accordion { flex: 1 1 0; min-width: min(420px, 100%) !important; }

/* ---------- 三大选项卡 ---------- */
.gradio-container [role="tablist"] { height: 46px; gap: 4px; }
.gradio-container [role="tablist"] > button {
  height: 46px;
  padding: 0 18px;
  font-size: 15px;
  font-weight: 600;
  color: #64748B;
}
.gradio-container [role="tablist"] > button:hover { color: #0F172A; }
.gradio-container [role="tablist"] > button.selected { color: #0F766E; }

/* ---------- 步骤卡片：柔和左边框 + Step 徽章 ---------- */
.gradio-container .column.step-card {
  border-left: 3px solid #5EEAD4;
  padding: 14px 16px 16px !important;
  gap: 12px !important;
}
.step-head { display: flex; align-items: center; gap: 10px; }
.step-no {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 9px;
  border: 1px solid #99F6E4;
  border-radius: 999px;
  background: #F0FDFA;
  color: #0F766E;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}
.step-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  line-height: 1.4;
  color: #0F172A;
}
.step-card .prose h4 { margin: 4px 0 0; font-size: 14px; font-weight: 600; color: #0F172A; }

/* 状态说明（草稿 / 已确认 / 传图清单）：浅底提示条，与可编辑区分开 */
.gradio-container .block.status-note {
  padding: 8px 12px !important;
  border-radius: 8px !important;
  background: #F8FAFC !important;
  border: 1px solid #E2E8F0 !important;
}
.gradio-container .status-note p { margin: 0; font-size: 13px; line-height: 1.65; color: #334155; }
.gradio-container .status-note p + p { margin-top: 4px; }

/* 生成类大按钮 */
.gradio-container button.cta { min-height: 46px; font-size: 15px; }

/* ---------- 可编辑表格：列宽固定、横向平滑滚动 ---------- */
.plan-table .table-wrap {
  overflow-x: auto;
  scroll-behavior: smooth;
  overscroll-behavior-x: contain;
  scrollbar-color: #CBD5E1 transparent;
}
.plan-table th { background: #F1F5F9; color: #334155; font-weight: 600; font-size: 13px; }
.plan-table td { font-size: 13px; line-height: 1.55; vertical-align: top; }

/* ---------- 运行日志：深色终端监控台 ---------- */
.gradio-container .block.terminal-log {
  background: #0F172A !important;
  border: 1px solid #1E293B !important;
  border-radius: 10px !important;
  padding: 10px 12px 12px !important;
}
.gradio-container .terminal-log [data-testid="block-info"] {
  color: #38BDF8 !important;
  font-family: Consolas, "Cascadia Mono", monospace;
  font-size: 12.5px;
}
.gradio-container .terminal-log textarea {
  background: transparent !important;
  border: 0 !important;
  box-shadow: none !important;
  padding: 4px 2px !important;
  color: #4ADE80 !important;
  -webkit-text-fill-color: #4ADE80;
  font-family: Consolas, "Cascadia Mono", "Courier New", monospace !important;
  font-size: 12.5px !important;
  line-height: 1.6 !important;
  font-variant-numeric: tabular-nums;
  scrollbar-color: #334155 transparent;
}
.gradio-container .terminal-log textarea::selection { background: #1E3A5F; }

/* 工作区左右两栏顶部对齐；视频选项卡的日志随页面滚动停靠在右侧，生成时始终可见 */
.workbench { align-items: flex-start !important; gap: 14px !important; }
.workbench > .log-dock { position: sticky; top: 12px; align-self: flex-start; }
/* 窄屏左右两栏会上下堆叠，此时不停靠，避免日志盖住表单 */
@media (max-width: 1024px) { .workbench > .log-dock { position: static; } }

/* ---------- 键盘焦点与减少动态效果 ---------- */
.gradio-container button:focus-visible,
.gradio-container [role="tab"]:focus-visible,
.gradio-container .label-wrap:focus-visible {
  outline: 2px solid #0D9488;
  outline-offset: 2px;
}
@media (prefers-reduced-motion: reduce) {
  .gradio-container *, .gradio-container *::before, .gradio-container *::after {
    transition-duration: 0s !important;
    scroll-behavior: auto !important;
  }
}
"""
