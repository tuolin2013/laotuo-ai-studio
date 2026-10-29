"""Gradio 启动入口：python -m app.main"""

from __future__ import annotations

import os

from app.settings import PUBLIC_EVENT_QUEUE_MAX_SIZE, PUBLIC_MAX_UPLOAD_SIZE, load_settings
from app.ui.app import CSS, THEME, build_app


def _bypass_proxy_for_localhost() -> None:
    """系统代理（如 Clash 127.0.0.1:7890）会拦截 Gradio 对本机的启动自检导致 502，这里让本机地址直连。"""
    local = ["127.0.0.1", "localhost", "::1"]
    for key in ("NO_PROXY", "no_proxy"):
        existing = [h.strip() for h in os.environ.get(key, "").split(",") if h.strip()]
        os.environ[key] = ",".join(dict.fromkeys([*existing, *local]))


def main() -> None:
    _bypass_proxy_for_localhost()
    settings = load_settings()
    if settings.public_demo:
        print("🌐 公共免 Key 在线演示模式（LAOTUO_PUBLIC_DEMO=1）：全部走演示模式，不读取任何 Key，"
              "不显示 API 配置；界面没有登录鉴权，打开网址的人都能使用。")
    elif settings.server_name not in {"127.0.0.1", "localhost"}:
        print("⚠️ 当前监听非本机地址，界面没有登录鉴权，局域网内任何人都可访问。")
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    demo = build_app(settings)
    queue_options: dict = {}
    launch_options: dict = {}
    if demo.public is not None:
        print("⏳ 正在准备公共演示的样板项目（张家界莓茶示例主图与详情页，本机绘制）…")
        demo.public.prepare()
        # 动效渲染另有自己的排队上限（5 条，见 app/engine/public_demo.py）；这里只防长任务无限堆积
        queue_options["max_size"] = PUBLIC_EVENT_QUEUE_MAX_SIZE
        launch_options["max_file_size"] = PUBLIC_MAX_UPLOAD_SIZE
    demo.queue(default_concurrency_limit=4, **queue_options).launch(
        server_name=settings.server_name,
        server_port=settings.server_port,
        # 允许界面直接显示 / 下载项目目录中的图片与视频
        allowed_paths=[str(settings.workspace_dir.resolve())],
        theme=THEME,
        css=CSS,
        inbrowser=False,
        **launch_options,
    )


if __name__ == "__main__":
    main()
