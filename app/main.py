"""Gradio 启动入口：python -m app.main"""

from __future__ import annotations

import os

from app.settings import load_settings
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
    if settings.server_name not in {"127.0.0.1", "localhost"}:
        print("⚠️ 当前监听非本机地址，界面没有登录鉴权，局域网内任何人都可访问。")
    demo = build_app(settings)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    demo.queue(default_concurrency_limit=4).launch(
        server_name=settings.server_name,
        server_port=settings.server_port,
        # 允许界面直接显示 / 下载项目目录中的图片与视频
        allowed_paths=[str(settings.workspace_dir.resolve())],
        theme=THEME,
        css=CSS,
        inbrowser=False,
    )


if __name__ == "__main__":
    main()
