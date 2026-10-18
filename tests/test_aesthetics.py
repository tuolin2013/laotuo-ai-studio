"""测试电商图片三维设计体系的数据模型与转换。"""


from app.schemas.plan import (
    AestheticStyle,
    CompositionMode,
    ImageSlot,
    slot_from_llm,
)


def test_image_slot_with_aesthetics():
    """测试 ImageSlot 包含美学与构图字段。"""
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
    """测试从 LLM 输出转换时，未指定美学与构图会默认为 auto。"""
    data = {
        "title": "白霜特写",
        "visual_description": "微距拍摄银白色霜层",
        "generation_mode": "text_to_image",
        "required_references": [],
    }
    slot = slot_from_llm(data, default_aspect_ratio="1:1")
    assert slot.aesthetic_style == "auto"
    assert slot.composition_mode == "auto"
    assert slot.aspect_ratio == "1:1"


def test_slot_from_llm_with_explicit_aesthetics():
    """测试从 LLM 输出转换时，显式指定的美学与构图会被保留。"""
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
    """测试美学流派的所有合法值。"""
    valid_styles: list[AestheticStyle] = [
        "auto",
        "new_chinese_zen",
        "organic_nature_sunlight",
        "clinical_lab_minimal",
        "nordic_warm_lifestyle",
        "high_impact_retail",
    ]
    for style in valid_styles:
        slot = ImageSlot(
            title="测试",
            visual_description="测试",
            generation_mode="text_to_image",
            aspect_ratio="1:1",
            aesthetic_style=style,
        )
        assert slot.aesthetic_style == style


def test_composition_mode_literal_values():
    """测试构图模式的所有合法值。"""
    valid_modes: list[CompositionMode] = [
        "auto",
        "rule_of_thirds_negative_space",
        "extreme_macro_texture",
        "depth_layering",
        "diagonal_dynamic_flow",
        "split_screen_comparison",
        "knolling_flat_lay",
    ]
    for mode in valid_modes:
        slot = ImageSlot(
            title="测试",
            visual_description="测试",
            generation_mode="text_to_image",
            aspect_ratio="1:1",
            composition_mode=mode,
        )
        assert slot.composition_mode == mode
