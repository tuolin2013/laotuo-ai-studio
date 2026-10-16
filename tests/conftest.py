"""全局测试保护：测试绝不调用真实的 Modal GPU（会产生费用）。

本机若已登录 Modal（~/.modal.toml），选择「Modal MiniMax H3」的测试会真的启动 GPU。
这里把「是否已登录 Modal」统一视为否，需要真实服务的场景请用 scripts/h3_smoke_test.py 手动运行。
"""

import pytest


@pytest.fixture(autouse=True)
def _no_real_modal(monkeypatch, request):
    if request.node.get_closest_marker("allow_modal_login"):
        return
    import app.providers.video.modal_minimax_h3 as h3
    import app.settings as settings

    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    monkeypatch.setattr(settings, "_modal_logged_in", lambda: False)
    monkeypatch.setattr(h3, "modal_logged_in", lambda: False)


def pytest_configure(config):
    config.addinivalue_line("markers", "allow_modal_login: 允许测试看到真实的 Modal 登录状态（仍需自行注入替身）")
