"""电商高转化文案写作指南：策划卡带挂载附属指南，且所有卡带的「正确示范」不含违禁词与功效暗示。"""

import re

from app.engine.compiler import compile_planner
from app.engine.config_loader import load_categories, load_platforms
from app.engine.skill_loader import SKILLS_DIR, SkillRegistry

GUIDE = "references/ecommerce_copywriting_guide.md"
TEA = "zhangjiajie_berry_tea"
# 类目违禁词之外，示范句里同样不该出现的功效暗示
HEALTH_HINTS = ["护嗓", "咽炎", "嗓子", "改善", "好转", "调理", "神器", "解酒", "护肝", "助眠"]


def _banned_words() -> list[str]:
    words = set(HEALTH_HINTS)
    for cat in load_categories().values():
        words.update(cat.forbidden_words)
    for plat in load_platforms().values():
        words.update(plat.forbidden_words)
    return sorted(words, key=len, reverse=True)


def test_all_planner_cards_load_copywriting_guide():
    reg = SkillRegistry()
    assert reg.errors == {}
    for card in reg.list("ecommerce_planner"):
        assert GUIDE in card.reference_texts, card.skill_id
        assert "FAB" in card.reference_texts[GUIDE]


def test_compile_planner_appends_guide_before_global_style():
    card = SkillRegistry().get("ecommerce_planner", "default_planner")
    args = (card, "main_image", load_categories()[TEA], load_platforms()["pinduoduo"], "1:1", 5, None)
    text = compile_planner(*args, aesthetic_style="new_chinese_zen")
    assert "{{" not in text
    guide_at = text.index(f'<reference_file path="{GUIDE}">')
    assert "诚实交底" in text[guide_at:]
    # 全局美学流派约束仍在最后，优先级不被附属指南覆盖
    assert guide_at < text.index("# 全局美学流派")


def test_correct_examples_in_skill_cards_are_compliant():
    """所有卡带与附属指南中 ✅ 示范句不得含违禁词或功效暗示，避免模型照着示范写出违规文案。"""
    banned = _banned_words()
    problems = []
    for path in sorted(SKILLS_DIR.rglob("*.md")):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "✅" not in line:
                continue
            example = line.split("✅", 1)[1]
            hits = [w for w in banned if w in example]
            if hits:
                problems.append(f"{path.relative_to(SKILLS_DIR)}:{no} {hits}")
    assert problems == []


def test_guide_opens_with_rule_zero_and_cat_bed_test():
    """第 0 定律（卖恐惧、欲望、体面、愧疚与想象）排在五条规律之前，但仍在合规红线之下。"""
    text = (SKILLS_DIR / "ecommerce_planner" / GUIDE).read_text(encoding="utf-8")
    zero = text[text.index("第 0 定律"):text.index("## 一、动笔前的五条规律")]
    assert "让它别再睡纸箱了" in zero and "### 猫窝测试" in zero
    assert "合规红线之下" in zero


def test_guide_examples_do_not_invent_numbers():
    """指南示范中不出现无来源的倍数与百分比。"""
    text = (SKILLS_DIR / "ecommerce_planner" / GUIDE).read_text(encoding="utf-8")
    assert not re.search(r"\d+\s*倍", text)
    assert not re.search(r"\d+(\.\d+)?\s*%", text)
