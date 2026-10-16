"""Gradio 主界面骨架：【主图】【详情页】【视频】三个独立选项卡。

Step 1 只搭建骨架与 Skill 面板；策划生成 / 渲染流程在后续步骤接入。
每个选项卡的长任务使用独立 concurrency_id，互不阻塞，切换选项卡不中断。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import gradio as gr

from app.engine.config_loader import load_categories, load_platforms
from app.engine.skill_loader import SkillRegistry
from app.settings import Settings
from app.ui.skill_panel import build_skill_panel


def _log_line(msg: str) -> str:
    return f"[{datetime.now():%H:%M:%S}] {msg}"


def _make_dry_run(tab_name: str):
    async def dry_run(idea: str):
        """演示异步生成器 + 实时日志滚动（不调用任何外部接口）。"""
        logs: list[str] = []
        for msg in (
            f"{tab_name}：读取类目与平台配置",
            "策划想法：" + (idea.strip() if idea and idea.strip() else "（未填写，交由 AI 自主策划）"),
            "Step 1 骨架已就绪，策划生成将在下一步接入",
        ):
            logs.append(_log_line(msg))
            yield "\n".join(logs)
            await asyncio.sleep(0.3)

    return dry_run


def _build_tab(name: str, key: str, registry: SkillRegistry, skill_slots: list[tuple[str, str]], ctx):
    categories, platforms = ctx
    with gr.Tab(name):
        with gr.Row():
            gr.Dropdown([(c.name, cid) for cid, c in categories.items()], label="类目")
            gr.Dropdown([(p.name, pid) for pid, p in platforms.items()], label="平台")
        for slot, label in skill_slots:
            build_skill_panel(registry, slot, label)
        idea = gr.Textbox(label="我的策划想法（选填）", lines=3, placeholder="例如：中秋主题、暖色调、突出白霜对比")
        run = gr.Button("生成策划（骨架演示）", variant="primary")
        logs = gr.Textbox(label="运行日志 Running Logs", lines=8, autoscroll=True, interactive=False)
        run.click(_make_dry_run(name), idea, logs, concurrency_id=f"{key}_tab", concurrency_limit=2)


def build_app(settings: Settings) -> gr.Blocks:
    registry = SkillRegistry()
    ctx = (load_categories(), load_platforms())
    with gr.Blocks(title="电商 AIGC 内容工厂") as demo:
        gr.Markdown("# 电商 AIGC 内容工厂\n主图 · 详情页 · 带货短视频（MiniMax H3 原生音画一体）")
        with gr.Tabs():
            image_skills = [("ecommerce_planner", "电商策划"), ("image_prompt", "生图提示词")]
            _build_tab("主图", "main_image", registry, image_skills, ctx)
            _build_tab("详情页", "detail_page", registry, image_skills, ctx)
            _build_tab(
                "视频",
                "video",
                registry,
                [("video_scriptwriter", "视频编剧"), ("image_prompt", "首帧生图提示词"),
                 ("video_prompt", "H3 视频提示词")],
                ctx,
            )
    return demo
