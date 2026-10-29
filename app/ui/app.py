"""Gradio 主界面：顶部全局控制栏 + 【主图】【详情页】【视频】【配音】【动效广告】五个独立选项卡。

每个选项卡的长任务都是 async 生成器，使用独立 concurrency_id，互不阻塞，切换选项卡不中断。
"""

from __future__ import annotations

import gradio as gr

from app.engine.public_demo import PublicDemo
from app.engine.runtime import EngineRuntime
from app.settings import PUBLIC_OUTPUT_TTL_SECONDS, Settings
from app.ui.header import build_header
from app.ui.image_tab import DETAIL_SPEC, MAIN_SPEC, build_image_tab
from app.ui.motion_tab import build_motion_tab
from app.ui.state import Workspace
from app.ui.theme import CSS, THEME
from app.ui.video_tab import build_video_tab
from app.ui.voice_tab import build_voice_tab

__all__ = ["CSS", "THEME", "build_app"]  # app.main 启动时把主题与样式传给 launch()


def build_app(settings: Settings, runtime: EngineRuntime | None = None) -> gr.Blocks:
    rt = runtime or EngineRuntime(settings=settings, force_demo=settings.demo_mode)
    ws = Workspace(rt)
    # 公共免 Key 在线演示（LAOTUO_PUBLIC_DEMO=1）：访客独立项目 + 过期清理；Gradio 自己缓存的文件（上传、
    # 界面显示的图片与视频副本）也每半小时清一次超过 1 小时的
    cache_ttl = None
    if rt.settings.public_demo:
        ws.public = PublicDemo(rt, forget=ws.forget)
        cache_ttl = (PUBLIC_OUTPUT_TTL_SECONDS // 2, PUBLIC_OUTPUT_TTL_SECONDS)
    with gr.Blocks(title="Laotuo(老庹)电商 AIGC 内容工厂", delete_cache=cache_ttl) as demo:
        header = build_header(ws, demo)
        with gr.Tabs():
            with gr.Tab("🖼️ 主图生成"):
                build_image_tab(ws, header, MAIN_SPEC)
            with gr.Tab("📜 详情页生成"):
                build_image_tab(ws, header, DETAIL_SPEC)
            with gr.Tab("🎬 带货视频生成"):
                build_video_tab(ws, header)
            with gr.Tab("🎙️ 配音"):
                build_voice_tab(ws, header)
            with gr.Tab("✨ 动效广告"):
                build_motion_tab(ws, header)
    demo.rt = rt  # 便于测试访问
    demo.public = ws.public  # app.main 启动时预载样板项目
    return demo
