import os
import shutil
import time

import pytest

from app.engine.skill_loader import SKILLS_DIR, SkillRegistry
from app.schemas.skill import SKILL_SLOTS, SkillRenderError

EXPECTED = {
    "ecommerce_planner": {"default_planner"},
    "video_scriptwriter": {"default_scriptwriter"},
    "image_prompt": {"gpt_image_2", "gemini_nano_banana"},
    "video_prompt": {"official_h3_prompt_writing", "minimax_h3_prompt"},
}


@pytest.fixture
def skills_copy(tmp_path):
    dst = tmp_path / "skills"
    shutil.copytree(SKILLS_DIR, dst)
    return dst


def test_builtin_skills_scanned_in_all_four_slots():
    reg = SkillRegistry()
    assert reg.errors == {}
    for slot in SKILL_SLOTS:
        assert {c.skill_id for c in reg.list(slot)} == EXPECTED[slot]


def test_builtin_skills_declare_all_placeholders():
    """每个卡带正文用到的变量都已在元数据 variables 中声明，反之亦然。"""
    reg = SkillRegistry()
    for slot in SKILL_SLOTS:
        for card in reg.list(slot):
            assert card.placeholders == set(card.variables), card.skill_id
            assert card.name and card.target_models


def test_render_substitutes_and_rejects_missing_vars():
    card = SkillRegistry().get("video_prompt", "minimax_h3_prompt")
    values = {v: f"<{v}>" for v in card.variables}
    out = card.render(**values)
    assert "<shot_json>" in out and "{{" not in out
    values.pop("aspect_ratio")
    with pytest.raises(SkillRenderError, match="aspect_ratio"):
        card.render(**values)


NEW_CARD = """---
skill_id: festival_planner
name: 节日大促策划
description: 从 GitHub 下载的新卡带
target_models: [claude-opus-5-5]
variables: [user_creative_idea]
---
节日主题：{{ user_creative_idea }}
"""


def test_hot_load_new_card_modify_and_remove(skills_copy):
    reg = SkillRegistry(skills_copy)
    path = skills_copy / "ecommerce_planner" / "festival_planner.md"
    path.write_text(NEW_CARD, encoding="utf-8")

    assert reg.refresh() is True
    card = reg.get("ecommerce_planner", "festival_planner")
    assert card.render(user_creative_idea="中秋") == "节日主题：中秋\n"
    assert ("节日大促策划（festival_planner）", "festival_planner") in reg.choices("ecommerce_planner")

    # 修改文件后热重载
    path.write_text(NEW_CARD.replace("节日主题", "大促主题"), encoding="utf-8")
    future = time.time() + 5
    os.utime(path, (future, future))
    reg.refresh()
    assert reg.get("ecommerce_planner", "festival_planner").body.startswith("大促主题")

    # 删除后消失
    path.unlink()
    reg.refresh()
    with pytest.raises(KeyError):
        reg.get("ecommerce_planner", "festival_planner")


def test_broken_card_is_reported_without_breaking_others(skills_copy):
    bad = skills_copy / "image_prompt" / "broken.md"
    bad.write_text("没有元数据的卡带", encoding="utf-8")
    dup = skills_copy / "image_prompt" / "dup.md"
    dup.write_text("---\nskill_id: gpt_image_2\nname: 重复\n---\nx", encoding="utf-8")
    reg = SkillRegistry(skills_copy)
    assert bad in reg.errors and dup in reg.errors
    assert {c.skill_id for c in reg.list("image_prompt")} == EXPECTED["image_prompt"]


def test_override_is_temporary_and_not_written_to_disk(skills_copy):
    reg = SkillRegistry(skills_copy)
    original = reg.get("video_scriptwriter", "default_scriptwriter")
    disk_before = original.source_path.read_text(encoding="utf-8")

    reg.set_override("video_scriptwriter", "default_scriptwriter", "临时指令")
    overridden = reg.get("video_scriptwriter", "default_scriptwriter")
    assert overridden.body == "临时指令" and overridden.is_override
    assert original.source_path.read_text(encoding="utf-8") == disk_before

    reg.clear_override("video_scriptwriter", "default_scriptwriter")
    assert reg.get("video_scriptwriter", "default_scriptwriter").body == original.body
