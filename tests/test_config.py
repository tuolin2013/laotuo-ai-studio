import json

import pytest

from app.engine.config_loader import CONFIG_DIR, ConfigError, load_categories, load_platforms


def test_load_two_categories():
    cats = load_categories()
    assert set(cats) == {"zhangjiajie_berry_tea", "pet_supplement"}
    tea = cats["zhangjiajie_berry_tea"]
    assert tea.name == "张家界莓茶"
    assert "product_package" in tea.reference_image_roles
    assert "inspection_report" in tea.reference_image_roles
    assert tea.ambient_sound_library and tea.forbidden_words
    assert "pet_model" in cats["pet_supplement"].reference_image_roles


def test_load_three_platforms():
    plats = load_platforms()
    assert set(plats) == {"taobao", "pinduoduo", "wechat_shop"}
    for p in plats.values():
        assert p.main_image.min_count <= p.main_image.default_count <= p.main_image.max_count
        assert p.detail_page.min_screens <= p.detail_page.default_screens <= p.detail_page.max_screens
        assert p.video.default_aspect_ratio in p.video.allowed_aspect_ratios
    for p in plats.values():
        assert set(p.main_image.allowed_aspect_ratios) >= {"1:1", "3:4", "9:16", "16:9"}
        assert set(p.video.allowed_aspect_ratios) == {"9:16", "16:9", "1:1"}
        assert p.visual_style_weights
    assert load_categories()["pet_supplement"].product_form_fields[0].label == "适用犬猫"


def test_config_id_must_match_filename(tmp_path):
    (tmp_path / "platforms").mkdir()
    data = json.loads((CONFIG_DIR / "platforms" / "taobao.json").read_text(encoding="utf-8"))
    (tmp_path / "platforms" / "tmall.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="文件名"):
        load_platforms(tmp_path)


def test_invalid_default_ratio_rejected(tmp_path):
    (tmp_path / "platforms").mkdir()
    data = json.loads((CONFIG_DIR / "platforms" / "taobao.json").read_text(encoding="utf-8"))
    data["video"]["default_aspect_ratio"] = "21:9"
    (tmp_path / "platforms" / "taobao.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_platforms(tmp_path)


def test_pet_supplement_product_lines_from_package():
    """宠物营养补充剂按辰荣贝康包装登记 7 款单品，各自的原料组成不同。"""
    pet = load_categories()["pet_supplement"]
    lines = {p.name: p for p in pet.product_lines}
    assert set(lines) == {"猫静宝", "重重没", "护圣宝", "艾留平", "护宫宝", "感克消", "辰节宁"}
    assert lines["猫静宝"].ingredients == ["鸡肝粉", "糊精", "夜交藤", "酸枣仁", "远志", "葛根", "甘草"]
    assert lines["辰节宁"].standard == "Q/CSDD 245-2026" and "妊娠期禁用" in lines["辰节宁"].cautions
    assert len({tuple(p.ingredients) for p in pet.product_lines}) == 7
    for p in pet.product_lines:
        assert p.ingredients[:2] == ["鸡肝粉", "糊精"] and p.positioning and p.guaranteed_analysis
