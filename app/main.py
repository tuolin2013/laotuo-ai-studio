"""Gradio 启动入口：python -m app.main"""

from __future__ import annotations

from app.settings import load_settings
from app.ui.app import build_app


def main() -> None:
    settings = load_settings()
    if settings.server_name not in {"127.0.0.1", "localhost"}:
        print("⚠️ 当前监听非本机地址，界面没有登录鉴权，局域网内任何人都可访问。")
    demo = build_app(settings)
    demo.queue(default_concurrency_limit=4).launch(
        server_name=settings.server_name, server_port=settings.server_port
    )


if __name__ == "__main__":
    main()
