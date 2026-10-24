"""界面主题：运行日志监控台、商品信息单行输入框、步骤卡片（只检查组件配置，不启动服务）。"""

import gradio as gr
import pytest

from app.engine.runtime import EngineRuntime
from app.settings import Settings
from app.ui.app import build_app
from app.ui.theme import CSS, THEME


@pytest.fixture(scope="module")
def blocks(tmp_path_factory):
    rt = EngineRuntime(settings=Settings(workspace_dir=tmp_path_factory.mktemp("ws")), force_demo=True)
    return list(build_app(rt.settings, runtime=rt).blocks.values())


def _classes(block) -> list[str]:
    c = block.elem_classes or []
    return [c] if isinstance(c, str) else list(c)


def test_running_logs_use_terminal_style(blocks):
    logs = [b for b in blocks if isinstance(b, gr.Textbox) and b.label == "📜 运行日志 Running Logs"]
    assert len(logs) == 5  # 主图 / 详情页 / 视频 / 配音 / 动效广告
    assert all("terminal-log" in _classes(b) for b in logs)
    assert ".terminal-log textarea" in CSS


def test_product_info_fields_are_single_line(blocks):
    fields = [b for b in blocks if isinstance(b, gr.Textbox) and str(b.key).startswith("info_")]
    assert len(fields) == 8
    assert all(b.max_lines == 1 for b in fields)


def test_each_tab_has_step_cards(blocks):
    cards = [b for b in blocks if isinstance(b, gr.Column) and "step-card" in _classes(b)]
    # 主图 / 详情页各 Step 1~3，视频 Step 1~4（Step 4 剪辑大师一键成片），配音 Step 1~4，动效广告 Step 1~3
    assert len(cards) == 17


def test_theme_labels_plain_and_primary_buttons_teal():
    assert THEME.block_title_background_fill == "transparent"
    assert "#0D9488" in THEME.button_primary_background_fill
    assert THEME.button_secondary_background_fill == "#FFFFFF"
