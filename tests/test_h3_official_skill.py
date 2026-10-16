"""官方 MiniMax-H3 h3-prompt-writing 技能接入：卡带加载、references 挂载、I2VA / Ref2VA 双模式编译。"""

import shutil

import pytest

from app.engine.compiler import (
    I2VA_FIELDS,
    I2VA_INSTRUCTION,
    REF2VA_SECTIONS,
    compile_official_h3_prompt,
    h3_structure_issues,
    h3_target_seconds,
)
from app.engine.config_loader import load_categories
from app.engine.prompt_compiler import PromptCompiler
from app.engine.runtime import EngineRuntime
from app.engine.skill_loader import SKILLS_DIR, SkillRegistry
from app.providers.demo.h3_official import demo_official_h3_prompt
from app.settings import Settings
from tests.test_video_script import make_script

SKILL_ID = "official_h3_prompt_writing"
TEA = "zhangjiajie_berry_tea"
DIALOGUE_SHOT = {
    "duration_seconds": 6, "shot_purpose": "selling_point", "shot_size": "中景", "camera_movement": "缓慢推近",
    "audio_mode": "dialogue_and_ambient",
    "character_dialogue": "年轻女生眼睛一亮，惊喜轻快地说：“这霜也太厚了吧！”",
    "ambient_sound_description": "沸水注入玻璃杯的哗哗声与杯壁轻响",
}


def _positions(prompt: str, names) -> list[int]:
    return [prompt.index(f"{n}:") for n in names]


# ---------- 卡带与 references 加载 ----------

def test_official_card_loaded_with_references_and_is_default():
    reg = SkillRegistry()
    assert reg.errors == {}
    card = reg.get("video_prompt", SKILL_ID)
    assert reg.list("video_prompt")[0].skill_id == SKILL_ID  # 下拉框默认选中
    assert set(card.reference_texts) == {"references/base-en.txt", "references/ref-en.txt"}
    assert "integrated_multimodal_description" in card.reference_texts["references/base-en.txt"]
    assert "retention_analysis" in card.reference_texts["references/ref-en.txt"]
    # 官方 SKILL.md 原文被完整收录
    assert "# H3 Prompt Writing" in card.body and "Tips for Better Results" in card.body
    assert all(p.is_file() for p in card.reference_paths)


def test_official_files_also_deployed_for_claude_code():
    root = SKILLS_DIR.parents[1] / ".claude" / "skills" / "h3-prompt-writing"
    for rel in ("SKILL.md", "references/base-en.txt", "references/ref-en.txt"):
        assert (root / rel).is_file(), rel
    for name in ("base-en.txt", "ref-en.txt"):
        slot_copy = SKILLS_DIR / "video_prompt" / "references" / name
        assert (root / "references" / name).read_bytes() == slot_copy.read_bytes()


def test_missing_reference_file_is_reported_not_fatal(tmp_path):
    dst = tmp_path / "skills"
    shutil.copytree(SKILLS_DIR, dst)
    (dst / "video_prompt/references/ref-en.txt").unlink()
    reg = SkillRegistry(dst)
    assert any("ref-en.txt" in e for e in reg.errors.values())
    assert {c.skill_id for c in reg.list("video_prompt")} == {"minimax_h3_prompt"}


def test_reference_outside_references_dir_rejected(tmp_path):
    slot = tmp_path / "video_prompt"
    slot.mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    (slot / "evil.md").write_text("---\nskill_id: evil\nname: e\nreferences: [references/../../secret.txt]\n---\n"
                                  "body\n", encoding="utf-8")
    reg = SkillRegistry(tmp_path)
    assert reg.list("video_prompt") == [] and any("references/" in e for e in reg.errors.values())


def test_reference_edit_triggers_hot_reload(tmp_path):
    dst = tmp_path / "skills"
    shutil.copytree(SKILLS_DIR, dst)
    reg = SkillRegistry(dst)
    ref = dst / "video_prompt/references/base-en.txt"
    import os
    import time
    ref.write_text(ref.read_text(encoding="utf-8") + "\nEXTRA RULE", encoding="utf-8")
    later = time.time() + 5
    os.utime(ref, (later, later))
    assert reg.refresh()
    assert reg.get("video_prompt", SKILL_ID).reference_texts["references/base-en.txt"].endswith("EXTRA RULE")


# ---------- 编译给 LLM 的指令 ----------

@pytest.mark.parametrize(("mode", "guide", "other"), [("i2va", "base-en.txt", "ref-en.txt"),
                                                       ("ref2va", "ref-en.txt", "base-en.txt")])
def test_compiled_instruction_attaches_selected_guide(mode, guide, other):
    script = make_script()
    card = SkillRegistry().get("video_prompt", SKILL_ID)
    text = compile_official_h3_prompt(card, script.shots[1], script.aspect_ratio,
                                      load_categories()[TEA], mode, ["product_package", "tea_soup"])
    assert "{{" not in text and f"`{mode}`" in text and "这霜也太厚了吧" in text
    assert f'path="references/{guide}"' in text and f'path="references/{other}"' not in text
    if mode == "ref2va":
        assert "<Picture 2>: product reference photo, role `product_package`" in text
        assert "<Picture 3>" in text


def test_duration_clamped_to_official_range():
    assert h3_target_seconds(3) == 4.0 and h3_target_seconds(6.5) == 6.5 and h3_target_seconds(20) == 15.0


# ---------- 演示模式输出（与真实 Skill 结构一致） ----------

def test_demo_i2va_three_fields_keep_chinese():
    cat = load_categories()[TEA]
    prompt = demo_official_h3_prompt(DIALOGUE_SHOT, "9:16", cat, "i2va", ["product_package"])["prompt"]
    assert prompt.startswith(I2VA_INSTRUCTION + "\n\n")
    pos = _positions(prompt, I2VA_FIELDS)
    assert pos == sorted(pos) and h3_structure_issues(prompt, "i2va") == []
    assert "<d>[Chinese] 这霜也太厚了吧！</d>" in prompt  # 中文台词原样保留
    assert '"张家界莓茶"' in prompt  # 包装可见中文字保留
    assert "6.00 seconds" in prompt  # 时间轴与分镜时长对齐
    for word in ("cinematic", "beautiful", "voiceover"):
        assert word not in prompt.lower()


def test_demo_ref2va_six_sections_consistent_labels():
    cat = load_categories()[TEA]
    prompt = demo_official_h3_prompt(DIALOGUE_SHOT, "9:16", cat, "ref2va", ["product_package", "tea_soup"])["prompt"]
    pos = _positions(prompt, REF2VA_SECTIONS)
    assert pos == sorted(pos) and h3_structure_issues(prompt, "ref2va") == []
    sections = {}
    for name, start, end in zip(REF2VA_SECTIONS, pos, [*pos[1:], len(prompt)], strict=True):
        sections[name] = prompt[start:end]
    for name in ("subject_definitions", "summary", "retention_analysis", "detailed_description"):
        assert "<Picture 1>" in sections[name] or "<Subject 1>" in sections[name], name
    assert "<Picture 1>" in sections["subject_definitions"] and "<Picture 3>" in sections["subject_definitions"]
    assert "fully_preserved" in sections["retention_analysis"]
    assert sections["summary"].startswith("summary:\n[keyframe completion")
    assert "<d>[Chinese] 这霜也太厚了吧！</d>" in sections["detailed_description"]


def test_structure_checker_flags_bad_output():
    assert "缺少字段 overall_soundscape" in h3_structure_issues(
        I2VA_INSTRUCTION + "\n\nintegrated_multimodal_description: x\n\nnon_diegetic_music: N/A", "i2va")
    assert "第一行不是官方 I2VA 首帧指令" in h3_structure_issues("integrated_multimodal_description: x", "i2va")
    bad = "summary: a\nsubject_definitions: b\nretention_analysis: c\ndetailed_description: <Picture 1>\n" \
          "overall_soundscape: d\nnon_diegetic_music: N/A"
    assert "字段顺序与官方规范不一致" in h3_structure_issues(bad, "ref2va")


# ---------- 编译器端到端（演示模式） ----------

async def test_compiler_generates_both_modes_and_keeps_active_version(tmp_path):
    rt = EngineRuntime(settings=Settings(workspace_dir=tmp_path), demo_delay=0)
    script = make_script()
    script.confirm_plan()
    shot = script.shots[1]
    frame = script.add_first_frame(shot.shot_id, tmp_path / "f.png", model="gpt-image-2")
    script.confirm_first_frame(shot.shot_id, frame.frame_id)
    compiler = PromptCompiler(rt)
    a = await compiler.h3_prompt(script, shot, mode="i2va")
    b = await compiler.h3_prompt(script, shot, mode="ref2va", activate=False)
    s = script.get_shot(shot.shot_id)
    assert s.h3_prompt == a and s.h3_prompt_mode == "i2va"
    assert s.h3_prompt_variants == {"i2va": a, "ref2va": b}
    assert "integrated_multimodal_description:" in a and "subject_definitions:" in b
    # 重新锁定首帧后，两种版本都失效
    script.confirm_first_frame(shot.shot_id, frame.frame_id)
    assert script.get_shot(shot.shot_id).h3_prompt_variants == {}
