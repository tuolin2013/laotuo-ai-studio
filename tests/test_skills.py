import os
import shutil
import time

import pytest

from app.engine.skill_loader import SKILLS_DIR, SkillRegistry
from app.schemas.skill import PRO_BADGE, SKILL_SLOTS, SkillRenderError

EXPECTED = {
    "concept_writer": {"default_concept_writer"},
    "story_scriptwriter": {"comparison_review_story", "emotional_story_story", "lifestyle_seeding_story",
                           "product_showcase_story", "trust_building_story"},
    # 行业专属策划卡带属于商业 Pro 卡带包（config/skills_pro/），不在开源仓库里
    "ecommerce_planner": {"default_planner"},
    "video_scriptwriter": {"default_scriptwriter", "hypit_viral_clone"},
    "image_prompt": {"gpt_image_2", "gemini_nano_banana"},
    "video_prompt": {"official_h3_prompt_writing", "minimax_h3_prompt"},
    "video_editor": {"default_video_editor"},
    "voice_writer": {"default_voice_writer"},
    "motion_director": {"default_motion_director"},
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
        # 本机装了 Pro 卡带包时一并加载，这里只核对开源卡带
        assert {c.skill_id for c in reg.list(slot) if not c.is_pro} == EXPECTED[slot]


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


TEA_ONLY = """---
skill_id: tea_only_planner
name: 莓茶专属策划
categories: [zhangjiajie_berry_tea]
---
莓茶
"""


def test_category_specific_planner_is_filtered_and_preferred(skills_copy):
    """类目专属卡带只在对应类目下出现，并排在第一位作为默认选中；通用卡带所有类目可见。"""
    (skills_copy / "ecommerce_planner" / "tea_only.md").write_text(TEA_ONLY, encoding="utf-8")
    reg = SkillRegistry(skills_copy)
    tea = [c.skill_id for c in reg.list("ecommerce_planner", "zhangjiajie_berry_tea")]
    assert tea == ["tea_only_planner", "default_planner"]
    assert [c.skill_id for c in reg.list("ecommerce_planner", "pet_supplement")] == ["default_planner"]
    assert reg.get("ecommerce_planner", "tea_only_planner").categories == ["zhangjiajie_berry_tea"]


# ---------------------------------------------------------------- 商业 Pro 卡带包（config/skills_pro/）

PRO_CARD = """---
skill_id: pro_tea_planner
name: 莓茶行业高转化策划
categories: [zhangjiajie_berry_tea]
references: [references/ecommerce_copywriting_guide.md]
target_models: [claude-opus-5-5]
variables: [user_creative_idea]
---
Pro 指令：{{ user_creative_idea }}
"""


def _pro_slot(skills_dir):
    """与开源卡带目录同级的 skills_pro/{slot}/，和 config/skills_pro/ 的位置关系一致。"""
    return skills_dir.with_name("skills_pro") / "ecommerce_planner"


def test_pro_pack_is_loaded_with_badge(skills_copy):
    pro = _pro_slot(skills_copy)
    pro.mkdir(parents=True)
    (pro / "pro_tea.md").write_text(PRO_CARD, encoding="utf-8")
    reg = SkillRegistry(skills_copy)
    assert reg.errors == {}
    card = reg.get("ecommerce_planner", "pro_tea_planner")
    assert card.is_pro and card.display_name == f"{PRO_BADGE} 莓茶行业高转化策划"
    assert not reg.get("ecommerce_planner", "default_planner").is_pro
    # 按类目专属规则排第一；下拉框显示名带 Pro 标记
    assert reg.choices("ecommerce_planner", "zhangjiajie_berry_tea")[0] == (
        f"{PRO_BADGE} 莓茶行业高转化策划（pro_tea_planner）", "pro_tea_planner")
    assert card.render(user_creative_idea="中秋") == "Pro 指令：中秋\n"
    # Pro 卡带包不必携带开源仓库已有的附属指南：回落到开源卡槽的 references/
    guide = "references/ecommerce_copywriting_guide.md"
    assert "FAB" in card.reference_texts[guide]
    assert card.reference_paths == [(skills_copy / "ecommerce_planner" / guide).resolve()]


def test_without_pro_pack_only_open_source_cards_load(skills_copy, tmp_path):
    reg = SkillRegistry(skills_copy, pro_dir=tmp_path / "not_installed")
    assert reg.errors == {}
    assert not any(c.is_pro for slot in SKILL_SLOTS for c in reg.list(slot))
    assert {c.skill_id for c in reg.list("ecommerce_planner")} == EXPECTED["ecommerce_planner"]


def test_pro_card_cannot_replace_open_source_card(skills_copy):
    pro = _pro_slot(skills_copy)
    pro.mkdir(parents=True)
    fake = pro / "fake_default.md"
    fake.write_text(PRO_CARD.replace("pro_tea_planner", "default_planner"), encoding="utf-8")
    reg = SkillRegistry(skills_copy)
    assert "重复" in reg.errors[fake]
    assert not reg.get("ecommerce_planner", "default_planner").is_pro


def test_pro_pack_unzipped_while_running_is_hot_loaded(skills_copy):
    """服务运行中解压 Pro 卡带包：重新扫描即可加载；删除后消失。"""
    reg = SkillRegistry(skills_copy)
    pro = _pro_slot(skills_copy)
    pro.mkdir(parents=True)
    path = pro / "pro_tea.md"
    path.write_text(PRO_CARD, encoding="utf-8")
    assert reg.refresh() is True
    assert reg.get("ecommerce_planner", "pro_tea_planner").is_pro
    path.unlink()
    reg.refresh()
    with pytest.raises(KeyError):
        reg.get("ecommerce_planner", "pro_tea_planner")
