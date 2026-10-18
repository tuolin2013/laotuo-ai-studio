"""克隆参考爆款视频：本地抽帧、原创 / 克隆双路径剧本，以及两条路径共用的后续全链路。"""

import json
import subprocess

import httpx
import pytest
from PIL import Image

from app.engine.compiler import I2VA_INSTRUCTION, compile_clone_scriptwriter, h3_structure_issues
from app.engine.project_service import create_project
from app.engine.prompt_compiler import PromptCompiler
from app.engine.render_engine import RenderEngine
from app.engine.runtime import EngineRuntime
from app.engine.script_engine import CLONE_SKILL_ID, ScriptEngine
from app.engine.video_cloner import (
    VideoCloneError,
    extract_keyframes,
    ffmpeg_exe,
    format_timestamp,
    frame_to_base64,
    parse_probe,
    sample_timestamps,
    segment_bounds,
    validate_video_file,
)
from app.providers.llm import RightAPILLM
from app.providers.video import DemoVideoProvider
from app.schemas import VideoScript
from app.settings import Settings

TEA = "zhangjiajie_berry_tea"
COLORS = ["red", "blue", "green", "yellow"]  # 4 段纯色，每段 2 秒：相邻关键帧之间必然出现明显切镜


@pytest.fixture(scope="module")
def ref_video(tmp_path_factory):
    """用 ffmpeg 合成一段 8 秒、360x640（9:16）的测试视频。"""
    exe = ffmpeg_exe()
    if not exe:
        pytest.skip("没有可用的 ffmpeg")
    out = tmp_path_factory.mktemp("refvid") / "viral.mp4"
    args = [exe, "-y", "-loglevel", "error"]
    for c in COLORS:
        args += ["-f", "lavfi", "-i", f"color=c={c}:s=360x640:r=24:d=2"]
    args += ["-filter_complex", "".join(f"[{i}:v]" for i in range(len(COLORS))) + f"concat=n={len(COLORS)}:v=1:a=0",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)]
    subprocess.run(args, check=True, timeout=120)
    return out


@pytest.fixture
def rt(tmp_path):
    return EngineRuntime(settings=Settings(workspace_dir=tmp_path), demo_delay=0)


async def collect(gen):
    return [e async for e in gen]


# ---------------------------------------------------------------- 抽帧工具

def test_timestamp_helpers():
    assert format_timestamp(1.5) == "00:01.5"
    assert format_timestamp(64.24) == "01:04.2"
    assert format_timestamp(3725) == "1:02:05.0"
    ts = sample_timestamps(10, 4)
    assert ts == [1.25, 3.75, 6.25, 8.75]
    assert len(sample_timestamps(10, 2)) == 4 and len(sample_timestamps(10, 20)) == 8  # 限制在 4~8 张


def test_parse_probe_handles_rotated_phone_video():
    stderr = ("  Duration: 00:00:12.48, start: 0.000000, bitrate: 900 kb/s\n"
              "  Stream #0:0[0x1](und): Video: h264 (High), yuv420p, 1920x1080, 30 fps\n"
              "      Side data:\n        displaymatrix: rotation of -90.00 degrees\n")
    assert parse_probe(stderr) == (12.48, 1080, 1920)
    with pytest.raises(VideoCloneError):
        parse_probe("not a video")


def test_validate_rejects_bad_files(tmp_path):
    txt = tmp_path / "a.txt"
    txt.write_text("x")
    with pytest.raises(VideoCloneError, match="只支持"):
        validate_video_file(txt)
    with pytest.raises(VideoCloneError, match="找不到"):
        validate_video_file(tmp_path / "missing.mp4")


async def test_extract_keyframes_from_real_video(ref_video, tmp_path):
    out_dir = tmp_path / "reference_video_frames" / "refvid_test"
    source = await extract_keyframes(ref_video, out_dir, frame_count=4)
    assert abs(source.duration_seconds - 8) < 0.2
    assert (source.width, source.height, source.aspect_ratio) == (360, 640, "9:16")
    assert [f.timestamp for f in source.frames] == ["00:01.0", "00:03.0", "00:05.0", "00:07.0"]
    assert [f.index for f in source.frames] == [1, 2, 3, 4]
    for f, color in zip(source.frames, COLORS, strict=True):
        assert f.file_path.parent == out_dir and f.file_path.exists()
        with Image.open(f.file_path) as im:
            assert im.size == (360, 640)
            r, g, b = im.convert("RGB").getpixel((180, 320))
            assert {"red": r > 200 and g < 60, "blue": b > 200 and r < 60,
                    "green": g > 90 and r < 60, "yellow": r > 200 and g > 200}[color]
    # 每一帧都是不同纯色：除第 1 帧外都被识别为切镜
    assert source.frames[0].change_score == 0 and all(f.change_score > 0.1 for f in source.frames[1:])
    assert segment_bounds(source) == [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0), (6.0, source.duration_seconds)]
    assert len(frame_to_base64(source.frames[0].file_path)) > 100


# ---------------------------------------------------------------- 双路径剧本

def test_clone_skill_card_renders_timeline(ref_video, rt):
    card = rt.skills.get("video_scriptwriter", CLONE_SKILL_ID)
    assert "Hypit" in card.body and "严禁画外音" in card.body and card.source_url.startswith("https://github.com/hypit")


async def test_original_and_clone_return_compatible_scripts(ref_video, rt, tmp_path):
    engine = ScriptEngine(rt)
    original, mode_a = await engine.generate(
        category_id=TEA, platform_id="taobao", video_type="product_showcase", aspect_ratio="9:16", shot_count=5,
        user_creative_idea="办公室冲泡", mode="original")
    source = await extract_keyframes(ref_video, tmp_path / "frames", frame_count=4)
    clone, mode_b = await engine.generate(
        category_id=TEA, platform_id="taobao", video_type="product_showcase", aspect_ratio="9:16", shot_count=5,
        user_creative_idea="人设换成宝妈", mode="clone", reference_video=source, clone_strategy="replicate")
    assert mode_a == mode_b == "演示模式"
    for script in (original, clone):
        assert isinstance(script, VideoScript) and script.plan_status == "draft"
        assert script.shots[0].shot_purpose == "hook" and script.shots[0].duration_seconds <= 3
        assert script.validate_plan() == []
        dumped = [s.model_dump(mode="json") for s in script.shots]
        assert not any("voiceover" in k or "narration" in k for d in dumped for k in d)
    assert original.script_mode == "original" and original.reference_video is None
    assert clone.script_mode == "clone" and clone.clone_strategy == "replicate"
    assert clone.scriptwriter_skill_id == CLONE_SKILL_ID
    # 分镜数 = 关键帧数，且逐镜与原片时间点对齐
    assert [s.source_timestamp for s in clone.shots] == [f.timestamp for f in source.frames]
    report = clone.clone_analysis.to_markdown()
    assert "原爆款视频拆解报告" in report and "A-Roll" in report and "00:03.0" in report
    assert "人设换成宝妈" in clone.shots[0].scene_description
    assert len(clone.clone_analysis.shot_breakdown) == 4


async def test_clone_requires_frames_and_valid_strategy(rt):
    engine = ScriptEngine(rt)
    with pytest.raises(ValueError, match="上传参考爆款视频"):
        await engine.generate(category_id=TEA, platform_id="taobao", video_type="product_showcase",
                              aspect_ratio="9:16", shot_count=5, user_creative_idea=None, mode="clone")


def test_resolve_skill_id_keeps_cards_in_their_mode(rt):
    engine = ScriptEngine(rt)
    assert engine.resolve_skill_id("clone", "default_scriptwriter") == CLONE_SKILL_ID
    assert engine.resolve_skill_id("original", CLONE_SKILL_ID) == "default_scriptwriter"
    assert engine.resolve_skill_id("original", "default_scriptwriter") == "default_scriptwriter"
    assert engine.resolve_skill_id("clone", None) == CLONE_SKILL_ID


async def test_switching_back_to_original_clears_clone_data(ref_video, rt, tmp_path):
    engine = ScriptEngine(rt)
    source = await extract_keyframes(ref_video, tmp_path / "frames", frame_count=4)
    clone, _ = await engine.generate(category_id=TEA, platform_id="taobao", video_type="product_showcase",
                                     aspect_ratio="9:16", shot_count=4, user_creative_idea=None, mode="clone",
                                     reference_video=source)
    again, _ = await engine.generate(category_id=TEA, platform_id="taobao", video_type="lifestyle_seeding",
                                     aspect_ratio="9:16", shot_count=3, user_creative_idea=None, existing=clone)
    assert again is clone  # 草稿原地替换：同一个可编辑分镜表
    assert again.script_mode == "original" and again.clone_analysis is None and again.reference_video is None
    assert len(again.shots) == 3 and not any(s.source_timestamp for s in again.shots)


@pytest.mark.parametrize("mode", ["original", "clone"])
async def test_both_modes_run_the_same_downstream_pipeline(mode, ref_video, rt, tmp_path):
    """确认剧本 -> 传图 -> 生成并锁定首帧 -> 官方 H3（I2VA + Ref2VA）-> 生成视频。"""
    project = create_project(rt, f"{mode}-视频", TEA, "taobao")
    source = await extract_keyframes(ref_video, tmp_path / "frames", frame_count=4) if mode == "clone" else None
    script, _ = await ScriptEngine(rt).generate(
        category_id=TEA, platform_id="taobao", video_type="product_showcase", aspect_ratio="9:16", shot_count=4,
        user_creative_idea="办公室冲泡", mode=mode, reference_video=source)
    project.video_script = script
    rt.store.save(project)

    checklist = script.confirm_plan()
    for c in checklist:
        ref = tmp_path / f"{c.role}.png"
        Image.new("RGB", (200, 260), (180, 140, 60)).save(ref)
        script.upload_reference(c.role, ref)
    events = await collect(RenderEngine(rt).render_first_frames(project, script, "gpt-image-2", "gpt_image_2"))
    assert sum(1 for e in events if e.image_path) == len(script.shots)

    compiler = PromptCompiler(rt)
    for shot in script.shots:
        script.confirm_first_frame(shot.shot_id, shot.first_frame_candidates[0].frame_id)
        i2va = await compiler.h3_prompt(script, shot, mode="i2va")
        ref2va = await compiler.h3_prompt(script, shot, mode="ref2va", activate=False)
        assert i2va.startswith(I2VA_INSTRUCTION) and h3_structure_issues(i2va, "i2va") == []
        assert "<Picture 1>" in ref2va and h3_structure_issues(ref2va, "ref2va") == []
        assert shot.h3_prompt_mode == "i2va" and set(shot.h3_prompt_variants) == {"i2va", "ref2va"}

    if DemoVideoProvider.ffmpeg_path():
        first = script.shots[0]
        evs = await collect(RenderEngine(rt).render_video(project, script, first.shot_id, "modal_minimax_h3"))
        assert any(e.video_path for e in evs) and first.videos

    # 克隆来源信息随项目落盘，重启后可恢复
    rt.store.save(project)
    loaded = rt.store.load(project.project_id).video_script
    assert loaded.script_mode == mode
    if mode == "clone":
        assert loaded.clone_analysis is not None and len(loaded.reference_video.frames) == 4


# ---------------------------------------------------------------- 真实 API：多模态识图

def _llm_clone_json(n: int) -> dict:
    shots = [{
        "source_timestamp": f"00:0{2 * i + 1}.0", "duration_seconds": 3 if i == 0 else 2,
        "shot_purpose": "hook" if i == 0 else "selling_point", "shot_size": "特写", "camera_movement": "固定机位",
        "scene_description": f"复刻原片第 {i + 1} 段，换成莓茶", "subject_action": "沸水注入",
        "first_frame_description": "玻璃杯居中", "audio_mode": "ambient_sfx", "character_dialogue": None,
        "ambient_sound_description": "沸水注入声", "first_frame_generation_mode": "text_to_image",
        "required_references": [],
    } for i in range(n)]
    shots[1].update(audio_mode="native_dialogue", character_dialogue="女生对镜头说：“这霜好厚！”",
                    ambient_sound_description="")
    return {"clone_analysis": {"hook_analysis": "结果先行", "rhythm_analysis": "2 秒一切",
                               "a_b_roll_ratio": "A 25% / B 75%", "dialogue_anchoring": "说霜就给霜",
                               "shot_breakdown": [{"timestamp": "00:01.0", "roll": "B-Roll"}]},
            "script_summary": "复刻结果先行结构", "shots": shots}


async def test_live_clone_sends_keyframes_as_multimodal_images(ref_video, tmp_path):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/messages"
        body = json.loads(req.content)
        seen["body"] = body
        return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(_llm_clone_json(4))}]})

    settings = Settings(workspace_dir=tmp_path, llm_key="LLM", llm_api_format="anthropic")
    rt = EngineRuntime(settings=settings, demo_delay=0)
    rt.llm_override = RightAPILLM(settings.rightapi_base_url, "LLM", "claude-opus-5-5", "anthropic",
                                  transport=httpx.MockTransport(handler))
    source = await extract_keyframes(ref_video, tmp_path / "frames", frame_count=4)
    script, mode = await ScriptEngine(rt).generate(
        category_id=TEA, platform_id="taobao", video_type="product_showcase", aspect_ratio="9:16", shot_count=4,
        user_creative_idea="保留开场", product_info={"grade": "特级芽尖"}, mode="clone", reference_video=source,
        clone_strategy="adapt")
    assert mode == "真实 LLM · 多模态识图"
    content = seen["body"]["messages"][0]["content"]
    images = [b for b in content if b["type"] == "image"]
    assert len(images) == 4 and all(b["source"]["type"] == "base64" and b["source"]["data"] for b in images)
    text = content[-1]["text"]
    assert content[-1]["type"] == "text" and "00:05.0" in text and "adapt" in text and "特级芽尖" in text
    assert "保留开场" in text and "{{" not in text
    assert script.script_mode == "clone" and script.clone_strategy == "adapt"
    assert script.clone_analysis.hook_analysis == "结果先行"
    assert script.shots[1].character_dialogue and script.shots[0].source_timestamp == "00:01.0"


def test_clone_prompt_lists_every_frame(ref_video, rt, tmp_path):
    import asyncio

    source = asyncio.run(extract_keyframes(ref_video, tmp_path / "frames", frame_count=5))
    prompt = compile_clone_scriptwriter(rt.skills.get("video_scriptwriter", CLONE_SKILL_ID), source, "replicate",
                                        "9:16", 8, rt.category(TEA), rt.platform("taobao"), None, 5)
    assert all(f.timestamp in prompt for f in source.frames) and "第 5 张图" in prompt
    assert "（未填写）" in prompt and "{{" not in prompt
