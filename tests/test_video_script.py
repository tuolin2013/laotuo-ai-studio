import pytest
from pydantic import ValidationError

from app.engine.compiler import compile_h3_prompt
from app.engine.config_loader import load_categories
from app.engine.skill_loader import SkillRegistry
from app.schemas import PlanStateError, PlanValidationError, StoryboardShot, VideoScript, shot_from_llm

HOOK = {
    "duration_seconds": 3,
    "shot_purpose": "hook",
    "shot_size": "微距",
    "camera_movement": "缓慢推近",
    "scene_description": "玻璃杯中干茶，沸水注入",
    "subject_action": "沸水冲开茶叶，白霜溶开",
    "first_frame_description": "透明玻璃杯居中，杯中干茶，侧逆光",
    "audio_mode": "ambient_sfx",
    "character_dialogue": None,
    "ambient_sound_description": "沸水注入玻璃杯的哗哗声，杯壁轻响",
    "first_frame_generation_mode": "text_to_image",
    "required_references": [],
}
TALK = {
    "duration_seconds": 4,
    "shot_purpose": "selling_point",
    "scene_description": "办公室工位，年轻女生端着杯子",
    "first_frame_description": "女生手持玻璃杯，包装放在桌面右侧",
    "audio_mode": "dialogue_and_ambient",
    "character_dialogue": "年轻女生看着杯子，惊喜轻快地说：“这霜也太厚了吧！”",
    "ambient_sound_description": "办公室轻微键盘声",
    "first_frame_generation_mode": "single_image_edit",
    "required_references": [{"role": "product_package", "description": "罐子正面"}],
}


def make_script(**kw) -> VideoScript:
    script = VideoScript(
        platform_id="wechat_shop",
        category_id="zhangjiajie_berry_tea",
        scriptwriter_skill_id="default_scriptwriter",
        video_type="product_showcase",
        aspect_ratio=kw.pop("aspect_ratio", "9:16"),
        user_creative_idea="办公室冲泡，女生惊喜",
        **kw,
    )
    script.replace_all_items([shot_from_llm(HOOK), shot_from_llm(TALK)], script.user_creative_idea)
    return script


# ---------- 原生声音字段 ----------

def test_native_audio_fields():
    hook, talk = make_script().shots
    assert hook.audio_mode == "ambient_sfx" and hook.character_dialogue is None
    assert "沸水" in hook.ambient_sound_description
    assert talk.audio_mode == "dialogue_and_ambient" and "霜也太厚" in talk.character_dialogue


def test_no_voiceover_field_in_model():
    assert not [f for f in StoryboardShot.model_fields if "voiceover" in f or "narration" in f]
    with pytest.raises(ValidationError, match="画外音"):
        shot_from_llm(HOOK | {"voiceover": "旁白：这是张家界莓茶"})


@pytest.mark.parametrize(
    ("mode", "dialogue", "ambient"),
    [
        ("native_dialogue", None, ""),          # 缺对白
        ("ambient_sfx", None, ""),              # 缺环境音
        ("dialogue_and_ambient", "说话", ""),   # 缺环境音
        ("ambient_sfx", "不该有对白", "水声"),  # 模式不含对白
        ("silent", None, "水声"),               # 静音不应有环境音
    ],
)
def test_audio_mode_consistency(mode, dialogue, ambient):
    with pytest.raises(ValidationError):
        shot_from_llm(HOOK | {"audio_mode": mode, "character_dialogue": dialogue,
                              "ambient_sound_description": ambient})


def test_audio_fields_reach_h3_prompt():
    script = make_script()
    card = SkillRegistry().get("video_prompt", "minimax_h3_prompt")
    text = compile_h3_prompt(card, script.shots[1], script.aspect_ratio, load_categories()["zhangjiajie_berry_tea"])
    assert "这霜也太厚了吧" in text and "办公室轻微键盘声" in text
    assert "9:16" in text and "{{" not in text


# ---------- 剧本确认闸门 ----------

def test_first_shot_must_be_short_hook():
    script = make_script()
    script.move_shot(script.shots[1].shot_id, 0)
    with pytest.raises(PlanValidationError) as exc:
        script.confirm_plan()
    assert any("钩子" in i for i in exc.value.issues)


def test_edit_script_then_confirm_updates_checklist():
    script = make_script()
    assert script.compute_reference_checklist()[0].role == "product_package"
    hook_id = script.shots[0].shot_id
    script.update_shot(
        hook_id,
        first_frame_generation_mode="multi_image_fusion",
        required_references=[{"role": "product_package"}, {"role": "dry_leaf_closeup"}],
    )
    checklist = script.confirm_plan()
    roles = {c.role: c.slot_ids for c in checklist}
    assert roles == {"product_package": [hook_id, script.shots[1].shot_id], "dry_leaf_closeup": [hook_id]}


def test_unconfirmed_script_blocks_first_frame(tmp_path):
    script = make_script()
    with pytest.raises(PlanStateError):
        script.add_first_frame(script.shots[0].shot_id, tmp_path / "f.png", model="gpt-image-2")


# ---------- 首帧比例锁死 & 首帧确认闸门 ----------

def test_first_frame_ratio_locked_to_video_ratio(tmp_path):
    script = make_script(aspect_ratio="3:4")
    script.confirm_plan()
    sid = script.shots[0].shot_id
    frame = script.add_first_frame(sid, tmp_path / "f.png", model="gemini-3-pro-image-preview")
    assert frame.aspect_ratio == "3:4"
    with pytest.raises(ValueError, match="不一致"):
        script.add_first_frame(sid, tmp_path / "g.png", model="gpt-image-2", aspect_ratio="1:1")


def test_unconfirmed_first_frame_blocks_h3_and_video(tmp_path):
    script = make_script()
    script.confirm_plan()
    sid = script.shots[0].shot_id
    with pytest.raises(PlanStateError, match="首帧"):
        script.set_h3_prompt(sid, "[推进] ...")
    with pytest.raises(PlanStateError):
        script.bind_video(sid, tmp_path / "v.mp4", provider="manual_comfyui")

    frame = script.add_first_frame(sid, tmp_path / "f.png", model="gpt-image-2")
    script.confirm_first_frame(sid, frame.frame_id)
    script.set_h3_prompt(sid, "[推进] 沸水注入")
    video = script.bind_video(sid, tmp_path / "v.mp4", provider="manual_comfyui", source="external_upload")
    assert video.source == "external_upload"
    summary = script.combined_h3_prompts()
    assert "【分镜 1｜3 秒】\n[推进] 沸水注入" in summary and "（尚未编译）" in summary


def test_unlock_and_edit_clears_stale_frames(tmp_path):
    script = make_script()
    script.confirm_plan()
    sid = script.shots[0].shot_id
    frame = script.add_first_frame(sid, tmp_path / "f.png", model="gpt-image-2")
    script.confirm_first_frame(sid, frame.frame_id)

    script.unlock_plan()
    script.update_shot(sid, subject_action="改了动作")
    shot = script.get_shot(sid)
    assert shot.first_frame_candidates == [] and shot.confirmed_first_frame_id is None
    with pytest.raises(ValueError):
        script.update_shot(sid, confirmed_first_frame_id="frame_fake")
