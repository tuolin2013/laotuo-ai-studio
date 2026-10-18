"""电商图片三维设计体系：数据模型、表格编辑、策划 / 生图 Skill 编译、演示策划与演示渲染。"""

import pytest
from PIL import Image, ImageStat

from app.engine.compiler import compile_image_prompt, compile_planner
from app.engine.config_loader import load_categories, load_platforms
from app.engine.planner_engine import PlannerEngine
from app.engine.prompt_compiler import shot_frame_data, slot_prompt_data
from app.engine.runtime import EngineRuntime
from app.engine.skill_loader import SkillRegistry
from app.providers.demo.content import demo_image_prompt, demo_plan
from app.providers.demo.render import DemoArt, render_demo_image
from app.schemas.plan import (
    AESTHETIC_STYLE_NAMES,
    COMPOSITION_MODE_NAMES,
    AestheticStyle,
    CompositionMode,
    ImagePlan,
    ImageSlot,
    design_code,
    slot_from_llm,
)
from app.schemas.video import StoryboardShot
from app.settings import Settings
from app.ui.state import SLOT_HEADERS, UIError, apply_slot_rows, slots_to_rows

TEA = "zhangjiajie_berry_tea"
PET = "pet_supplement"
CATS, PLATS = load_categories(), load_platforms()


def test_image_slot_with_aesthetics():
    slot = ImageSlot(
        title="主图首图",
        visual_description="莓茶礼盒配云海背景",
        generation_mode="single_image_edit",
        aspect_ratio="1:1",
        aesthetic_style="new_chinese_zen",
        composition_mode="rule_of_thirds_negative_space",
    )
    assert slot.aesthetic_style == "new_chinese_zen"
    assert slot.composition_mode == "rule_of_thirds_negative_space"


def test_slot_from_llm_with_auto_defaults():
    data = {"title": "白霜特写", "visual_description": "微距拍摄银白色霜层", "generation_mode": "text_to_image",
            "required_references": []}
    slot = slot_from_llm(data, default_aspect_ratio="1:1")
    assert (slot.aesthetic_style, slot.composition_mode, slot.aspect_ratio) == ("auto", "auto", "1:1")


def test_slot_from_llm_with_explicit_aesthetics():
    data = {
        "title": "科学配方图",
        "visual_description": "实验室风格产品展示",
        "generation_mode": "single_image_edit",
        "aesthetic_style": "clinical_lab_minimal",
        "composition_mode": "knolling_flat_lay",
        "required_references": [{"role": "product_package", "description": "包装正面"}],
    }
    slot = slot_from_llm(data, default_aspect_ratio="3:4")
    assert slot.aesthetic_style == "clinical_lab_minimal"
    assert slot.composition_mode == "knolling_flat_lay"
    assert len(slot.required_references) == 1


def test_aesthetic_style_literal_values():
    valid_styles: list[AestheticStyle] = ["auto", *AESTHETIC_STYLE_NAMES]
    for style in valid_styles:
        slot = ImageSlot(title="测试", visual_description="测试", generation_mode="text_to_image",
                         aspect_ratio="1:1", aesthetic_style=style)
        assert slot.aesthetic_style == style


def test_composition_mode_literal_values():
    valid_modes: list[CompositionMode] = ["auto", *COMPOSITION_MODE_NAMES]
    for mode in valid_modes:
        slot = ImageSlot(title="测试", visual_description="测试", generation_mode="text_to_image",
                         aspect_ratio="1:1", composition_mode=mode)
        assert slot.composition_mode == mode


# ---------------------------------------------------------------- 名称解析（LLM 输出 / 表格输入）


@pytest.mark.parametrize(("value", "expected"), [
    ("new_chinese_zen", "new_chinese_zen"),
    ("`new_chinese_zen`", "new_chinese_zen"),
    ("新中式茶席禅意", "new_chinese_zen"),
    ("🍵 新中式茶席禅意", "new_chinese_zen"),
    ("新中式", "new_chinese_zen"),
    ("", "auto"),
    (None, "auto"),
    ("AUTO", "auto"),
    ("自动", "auto"),
    ("赛博朋克", None),
])
def test_design_code_accepts_codes_and_chinese_names(value, expected):
    assert design_code(value, AESTHETIC_STYLE_NAMES) == expected


def test_design_code_rejects_ambiguous_partial_names():
    # 「构图」同时出现在多个构图名称中，不能随便猜一个
    assert design_code("构图", COMPOSITION_MODE_NAMES) is None
    assert design_code("三分留白", COMPOSITION_MODE_NAMES) == "rule_of_thirds_negative_space"


def test_unknown_llm_values_fall_back_to_auto():
    """LLM 自造的流派名不让整套策划失败，退回 auto 交给生图 Skill 自动匹配。"""
    slot = slot_from_llm({"title": "t", "visual_description": "v", "generation_mode": "text_to_image",
                          "aesthetic_style": "赛博朋克", "composition_mode": "居中"}, "1:1")
    assert (slot.aesthetic_style, slot.composition_mode) == ("auto", "auto")


# ---------------------------------------------------------------- 表格编辑


def _plan() -> ImagePlan:
    plan = ImagePlan(deliverable_type="main_image", platform_id="taobao", category_id=TEA,
                     planner_skill_id="default_planner", default_aspect_ratio="1:1")
    plan.replace_all_items([
        ImageSlot(title="霜厚回甘", visual_description="v1", generation_mode="text_to_image", aspect_ratio="1:1",
                  aesthetic_style="new_chinese_zen", composition_mode="rule_of_thirds_negative_space"),
        ImageSlot(title="白底图", visual_description="v2", generation_mode="text_to_image", aspect_ratio="1:1"),
    ])
    return plan


def test_table_shows_chinese_names_and_round_trips_edits():
    plan = _plan()
    rows = slots_to_rows(plan)
    assert all(len(r) == len(SLOT_HEADERS) == 9 for r in rows)
    assert rows[0][6:8] == ["新中式茶席禅意", "三分留白构图"]
    assert rows[1][6:8] == ["自动", "自动"]
    assert apply_slot_rows(plan, rows, {}) == 0  # 原样写回不算修改

    rows[1][6], rows[1][7] = "🔬 科学实证极简", "knolling_flat_lay"
    assert apply_slot_rows(plan, rows, {}) == 1
    second = plan.slots[1]
    assert (second.aesthetic_style, second.composition_mode) == ("clinical_lab_minimal", "knolling_flat_lay")


def test_table_rejects_unknown_style_with_chinese_hint():
    plan = _plan()
    rows = slots_to_rows(plan)
    rows[0][6] = "赛博朋克"
    with pytest.raises(UIError, match="美学流派「赛博朋克」无效"):
        apply_slot_rows(plan, rows, {})


# ---------------------------------------------------------------- 策划：全局美学流派


def test_compile_planner_appends_global_style_only_when_chosen():
    card = SkillRegistry().get("ecommerce_planner", "laotuo_zhangjiajie_meicha_planner")
    args = (card, "main_image", CATS[TEA], PLATS["taobao"], "1:1", 5, None)
    assert "# 全局美学流派" not in compile_planner(*args)
    text = compile_planner(*args, aesthetic_style="nordic_warm_lifestyle")
    assert "`nordic_warm_lifestyle`（北欧暖阳治愈）" in text and "{{" not in text


def test_planner_cards_ask_for_aesthetics():
    reg = SkillRegistry()
    for card in reg.list("ecommerce_planner"):
        assert "aesthetic_style" in card.body and "composition_mode" in card.body, card.skill_id


def test_demo_plan_auto_assigns_real_styles_and_layout():
    plan = demo_plan("main_image", CATS[TEA], PLATS["taobao"], 5, "1:1", None, {})
    slots = plan["slots"]
    styles = {s["aesthetic_style"] for s in slots}
    assert "auto" not in styles and len(styles) >= 2
    assert slots[0]["aesthetic_style"] == "new_chinese_zen"
    assert all(s["composition_mode"] in COMPOSITION_MODE_NAMES for s in slots)
    assert all("留白" in s["visual_description"] for s in slots)
    # 拼多多首图走高对比零售视觉锤；宠物营养品走科学实证
    assert demo_plan("main_image", CATS[TEA], PLATS["pinduoduo"], 1, "1:1", None, {})["slots"][0][
        "aesthetic_style"] == "high_impact_retail"
    assert demo_plan("main_image", CATS[PET], PLATS["taobao"], 1, "1:1", None, {})["slots"][0][
        "aesthetic_style"] == "clinical_lab_minimal"


async def test_global_style_applies_to_every_slot_and_regeneration(tmp_path):
    rt = EngineRuntime(settings=Settings(workspace_dir=tmp_path), demo_delay=0)
    planner = PlannerEngine(rt)
    plan, _ = await planner.generate(deliverable_type="detail_page", category_id=TEA, platform_id="taobao",
                                     aspect_ratio="3:4", count=6, user_creative_idea=None,
                                     aesthetic_style="high_impact_retail")
    assert plan.aesthetic_style == "high_impact_retail"
    assert {s.aesthetic_style for s in plan.slots} == {"high_impact_retail"}
    new = await planner.regenerate_slot(plan, plan.slots[2].slot_id)
    assert new.aesthetic_style == "high_impact_retail"

    auto, _ = await planner.generate(deliverable_type="main_image", category_id=TEA, platform_id="taobao",
                                     aspect_ratio="1:1", count=5, user_creative_idea=None)
    assert auto.aesthetic_style == "auto" and len({s.aesthetic_style for s in auto.slots}) >= 2


# ---------------------------------------------------------------- 生图 Skill


def test_image_skills_attach_aesthetics_guide():
    reg = SkillRegistry()
    slot = ImageSlot(title="霜厚回甘", visual_description="v", generation_mode="text_to_image", aspect_ratio="1:1",
                     aesthetic_style="new_chinese_zen", composition_mode="extreme_macro_texture")
    data = slot_prompt_data(slot)
    assert data["aesthetic_style"] == "new_chinese_zen" and data["composition_mode"] == "extreme_macro_texture"
    for skill_id in ("gpt_image_2", "gemini_nano_banana"):
        card = reg.get("image_prompt", skill_id)
        assert list(card.reference_texts) == ["references/ecommerce_aesthetics_guide.md"]
        text = compile_image_prompt(card, data, CATS[TEA], PLATS["taobao"], "1:1", [])
        assert '"aesthetic_style": "new_chinese_zen"' in text
        assert "# Attached Reference Guide" in text and "铁律 4" in text and "{{" not in text


def test_demo_prompt_uses_style_palette_font_and_layout():
    slot = slot_prompt_data(ImageSlot(
        title="霜厚回甘", subtitle="张家界莓茶", copy_text="霜厚回甘 张家界莓茶 头采嫩芽", visual_description="礼盒",
        generation_mode="single_image_edit", aspect_ratio="1:1", aesthetic_style="new_chinese_zen",
        composition_mode="rule_of_thirds_negative_space"))
    gpt = demo_image_prompt("gpt_image_2", slot, ["product_package"], "1:1", CATS[TEA])["prompt"]
    assert "#1B3328" in gpt and "Songti" in gpt and "lower-left third" in gpt
    assert '"霜厚回甘"' in gpt and '"头采嫩芽"' in gpt and "pill-shaped badge" in gpt
    gemini = demo_image_prompt("gemini_nano_banana", slot, [], "1:1", CATS[TEA])["prompt"]
    assert "比例" in gemini and "宋刻衬线体" in gemini and "竖排" in gemini


def test_first_frame_prompt_keeps_classic_style():
    """视频首帧没有美学字段：仍按类目色板编译，不排海报文字。"""
    shot = StoryboardShot(duration_seconds=3, scene_description="桌面", first_frame_description="玻璃杯",
                          audio_mode="ambient_sfx", ambient_sound_description="水声")
    out = demo_image_prompt("gpt_image_2", shot_frame_data(shot, "9:16"), [], "9:16", CATS[TEA])["prompt"]
    assert "Color palette: " + "、".join(CATS[TEA].visual_style.palette) in out
    assert "headline" not in out


# ---------------------------------------------------------------- 演示渲染


def _band_luma(path, top: float, bottom: float) -> float:
    with Image.open(path) as im:
        g = im.convert("L")
        w, h = g.size
        return ImageStat.Stat(g.crop((0, int(h * top), w, int(h * bottom)))).mean[0]


@pytest.mark.parametrize("composition", list(COMPOSITION_MODE_NAMES))
def test_demo_render_supports_every_composition(tmp_path, composition):
    art = DemoArt(title="霜厚回甘", subtitle="张家界莓茶", pill="头采嫩芽", aesthetic="clinical_lab_minimal",
                  composition=composition, palette=CATS[TEA].visual_style.palette, seed="s")
    out = render_demo_image(art, "3:4", tmp_path / f"{composition}.png")
    with Image.open(out) as im:
        assert im.size == (768, 1024)


def test_demo_render_follows_style_palette(tmp_path):
    def render(style):
        art = DemoArt(title="霜厚回甘", aesthetic=style, composition="rule_of_thirds_negative_space", seed="s")
        return render_demo_image(art, "1:1", tmp_path / f"{style}.png")

    dark, light = render("new_chinese_zen"), render("clinical_lab_minimal")
    assert _band_luma(dark, 0.0, 0.3) + 60 < _band_luma(light, 0.0, 0.3)


def test_demo_render_without_composition_uses_classic_layout(tmp_path):
    out = render_demo_image(DemoArt(title="镜头 1 · 首帧", palette=CATS[TEA].visual_style.palette), "9:16",
                            tmp_path / "classic.png")
    with Image.open(out) as im:
        assert im.size == (576, 1024)
