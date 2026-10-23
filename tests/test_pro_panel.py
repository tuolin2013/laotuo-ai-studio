"""顶部「👑 老庹 AI 工坊」入口：徽章默认只显示一枚按钮，面板收起；展开后显示开源 / Pro 卡带状态与商业版说明。"""

import shutil

import gradio as gr
import pytest

from app.engine.runtime import EngineRuntime
from app.engine.skill_loader import SKILLS_DIR, SkillRegistry
from app.settings import Settings
from app.ui.app import build_app
from app.ui.header import SLOT_LABELS
from app.ui.pro_panel import BADGE_LABEL, COMMERCIAL_MD, panel_updates, skill_status_md

PRO_CARD = "---\nskill_id: pro_pet_planner\nname: 宠物营养补充剂行业包\n---\n正文\n"


@pytest.fixture
def skills_copy(tmp_path):
    dst = tmp_path / "skills"
    shutil.copytree(SKILLS_DIR, dst)
    return dst


def test_badge_is_in_top_bar_and_panel_starts_hidden(tmp_path):
    rt = EngineRuntime(settings=Settings(workspace_dir=tmp_path), force_demo=True)
    blocks = list(build_app(rt.settings, runtime=rt).blocks.values())
    badges = [b for b in blocks if isinstance(b, gr.Button) and b.value == BADGE_LABEL]
    assert len(badges) == 1 and "pro-badge" in badges[0].elem_classes
    panels = [b for b in blocks if isinstance(b, gr.Column) and "pro-panel" in (b.elem_classes or [])]
    assert len(panels) == 1 and panels[0].visible is False


def test_status_without_pro_pack_explains_how_to_install(skills_copy, tmp_path):
    reg = SkillRegistry(skills_copy, pro_dir=tmp_path / "none")
    md = skill_status_md(reg, SLOT_LABELS)
    assert "未安装 Pro 卡带包" in md and "config/skills_pro/" in md
    assert "| 电商策划 | 1 | — |" in md


def test_status_lists_loaded_pro_cards_and_errors(skills_copy):
    pro = skills_copy.with_name("skills_pro") / "ecommerce_planner"
    pro.mkdir(parents=True)
    (pro / "pet.md").write_text(PRO_CARD, encoding="utf-8")
    (pro / "broken.md").write_text("没有元数据", encoding="utf-8")
    md = skill_status_md(SkillRegistry(skills_copy), SLOT_LABELS)
    assert "👑 Pro 卡带 1 张" in md and "| 电商策划 | 1 | 1 |" in md
    assert "[👑 Pro] 宠物营养补充剂行业包（电商策划）" in md
    assert "⚠️ 无法加载：broken.md" in md


def test_toggle_counts_cards_only_when_opening(skills_copy, tmp_path):
    reg = SkillRegistry(skills_copy, pro_dir=tmp_path / "none")
    shown, state, status = panel_updates(True, reg, SLOT_LABELS)
    assert shown["visible"] is True and state is True and "卡带状态" in status
    hidden, state, status = panel_updates(False, reg, SLOT_LABELS)
    assert hidden["visible"] is False and state is False and status == gr.update()


def test_commercial_copy_has_three_tiers_and_contact_placeholders():
    for tier in ("开源社区版", "商业专业版", "企业定制版"):
        assert tier in COMMERCIAL_MD
    assert "微信" in COMMERCIAL_MD and "邮箱" in COMMERCIAL_MD and "公众号" in COMMERCIAL_MD
    assert "老庹 AI 工作室" in COMMERCIAL_MD and "老拓" not in COMMERCIAL_MD
