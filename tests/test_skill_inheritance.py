"""Skill 卡带继承：区块替换 / 追加 / 删除、abstract / final、目录方向、循环检测、元数据合并、热重载。"""

import os
import shutil
import time

import pytest

from app.engine.skill_inherit import InheritanceError, parse_blocks
from app.engine.skill_loader import SKILLS_DIR, SkillRegistry, describe_resolution

SLOT = "ecommerce_planner"

BASE = """---
skill_id: base_planner
name: 基类
description: 基类描述
version: "1.0"
target_models: [claude-opus-5-5]
variables: [user_creative_idea]
references: [references/ecommerce_copywriting_guide.md]
categories: [zhangjiajie_berry_tea]
default: true
---
# 角色 {#role}

基类角色

# 本次任务 {#task final}

想法：{{ user_creative_idea }}

# 防雷同 {#anti_duplicate}

基类防雷同

# 文案 {#copywriting}

基类文案

# 输出格式 {#output_format final}

基类输出
"""


def card(skill_id, body, extends=None, extra=""):
    ext = f"extends: {extends}\n" if extends else ""
    return f"---\nskill_id: {skill_id}\nname: {skill_id}\n{ext}{extra}---\n{body}"


@pytest.fixture
def dirs(tmp_path):
    skills = tmp_path / "skills"
    (skills / SLOT).mkdir(parents=True)
    shutil.copytree(SKILLS_DIR / SLOT / "references", skills / SLOT / "references")
    (skills / SLOT / "base.md").write_text(BASE, encoding="utf-8")
    pro = tmp_path / "skills_pro" / SLOT
    pro.mkdir(parents=True)
    return skills, pro


def write(d, name, text):
    (d / name).write_text(text, encoding="utf-8")


def titles(c):
    return [b.block_id for b in c.blocks]


def test_card_with_blocks_renders_without_markers(dirs):
    reg = SkillRegistry(dirs[0])
    base = reg.get(SLOT, "base_planner")
    assert "{#" not in base.body and base.body.startswith("# 角色\n\n基类角色")
    assert base.inheritance_chain == ["base_planner"]


def test_replace_append_prepend_remove_and_insert(dirs):
    skills, pro = dirs
    write(pro, "child.md", card("child_planner", """# 新角色 {#role}

子类角色

# 单品表 {#catalog after=task}

七款单品

# 场景 {#scene}

三幕场景

# {#anti_duplicate remove}

# 文案 {#copywriting append}

子类追加文案

# {#output_format prepend}

输出前置说明
""", extends="base_planner", extra="categories: [pet_supplement]\n"))
    reg = SkillRegistry(skills)
    assert reg.errors == {}
    c = reg.get(SLOT, "child_planner")
    assert titles(c) == ["role", "task", "catalog", "scene", "copywriting", "output_format"]
    assert "# 新角色\n\n子类角色" in c.body and "基类角色" not in c.body
    assert "基类防雷同" not in c.body
    assert "基类文案\n\n子类追加文案" in c.body
    assert "# 输出格式\n\n输出前置说明\n\n基类输出" in c.body
    assert c.inheritance_chain == ["base_planner", "child_planner"]
    origins = {b.block_id: b.origin for b in c.blocks}
    assert origins["task"] == "base_planner" and origins["role"] == "child_planner"
    assert origins["copywriting"] == "base_planner + child_planner(append)"
    # 元数据：variables / references 合并，description 等继承，categories / default 不继承
    assert c.variables == ["user_creative_idea"] and c.placeholders == {"user_creative_idea"}
    assert "FAB" in c.reference_texts["references/ecommerce_copywriting_guide.md"]
    assert c.description == "基类描述" and c.target_models == ["claude-opus-5-5"]
    assert c.categories == ["pet_supplement"] and c.default is False
    assert c.render(user_creative_idea="中秋").count("中秋") == 1
    assert "child_planner(append)" in describe_resolution(c)


def test_new_block_defaults_before_first_final(dirs):
    skills, pro = dirs
    write(pro, "c.md", card("c_planner", "# 新增 {#extra}\n\nx\n", extends="base_planner"))
    c = SkillRegistry(skills).get(SLOT, "c_planner")
    assert titles(c) == ["role", "extra", "task", "anti_duplicate", "copywriting", "output_format"]


@pytest.mark.parametrize("body, msg", [
    ("# 改任务 {#task}\n\nx\n", "final"),
    ("# {#output_format remove}\n", "final"),
    ("# {#nope append}\n\nx\n", "没有区块"),
    ("# 新 {#x after=nope}\n\nx\n", "锚点"),
    ("没有区块的正文\n", "必须全部写在"),
    ("# 角色 {#role bogus}\n\nx\n", "无法识别"),
    ("# A {#a}\n\nx\n\n# B {#a}\n\ny\n", "重复"),
])
def test_invalid_child_is_reported(dirs, body, msg):
    skills, pro = dirs
    write(pro, "bad.md", card("bad_planner", body, extends="base_planner"))
    reg = SkillRegistry(skills)
    assert msg in reg.errors[pro / "bad.md"]
    with pytest.raises(KeyError):
        reg.get(SLOT, "bad_planner")
    assert reg.get(SLOT, "base_planner")  # 其他卡带不受影响


def test_ops_without_extends_rejected(dirs):
    skills, pro = dirs
    write(pro, "x.md", card("x_planner", "# {#role append}\n\nx\n"))
    assert "没有 extends" in SkillRegistry(skills).errors[pro / "x.md"]


def test_abstract_layer_hidden_and_must_be_implemented(dirs):
    skills, pro = dirs
    write(pro, "_brand.md", card("brand_base", "# 单品表 {#catalog abstract after=task}\n\n请实现\n\n"
                                               "# 品牌合规 {#brand_rules}\n\n不提病名\n",
                                 extends="base_planner", extra="abstract: true\n"))
    write(pro, "ok.md", card("ok_planner", "# 单品表 {#catalog}\n\n七款\n", extends="brand_base"))
    write(pro, "lazy.md", card("lazy_planner", "# 角色 {#role}\n\nx\n", extends="brand_base"))
    reg = SkillRegistry(skills)
    assert "brand_base" not in {c.skill_id for c in reg.list(SLOT)}
    with pytest.raises(KeyError):
        reg.get(SLOT, "brand_base")
    ok = reg.get(SLOT, "ok_planner")
    assert ok.inheritance_chain == ["base_planner", "brand_base", "ok_planner"]
    assert "七款" in ok.body and "不提病名" in ok.body and "请实现" not in ok.body
    assert "catalog" in reg.errors[pro / "lazy.md"]
    assert reg.resolved("brand_base").abstract


def test_open_source_card_cannot_extend_pro(dirs):
    skills, pro = dirs
    write(pro, "p.md", card("pro_parent", "# 角色 {#role}\n\nx\n"))
    write(skills / SLOT, "o.md", card("open_child", "# 文案 {#copywriting}\n\ny\n", extends="pro_parent"))
    assert "不能继承 Pro" in SkillRegistry(skills).errors[skills / SLOT / "o.md"]


def test_cycle_missing_parent_and_depth(dirs):
    skills, pro = dirs
    write(pro, "a.md", card("a_planner", "# A {#a}\n\nx\n", extends="b_planner"))
    write(pro, "b.md", card("b_planner", "# B {#b}\n\nx\n", extends="a_planner"))
    write(pro, "m.md", card("m_planner", "# M {#m}\n\nx\n", extends="ghost"))
    for i, parent in enumerate(["base_planner", "d1", "d2", "d3"], start=1):
        write(pro, f"d{i}.md", card(f"d{i}", f"# D{i} {{#d{i}}}\n\nx\n", extends=parent))
    reg = SkillRegistry(skills)
    assert "循环继承" in reg.errors[pro / "a.md"] and "循环继承" in reg.errors[pro / "b.md"]
    assert "找不到父卡带" in reg.errors[pro / "m.md"]
    assert reg.get(SLOT, "d3").inheritance_chain == ["base_planner", "d1", "d2", "d3"]
    assert "超过" in reg.errors[pro / "d4.md"]


def test_parent_change_hot_reloads_child_and_errors_clear(dirs):
    skills, pro = dirs
    write(pro, "c.md", card("c_planner", "# 文案 {#copywriting append}\n\n子类\n", extends="base_planner"))
    reg = SkillRegistry(skills)
    assert "基类文案" in reg.get(SLOT, "c_planner").body

    base = skills / SLOT / "base.md"
    base.write_text(BASE.replace("基类文案", "新版文案"), encoding="utf-8")
    future = time.time() + 5
    os.utime(base, (future, future))
    reg.refresh()
    assert "新版文案\n\n子类" in reg.get(SLOT, "c_planner").body

    # 父卡带删掉被 append 的区块 → 子卡带报错；恢复后错误消失
    base.write_text(BASE.replace("# 文案 {#copywriting}\n\n基类文案\n\n", ""), encoding="utf-8")
    os.utime(base, (future + 5, future + 5))
    reg.refresh()
    assert pro / "c.md" in reg.errors
    base.write_text(BASE, encoding="utf-8")
    os.utime(base, (future + 10, future + 10))
    reg.refresh()
    assert reg.errors == {} and reg.get(SLOT, "c_planner")


def test_heading_inside_code_fence_is_not_a_block():
    _pre, blocks = parse_blocks("# 输出 {#out}\n\n```\n# 不是区块 {#fake}\n```\n", "x")
    assert [p.block.block_id for p in blocks] == ["out"] and "{#fake}" in blocks[0].block.content


def test_parse_rejects_two_ops():
    with pytest.raises(InheritanceError, match="只能有一个操作"):
        parse_blocks("# {#a append remove}\n", "x")


def test_builtin_default_planner_is_block_structured():
    """开源默认策划卡带按区块组织，task 与 output_format 为 final，供 Pro 卡带继承。"""
    base = SkillRegistry().get(SLOT, "default_planner")
    flags = {b.block_id: b.final for b in base.blocks}
    assert list(flags) == ["role", "task", "planning_rhythm", "generation_mode", "anti_duplicate",
                           "visual_system", "copywriting", "output_format", "self_check"]
    assert flags["task"] and flags["output_format"] and "{#" not in base.body



def test_changes_list_what_the_child_overrides(dirs):
    skills, pro = dirs
    write(pro, "c.md", card("c_planner", "# 新角色 {#role}\n\nx\n\n# 单品表 {#catalog after=task}\n\ny\n\n"
                                         "# {#anti_duplicate remove}\n\n# {#copywriting append}\n\nz\n",
                            extends="base_planner"))
    reg = SkillRegistry(skills)
    c = reg.get(SLOT, "c_planner")
    changes = reg.changes(c)
    assert changes == {"新增": ["单品表"], "改写": ["新角色"], "追加": ["文案"], "删除": ["防雷同"]}
    assert reg.changes(reg.get(SLOT, "base_planner")) == {}
    text = describe_resolution(c, changes)
    assert "# 继承链：base_planner → c_planner" in text and "# 本卡删除：防雷同" in text


def test_cli_resolve_prints_sources(capsys):
    from app.engine.skill_loader import main

    main(["--resolve", "default_planner"])
    out = capsys.readouterr().out
    assert "# 继承链：default_planner" in out and "task" in out and "[final]" in out and "# 本次任务" in out
