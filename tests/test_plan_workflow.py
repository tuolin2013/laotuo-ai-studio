import pytest

from app.engine.compiler import IDEA_PLACEHOLDER, compile_planner
from app.engine.config_loader import load_categories, load_platforms
from app.engine.skill_loader import SkillRegistry
from app.schemas import (
    ImagePlan,
    ImageSlot,
    PlanStateError,
    PlanValidationError,
    ReferenceRequirement,
    slot_from_llm,
)

# 模拟策划 Skill 的 LLM 输出
LLM_OUTPUT = {
    "plan_summary": "中秋暖金色调，突出白霜",
    "slots": [
        {
            "title": "霜厚回甘",
            "copy_text": "霜厚回甘 张家界莓茶",
            "selling_point": "白霜",
            "visual_description": "罐装莓茶居中，暖金色中秋月光背景",
            "generation_mode": "single_image_edit",
            "required_references": [{"role": "product_package", "description": "罐子正面"}],
            "unknown_field": "应被忽略",
        },
        {
            "title": "云雾产区",
            "visual_description": "张家界峰林晨雾",
            "generation_mode": "text_to_image",
            "required_references": [],
        },
        {
            "title": "一杯清亮",
            "visual_description": "包装与茶汤同框",
            "generation_mode": "multi_image_fusion",
            "required_references": [
                {"role": "product_package", "description": "罐子 3/4 侧面"},
                {"role": "tea_soup", "description": "透明杯茶汤"},
            ],
        },
    ],
}


def make_draft(idea: str | None = "中秋主题，暖金色调") -> ImagePlan:
    plan = ImagePlan(
        deliverable_type="main_image",
        platform_id="taobao",
        category_id="zhangjiajie_berry_tea",
        planner_skill_id="default_planner",
        default_aspect_ratio="1:1",
        user_creative_idea=idea,
    )
    plan.replace_all_items([slot_from_llm(s, "1:1") for s in LLM_OUTPUT["slots"]], idea)
    return plan


def test_creative_idea_is_rendered_into_planner_skill():
    reg = SkillRegistry()
    cats, plats = load_categories(), load_platforms()
    args = (reg.get("ecommerce_planner", "default_planner"), "main_image",
            cats["zhangjiajie_berry_tea"], plats["taobao"], "1:1", None)
    with_idea = compile_planner(*args, "中秋主题，暖金色调")
    assert "中秋主题，暖金色调" in with_idea and "{{" not in with_idea
    assert "requested_count" not in with_idea and "auto" in with_idea
    assert IDEA_PLACEHOLDER in compile_planner(*args, "   ")


def test_blank_idea_normalized_to_none():
    assert make_draft("  ").user_creative_idea is None


def test_draft_edit_then_confirm_updates_checklist():
    plan = make_draft()
    assert plan.plan_status == "draft"
    first, second, _ = plan.slots
    assert first.aspect_ratio == "1:1"

    # 确认前先看一下按原方案算出的清单
    before = {c.role: c.slot_ids for c in plan.compute_reference_checklist()}
    assert set(before) == {"product_package", "tea_soup"}

    # 用户手动改第 1 个槽位：改文案，并从 single_image_edit 改为 multi_image_fusion
    plan.update_slot(
        first.slot_id,
        title="中秋霜礼",
        copy_text="中秋霜礼",
        generation_mode="multi_image_fusion",
        required_references=[
            {"role": "product_package", "description": "罐子正面"},
            {"role": "inspection_report", "description": "检测报告首页"},
        ],
    )
    # 再删掉第 3 个槽位，tea_soup 就不再需要
    plan.remove_slot(plan.slots[2].slot_id)

    checklist = plan.confirm_plan()
    assert plan.plan_status == "confirmed"
    roles = {c.role: c for c in checklist}
    assert set(roles) == {"product_package", "inspection_report"}
    assert roles["product_package"].slot_ids == [first.slot_id]
    assert plan.slots[0].title == "中秋霜礼"
    assert plan.slots[0].generation_mode == "multi_image_fusion"
    assert second.slot_id not in roles["product_package"].slot_ids
    assert [r.action for r in plan.edit_log][-3:] == ["update_item", "remove_item", "confirm_plan"]


def test_checklist_dedupes_roles_across_slots():
    plan = make_draft()
    checklist = plan.confirm_plan()
    pkg = next(c for c in checklist if c.role == "product_package")
    assert pkg.slot_ids == [plan.slots[0].slot_id, plan.slots[2].slot_id]
    assert "罐子正面" in pkg.description and "3/4 侧面" in pkg.description


def test_mode_reference_mismatch_blocks_confirm():
    plan = make_draft()
    plan.update_slot(plan.slots[0].slot_id, generation_mode="multi_image_fusion")  # 只有 1 张参考图
    with pytest.raises(PlanValidationError) as exc:
        plan.confirm_plan()
    assert "第 1 项" in exc.value.issues[0]
    assert plan.plan_status == "draft"


def test_confirmed_plan_is_locked_until_unlocked():
    plan = make_draft()
    plan.confirm_plan()
    with pytest.raises(PlanStateError):
        plan.update_slot(plan.slots[0].slot_id, title="x")
    with pytest.raises(PlanStateError):
        plan.add_slot(ImageSlot(title="新", visual_description="v", generation_mode="text_to_image",
                                aspect_ratio="1:1"))
    plan.unlock_plan()
    plan.update_slot(plan.slots[0].slot_id, title="解锁后修改")
    assert plan.plan_status == "draft"


def test_unconfirmed_plan_blocks_stage2(tmp_path):
    plan = make_draft()
    with pytest.raises(PlanStateError):
        plan.require_confirmed()
    with pytest.raises(PlanStateError):
        plan.upload_reference("product_package", tmp_path / "a.png")


def test_add_move_regenerate_slot():
    plan = make_draft()
    new = plan.add_slot(
        ImageSlot(title="白底图", visual_description="纯白底", generation_mode="single_image_edit",
                  aspect_ratio="1:1", required_references=[ReferenceRequirement(role="product_package")]),
        index=0,
    )
    assert plan.slots[0].slot_id == new.slot_id
    plan.move_slot(new.slot_id, 3)
    assert plan.slots[-1].slot_id == new.slot_id

    old_id = plan.slots[1].slot_id
    regenerated = plan.regenerate_slot(old_id, slot_from_llm(LLM_OUTPUT["slots"][1] | {"title": "重生成"}, "1:1"))
    assert regenerated.slot_id == old_id and plan.slots[1].title == "重生成"
    with pytest.raises(ValueError):
        plan.update_slot(old_id, slot_id="hack")


def test_stage2_upload_binds_to_all_slots_and_per_slot_edits(tmp_path):
    plan = make_draft()
    plan.confirm_plan()
    s1, s2, s3 = plan.slots
    pkg = plan.upload_reference("product_package", tmp_path / "pkg.png")
    assert [r.role for r in plan.missing_references()] == ["tea_soup"]
    soup = plan.upload_reference("tea_soup", tmp_path / "soup.png")
    assert plan.missing_references() == []

    assert [a.asset_id for a in plan.references_for(s1.slot_id)] == [pkg.asset_id]
    assert plan.references_for(s2.slot_id) == []
    assert [a.asset_id for a in plan.references_for(s3.slot_id)] == [pkg.asset_id, soup.asset_id]

    # 单槽位独立增删
    extra = plan.add_slot_reference(s2.slot_id, tmp_path / "extra.png")
    assert [a.asset_id for a in plan.references_for(s2.slot_id)] == [extra.asset_id]
    plan.remove_slot_reference(s3.slot_id, soup.asset_id)
    assert [a.asset_id for a in plan.references_for(s3.slot_id)] == [pkg.asset_id]
    assert [a.asset_id for a in plan.references_for(s1.slot_id)] == [pkg.asset_id]

    with pytest.raises(ValueError):
        plan.upload_reference("not_in_checklist", tmp_path / "x.png")


def test_candidate_count_range():
    plan = make_draft()
    plan.confirm_plan()
    sid = plan.slots[0].slot_id
    plan.set_candidate_count(sid, 4)
    assert plan.slots[0].candidate_count == 4
    with pytest.raises(ValueError):
        plan.set_candidate_count(sid, 5)
