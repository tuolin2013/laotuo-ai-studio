"""本草性状生图：原料性状图谱完整、识别画面提到的原料、按需附上指南，且全程不出现功效表述。"""

import pytest
from pydantic import ValidationError

from app.engine.compiler import HERB_GUIDE, compile_image_prompt, compile_planner
from app.engine.config_loader import load_categories, load_platforms
from app.engine.prompt_compiler import shot_frame_data, slot_prompt_data
from app.engine.skill_loader import SKILLS_DIR, SkillRegistry
from app.schemas.config import MAX_HERBS_IN_FRAME, ProductLine
from app.schemas.plan import ImageSlot
from app.schemas.video import StoryboardShot

PET = "pet_supplement"
TEA = "zhangjiajie_berry_tea"
CATS = load_categories()
PLATS = load_platforms()
GUIDE_PATH = SKILLS_DIR / "image_prompt" / "references" / HERB_GUIDE
# 图谱与指南里不得出现的功效 / 医疗表述（类目违禁词之外再加一层）
EFFICACY_HINTS = ["功效", "主治", "疗效", "改善", "调理", "滋补", "补益", "养护", "缓解", "助眠", "护肝",
                  "health benefit", "remedy", "cure", "heal", "treat", "medicinal effect"]


def _slot(visual: str, **kw) -> dict:
    return slot_prompt_data(ImageSlot(title=kw.pop("title", "草本配方看得见"), visual_description=visual,
                                      generation_mode="text_to_image", aspect_ratio="1:1", **kw))


def _compile(skill_id: str, data: dict, category: str = PET, roles: list[str] | None = None) -> str:
    card = SkillRegistry().get("image_prompt", skill_id)
    return compile_image_prompt(card, data, CATS[category], PLATS["taobao"], "1:1", roles or [])


# ---------------------------------------------------------------- 图谱数据


def test_every_ingredient_has_a_visual_entry():
    """7 款单品用到的每一味草本原料都有性状描述，且推荐入画的原料都在本款原料组成内。"""
    pet = CATS[PET]
    names = {v.name for v in pet.ingredient_visuals}
    herbs = {i for p in pet.product_lines for i in p.ingredients} - {"鸡肝粉", "糊精"}
    assert herbs == names and len(names) == 44
    for p in pet.product_lines:
        assert 3 <= len(p.hero_ingredients) <= MAX_HERBS_IN_FRAME, p.name
        assert set(p.hero_ingredients) <= set(p.ingredients), p.name
    for v in pet.ingredient_visuals:
        assert v.botanical and v.part and v.appearance and v.prompt_en, v.name
        assert set(v.confusable) <= names, v.name
    assert "herb_ingredients" in pet.reference_image_roles


def test_hero_ingredients_must_come_from_ingredients():
    with pytest.raises(ValidationError, match="不在原料组成里"):
        ProductLine(name="x", ingredients=["鸡肝粉", "杜仲"], hero_ingredients=["人参"])


def test_visual_data_and_guide_contain_no_efficacy_wording():
    """性状图谱只写外观：不出现类目违禁词、平台违禁词与功效暗示。"""
    pet = CATS[PET]
    banned = set(EFFICACY_HINTS) | set(pet.forbidden_words)
    for plat in PLATS.values():
        banned |= set(plat.forbidden_words) - {"最"}  # 「最」会误伤「最多 4 味」等说明文字，单独检查
    for v in pet.ingredient_visuals:
        text = " ".join([v.appearance, v.part, v.prompt_en]).lower()
        assert not [w for w in banned if w.lower() in text], v.name
        assert "最" not in v.appearance
    guide = GUIDE_PATH.read_text(encoding="utf-8")
    assert not [w for w in pet.forbidden_words if w in guide]
    assert not [w for w in ["功效", "主治", "疗效", "改善", "调理", "滋补"] if w in guide]


# ---------------------------------------------------------------- 识别画面提到的原料


def test_mentions_follow_order_and_prefer_longest_name():
    pet = CATS[PET]
    found = pet.ingredients_mentioned("前景三个白瓷小碟：土茯苓、远志、诃子，背后是枸杞子")
    assert [v.name for v in found] == ["土茯苓", "远志", "诃子", "枸杞子"]
    # 「土茯苓」「枸杞子」不会被重复算作「茯苓」「枸杞」
    assert [v.name for v in pet.ingredients_mentioned("土茯苓薄片")] == ["土茯苓"]
    # 别名也能识别：首乌藤 = 夜交藤，山萸肉 = 山茱萸
    assert [v.name for v in pet.ingredients_mentioned("首乌藤与山萸肉")] == ["夜交藤", "山茱萸"]


def test_color_and_smell_words_are_not_ingredients():
    pet = CATS[PET]
    assert pet.ingredients_mentioned("姜黄色背景，空气里有原木香气") == []
    assert [v.name for v in pet.ingredients_mentioned("姜黄色背景前摆一碟姜黄")] == ["姜黄"]


def test_only_visual_fields_are_scanned():
    """copy_text 里列出整张原料表是上图文字，不代表每味都要画出实物。"""
    pet = CATS[PET]
    data = _slot("包装盒居中，背景草本线描", copy_text="杜仲 补骨脂 苍术 茯苓 党参 木香 姜黄 川芎 薏苡仁")
    assert pet.ingredients_in(data) == []


# ---------------------------------------------------------------- 编译生图指令


@pytest.mark.parametrize("skill_id", ["gpt_image_2", "gemini_nano_banana"])
def test_herb_slot_gets_guide_and_atlas(skill_id):
    data = _slot("包装盒右侧，前景三个白瓷小碟分别盛杜仲、姜黄、补骨脂")
    text = _compile(skill_id, data)
    assert "{{" not in text
    assert f'<reference_file path="references/{HERB_GUIDE}">' in text and "六条铁律" in text
    atlas = text[text.index("# 原料性状图谱"):]
    # 只附画面提到的 3 味，英文描述原样给出，真实尺寸在内
    assert [line.split("**")[1] for line in atlas.splitlines() if line.startswith("- **")] == ["杜仲", "姜黄", "补骨脂"]
    assert "silvery-white elastic latex threads" in atlas and "3-5 mm long" in atlas
    assert "金银花" not in atlas


def test_non_herb_slots_skip_herb_guide():
    """不提原料的槽位（喂食场景、莓茶类目）不附本草指南，省 token 也避免误导。"""
    for category, visual in [(PET, "猫咪在阳光窗台边舔食掌心的片剂"), (TEA, "礼盒居中，云雾峰林背景")]:
        text = _compile("gpt_image_2", _slot(visual), category)
        assert HERB_GUIDE not in text and "# 原料性状图谱" not in text
        assert "ecommerce_aesthetics_guide.md" in text


def test_atlas_is_not_in_category_json():
    """几十味原料的图谱不整体塞进 category_json，只按画面提到的原料另附。"""
    text = _compile("gpt_image_2", _slot("包装盒与一碟杜仲"))
    head = text[:text.index("# Attached Reference Guide")]
    assert "ingredient_visuals" not in head and "silvery-white" not in head
    card = SkillRegistry().get("ecommerce_planner", "default_planner")
    plan = compile_planner(card, "main_image", CATS[PET], PLATS["taobao"], "1:1", 5, None)
    assert "ingredient_visuals" not in plan and '"hero_ingredients"' in plan


def test_confusable_pairs_and_frame_limit():
    data = _slot("五个小碟依次摆放：白术、苍术、木香、川芎、党参")
    text = _compile("gpt_image_2", data)
    atlas = text[text.index("# 原料性状图谱"):]
    assert "同框易混组合" in atlas and "白术 ↔ 苍术" in atlas and "川芎 ↔ 木香" in atlas
    # 单图最多 4 味，第 5 味只提示不画
    assert "- **党参**" not in atlas and "本图不画实物（可保留在中文文案里）：党参" in atlas


def test_first_frame_with_herbs_gets_atlas():
    """视频首帧画面提到原料时同样附上图谱。"""
    shot = StoryboardShot(duration_seconds=3, scene_description="木桌", first_frame_description="一碟金银花与包装盒",
                          audio_mode="ambient_sfx", ambient_sound_description="纸盒摩擦声")
    text = _compile("gemini_nano_banana", shot_frame_data(shot, "9:16"))
    assert "closed buds, not open flowers" in text


def test_herb_photo_reference_role_is_explained_in_skills():
    for skill_id in ("gpt_image_2", "gemini_nano_banana"):
        card = SkillRegistry().get("image_prompt", skill_id)
        assert "herb_ingredients" in card.body and "《原料性状图谱》" in card.body


# ---------------------------------------------------------------- 给人看的本草图谱（docs/）


def test_herb_atlas_doc_matches_category_config():
    """docs/herb_atlas_pet_supplement.md 由配置生成：数据改了而文档没重新生成时报错。"""
    from app.engine.herb_atlas import atlas_path, render_atlas

    path = atlas_path(PET)
    assert path.read_text(encoding="utf-8") == render_atlas(CATS[PET]), "请运行 python -m app.engine.herb_atlas"


def test_herb_atlas_covers_every_product_and_herb_with_source():
    from app.engine.herb_atlas import render_atlas

    pet = CATS[PET]
    text = render_atlas(pet)
    for p in pet.product_lines:
        assert f"## {p.name} · {p.subtitle}" in text
    for v in pet.ingredient_visuals:
        assert v.source_url.startswith("https://"), v.name
        assert f"[药典 2020]({v.source_url})" in text
    # 只写外观：不出现类目违禁词与功效暗示
    assert not [w for w in [*pet.forbidden_words, "功效", "主治", "疗效", "改善", "调理"] if w in text]
