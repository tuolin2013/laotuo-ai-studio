"""Gradio 主界面：顶部全局控制栏 + 【主图】【详情页】【视频】三个独立选项卡。

每个选项卡的长任务都是 async 生成器，使用独立 concurrency_id，互不阻塞，切换选项卡不中断。
"""

from __future__ import annotations

import gradio as gr

from app.engine.runtime import EngineRuntime
from app.settings import Settings
from app.ui.header import build_header
from app.ui.image_tab import DETAIL_SPEC, MAIN_SPEC, build_image_tab
from app.ui.state import Workspace
from app.ui.theme import CSS, THEME
from app.ui.video_tab import build_video_tab

__all__ = ["CSS", "THEME", "build_app"]  # app.main 启动时把主题与样式传给 launch()


def build_app(settings: Settings, runtime: EngineRuntime | None = None) -> gr.Blocks:
    rt = runtime or EngineRuntime(settings=settings, force_demo=settings.demo_mode)
    ws = Workspace(rt)
    with gr.Blocks(title="Laotuo(老庹)电商 AIGC 内容工厂") as demo:
        header = build_header(ws, demo)
        with gr.Tabs():
            with gr.Tab("🖼️ 主图生成"):
                build_image_tab(ws, header, MAIN_SPEC)
            with gr.Tab("📜 详情页生成"):
                build_image_tab(ws, header, DETAIL_SPEC)
            with gr.Tab("🎬 带货视频生成"):
                build_video_tab(ws, header)
    demo.rt = rt  # 便于测试访问
    return demo
