"""剪辑大师一键成片 + 剪映 / 达芬奇草稿导出。"""

import json
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from app.engine.compiler import compile_video_editor
from app.engine.config_loader import load_categories, load_platforms
from app.engine.draft_export import (
    build_srt,
    export_jianying,
    export_resolve,
    fill_scale,
)
from app.engine.edit_engine import (
    ClipSource,
    EditEngine,
    MediaInfo,
    analyze_media,
    assemble_plan,
    parse_media,
    sound_spans_from_silence,
    speech_window,
    stale_shots,
)
from app.engine.edit_render import build_ass, fit_font_size, render_final, safe_name
from app.engine.runtime import EngineRuntime
from app.engine.skill_loader import SkillRegistry
from app.engine.video_cloner import ffmpeg_exe
from app.providers.demo.edit import demo_edit
from app.schemas.edit import (
    EditClip,
    EditPlan,
    build_timeline,
    split_subtitle,
    spoken_text,
    transition_code,
)
from app.schemas.project import Project
from app.schemas.video import VideoScript, shot_from_llm
from app.settings import Settings
from app.ui.edit_panel import apply_edit_rows, edit_to_rows
from app.ui.state import UIError

CAT = load_categories()["pet_supplement"]
PLAT = load_platforms()["taobao"]


def clip(i: int, dur: float = 8.0, src_in: float = 0.0, src_out: float | None = None, **kw) -> EditClip:
    return EditClip(shot_id=f"shot_{i}", shot_index=i, video_id=f"vid_{i}", source_path=Path(f"/x/{i}.mp4"),
                    source_duration=dur, width=768, height=1344, source_in=src_in,
                    source_out=src_out if src_out is not None else dur, **kw)


def plan_of(*clips: EditClip, **kw) -> EditPlan:
    return EditPlan(script_id="script_x", editor_skill_id="default_video_editor", aspect_ratio="9:16",
                    width=1080, height=1920, clips=list(clips), **kw)


# ---------------------------------------------------------------- 台词与字幕


def test_spoken_text_extracts_quoted_line():
    assert spoken_text("西装男主播对着镜头，干脆有力地说：“皮毛营养，看这一盒！”") == "皮毛营养，看这一盒！"
    assert spoken_text("女生说：“这霜”，然后笑着说：“也太厚了吧！”") == "这霜，也太厚了吧！"
    assert spoken_text("女主人低声说：今天也要乖乖吃") == "今天也要乖乖吃"
    assert spoken_text(None) == ""


def test_split_subtitle_respects_line_limit():
    chunks = split_subtitle("辰荣贝康重重没，为皮毛屏障健康提供关键支持，检验合格，左下角直接拍。")
    assert chunks == ["辰荣贝康重重没", "为皮毛屏障健康提供关键支持", "检验合格，左下角直接拍"]
    assert all(len(c) <= 14 for c in split_subtitle("一" * 40))
    assert split_subtitle("") == []


def test_transition_accepts_code_and_chinese():
    assert transition_code("叠化") == "fade" and transition_code("fadewhite") == "fadewhite"
    assert transition_code("溶解") is None


# ---------------------------------------------------------------- 时间轴


def test_timeline_cuts_back_to_back():
    tl = build_timeline([clip(1, 5.18, 1.5, 4.25), clip(2, 8, 0.5, 7.5)])
    assert [p.start for p in tl.clips] == [0, 66]
    assert tl.total == 66 + 168
    assert tl.joins[0].kind == "cut"


def test_transition_uses_handles_and_keeps_total_duration():
    a = clip(1, 8, 1.0, 6.0, transition_out="fade", transition_seconds=0.5)
    b = clip(2, 8, 1.0, 6.0)
    tl = build_timeline([a, b])
    join = tl.joins[0]
    assert join.kind == "fade" and join.duration == 12  # 0.5 秒 × 24 帧
    assert tl.total == 240  # 转场不改变总时长
    assert tl.clips[0].tail == join.after and tl.clips[1].head == join.before


def test_transition_without_handles_falls_back_to_cut():
    a = clip(1, 6, 0.0, 6.0, transition_out="fade")  # 出点就是素材结尾，后一片段入点为 0
    tl = build_timeline([a, clip(2, 6, 0.0, 5.0)])
    assert tl.joins[0].kind == "cut"


def test_clip_validation():
    with pytest.raises(ValueError, match="超过素材时长"):
        clip(1, 5, 0, 6)
    with pytest.raises(ValueError, match="太短"):
        clip(1, 5, 2, 2.2)
    with pytest.raises(ValueError, match="14 个字"):
        clip(1, headline="一二三四五六七八九十一二三四五")


def test_cues_follow_speech_window():
    c = clip(1, 5.18, 1.5, 4.25, subtitle="皮毛营养，看这一盒！", subtitle_start=1.85, subtitle_end=3.85,
             headline="0.4g 小片")
    p = plan_of(c)
    cues = p.cues()
    sub = [x for x in cues if x.kind == "subtitle"]
    head = [x for x in cues if x.kind == "headline"]
    assert len(head) == 1 and head[0].start == 0 and head[0].end == p.timeline().total
    # 台词 1.85 秒开始 -> 成片 (1.85 - 1.5) 秒 = 第 8 帧
    assert sub[0].start == 8 and sub[-1].end == 56
    assert [x.text for x in sub] == ["皮毛营养，看这一盒！"]  # 短句合并为一条字幕


# ---------------------------------------------------------------- 有声段落检测


def test_sound_spans_from_silencedetect():
    err = ("[silencedetect] silence_start: 0\n[silencedetect] silence_end: 1.85 | silence_duration: 1.85\n"
           "[silencedetect] silence_start: 3.85\n[silencedetect] silence_end: 4.21\n"
           "[silencedetect] silence_start: 4.24\n")
    assert sound_spans_from_silence(err, 5.18) == [(1.85, 3.85)]
    assert sound_spans_from_silence("", 4.0) == [(0.0, 4.0)]  # 没有静音 = 全程有声


def test_parse_media():
    err = ("  Duration: 00:00:09.43, start: 0.000000, bitrate: 1740 kb/s\n"
           "  Stream #0:0[0x1](und): Video: h264 (High), yuv420p(progressive), 768x1344, 1603 kb/s, 24 fps\n"
           "  Stream #0:1[0x2](und): Audio: aac (LC), 32000 Hz, stereo\n")
    info = parse_media(err)
    assert (info.duration, info.width, info.height, info.fps, info.has_audio) == (9.43, 768, 1344, 24, True)


def test_speech_window_ignores_continuous_ambience():
    talk = "皮毛营养，看这一盒！"
    assert speech_window(talk, MediaInfo(5.18, sound_spans=[(1.85, 3.85)])) == (1.85, 3.85)
    # 环境音贯穿 8 秒，远长于台词：无法据此定位台词，改按语速估算
    assert speech_window(talk, MediaInfo(8, sound_spans=[(0.0, 8.0)])) is None


# ---------------------------------------------------------------- 剪辑方案纠正


HOOK = {"duration_seconds": 3, "shot_purpose": "hook", "scene_description": "仓库", "first_frame_description": "主播",
        "audio_mode": "dialogue_and_ambient", "character_dialogue": "主播说：“皮毛营养，看这一盒！”",
        "ambient_sound_description": "封箱声"}
SELL = {"duration_seconds": 8, "shot_purpose": "selling_point", "scene_description": "桌面",
        "first_frame_description": "产品", "audio_mode": "ambient_sfx", "ambient_sound_description": "倒片声"}
CTA = {"duration_seconds": 5, "shot_purpose": "call_to_action", "scene_description": "窗台",
       "first_frame_description": "手持", "audio_mode": "native_dialogue", "character_dialogue": "她说：“点下方链接”"}


def make_script(tmp_path: Path, n_videos: int = 3) -> VideoScript:
    script = VideoScript(platform_id="taobao", category_id="pet_supplement",
                         scriptwriter_skill_id="default_scriptwriter", video_type="product_showcase",
                         aspect_ratio="9:16")
    script.replace_all_items([shot_from_llm(d) for d in (HOOK, SELL, CTA)], None)
    script.confirm_plan()
    for i, shot in enumerate(script.shots[:n_videos]):
        frame = tmp_path / f"f{i}.png"
        frame.write_bytes(b"png")
        f = script.add_first_frame(shot.shot_id, frame, model="gpt-image-2")
        script.confirm_first_frame(shot.shot_id, f.frame_id)
        video = tmp_path / f"v{i}.mp4"
        video.write_bytes(b"mp4")
        script.bind_video(shot.shot_id, video, provider="demo")
    return script


def sources_for(script: VideoScript) -> list[ClipSource]:
    infos = [MediaInfo(5.18, 768, 1344, 24, True, True, [(1.85, 3.85)]),
             MediaInfo(8.0, 768, 1344, 24, True, True, [(0.0, 8.0)]),
             MediaInfo(5.2, 768, 1344, 24, True, True, [(0.6, 2.4)])]
    return [ClipSource(i, s, s.playable_videos[-1], infos[i - 1])
            for i, s in enumerate(script.shots, start=1) if s.playable_videos]


def test_assemble_plan_corrects_llm_output(tmp_path):
    script = make_script(tmp_path)
    sources = sources_for(script)
    data = {"title": "最好的治疗片", "clips": [
        {"shot_index": 3, "source_in": 0, "source_out": 1.0, "transition_out": "溶解", "headline": "治疗毛发问题"},
        {"shot_index": 1, "source_in": 2.5, "source_out": 3.0, "transition_out": "fade"},
        {"shot_index": 1, "source_in": 0, "source_out": 2},  # 重复：忽略
        {"shot_index": 9, "source_in": 0, "source_out": 2},  # 不存在：忽略
    ]}
    plan = assemble_plan(data, sources, script, CAT, PLAT, "default_video_editor", None)
    assert [c.shot_index for c in plan.clips] == [1, 3, 2]  # 钩子强制第一，漏掉的镜头追加
    hook, cta, missing = plan.clips
    assert hook.enabled and hook.source_in <= 1.75 and hook.source_out >= 3.85 - 1e-6  # 不剪断台词
    assert cta.source_out >= 2.4 and cta.transition_out == "cut"  # 未知转场 → 硬切；最后一镜硬切
    assert "治疗" not in cta.headline and "治疗" not in plan.title  # 违禁词已清理
    assert not missing.enabled  # Skill 漏掉的镜头保留为停用
    assert hook.subtitle == "皮毛营养，看这一盒！" and hook.subtitle_start == 1.85


def test_demo_edit_matches_skill_schema(tmp_path):
    script = make_script(tmp_path)
    payload = [s.payload() for s in sources_for(script)]
    data = demo_edit(payload, CAT, PLAT, 15, "节奏快一点", {"spec": "0.4g × 30 片/盒"})
    assert {"title", "edit_summary", "clips"} <= data.keys()
    assert [r["shot_index"] for r in data["clips"]] == [1, 2, 3]
    assert data["clips"][-1]["headline"]  # 行动号召镜头有下单引导花字
    plan = assemble_plan(data, sources_for(script), script, CAT, PLAT, "default_video_editor", "节奏快一点")
    assert plan.total_seconds < sum(s.info.duration for s in sources_for(script))  # 掐头去尾后变短


def test_editor_skill_renders_all_variables(tmp_path):
    skill = SkillRegistry().get("video_editor", "default_video_editor")
    script = make_script(tmp_path)
    payload = [s.payload() for s in sources_for(script)]
    text = compile_video_editor(skill, script, payload, CAT, PLAT, {"spec": "30 片"}, None)
    assert "{{" not in text and '"spoken_line": "皮毛营养，看这一盒！"' in text and "sound_spans" in text


def test_stale_shots_detects_new_versions(tmp_path):
    script = make_script(tmp_path, n_videos=2)
    plan = assemble_plan({"clips": []}, sources_for(script), script, CAT, PLAT, "default_video_editor", None)
    assert stale_shots(plan, script) == []
    extra = tmp_path / "v_new.mp4"
    extra.write_bytes(b"mp4")
    script.bind_video(script.shots[1].shot_id, extra, provider="demo")
    assert stale_shots(plan, script) == [2]


# ---------------------------------------------------------------- 表格编辑


def test_table_roundtrip_edit_and_reorder():
    plan = plan_of(clip(1, 5.18, 1.5, 4.25, subtitle="看这一盒"), clip(2, 8, 0.3, 7), clip(3, 8, 0.3, 6))
    rows = edit_to_rows(plan)
    rows[1][0], rows[2][0] = 3, 2  # 调换镜头 2、3
    rows[1][6], rows[1][9] = "叠化", "0.4g 小片"
    rows[2][2] = False
    assert apply_edit_rows(plan, rows) == 3
    assert [c.shot_index for c in plan.clips] == [1, 3, 2]
    assert plan.clips[2].transition_out == "fade" and plan.clips[2].headline == "0.4g 小片"
    assert not plan.clips[1].enabled
    rows = edit_to_rows(plan)
    rows[0][6] = "旋转"
    with pytest.raises(UIError, match="转场"):
        apply_edit_rows(plan, rows)


# ---------------------------------------------------------------- 字幕 / 花字样式


def test_ass_layout_and_font_fit():
    c = clip(1, 5.18, 1.5, 4.25, subtitle="皮毛营养，看这一盒！", headline="0.4g 小片")
    p = plan_of(c)
    ass = build_ass(p, p.cues(), font="Microsoft YaHei")
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Dialogue: 1,0:00:00.00" in ass and ",Head,," in ass and ",Sub,," in ass
    assert fit_font_size("一" * 20, 100, 900) < 100 and fit_font_size("短", 100, 900) == 100
    assert safe_name('a/b:c*?"<>|') == "abc" and safe_name("") == "成片"


def test_srt_format():
    p = plan_of(clip(1, 5.18, 1.5, 4.25, subtitle="皮毛营养，看这一盒！", subtitle_start=1.85, subtitle_end=3.85))
    srt = build_srt([c for c in p.cues() if c.kind == "subtitle"], 24)
    assert srt.startswith("1\n00:00:00,333 --> ") and "皮毛营养" in srt


# ---------------------------------------------------------------- 真实 ffmpeg：成片与导出


def _color_clip(path: Path, color: str, seconds: float, size: str = "432x768", tone: int = 440) -> Path:
    exe = ffmpeg_exe()
    subprocess.run([exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=24:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency={tone}:duration={seconds}",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
                   check=True, timeout=120)
    return path


@pytest.fixture
def media_plan(tmp_path):
    if not ffmpeg_exe():
        pytest.skip("没有可用的 ffmpeg")
    a = _color_clip(tmp_path / "镜头 一.mp4", "red", 3)
    b = _color_clip(tmp_path / "b.mp4", "blue", 3, tone=660)
    c = _color_clip(tmp_path / "c.mp4", "green", 2.5, size="768x768", tone=880)
    bgm = tmp_path / "bgm.m4a"
    subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
                    "-c:a", "aac", str(bgm)], check=True, timeout=60)

    def mk(i, path, dur, src_in, src_out, **kw):
        return EditClip(shot_id=f"shot_{i}", shot_index=i, video_id=f"v{i}", source_path=path, source_duration=dur,
                        width=432 if i < 3 else 768, height=768, source_in=src_in, source_out=src_out, **kw)

    plan = EditPlan(script_id="s", editor_skill_id="default_video_editor", aspect_ratio="9:16", width=540,
                    height=960, title="测试成片", clips=[
                        mk(1, a, 3, 0.5, 2.5, transition_out="fade", transition_seconds=0.4,
                           subtitle="皮毛营养，看这一盒！"),
                        mk(2, b, 3, 0.5, 2.5, headline="0.4g 小片", transition_out="fadewhite",
                           transition_seconds=0.3),
                        mk(3, c, 2.5, 0.3, 2.0, volume=0.8)])
    plan.set_bgm(bgm, 2.0)
    return plan


def test_filter_graph_pins_stereo_after_loudnorm(media_plan):
    """Debian 12 的 ffmpeg 5.1 在 loudnorm 之后协商不出声道布局：输出前显式写立体声。"""
    from app.engine.edit_render import build_filter_graph

    graph = build_filter_graph(media_plan, media_plan.timeline(), with_captions=False, bgm_input=3)
    assert graph.endswith(",aformat=channel_layouts=stereo[aout]")


async def test_render_final_matches_timeline(media_plan, tmp_path):
    events = [ev async for ev in render_final(media_plan, tmp_path / "out")]
    out = events[-1].video_path
    assert out is not None and out.exists() and events[-1].done
    info = await analyze_media(out)
    expected = media_plan.timeline().total / 24
    assert abs(info.duration - expected) < 0.1  # 转场不改变总时长：2 + 2 + 1.7 秒
    assert (info.width, info.height, info.fps, info.has_audio) == (540, 960, 24, True)
    assert (tmp_path / "out" / "captions.ass").exists()


def test_export_jianying_draft(media_plan, tmp_path):
    root = tmp_path / "jianying_root"
    root.mkdir()
    zip_path, installed = export_jianying(media_plan, tmp_path / "exports", root)
    assert installed is not None and installed.parent == root and zip_path.exists()
    content = json.loads((installed / "draft_content.json").read_text(encoding="utf-8"))
    assert (installed / "draft_info.json").read_bytes() == (installed / "draft_content.json").read_bytes()
    assert content["canvas_config"]["width"] == 540 and content["fps"] == 24
    tracks = {t["name"]: t for t in content["tracks"]}
    assert set(tracks) == {"主轨", "背景音乐", "台词字幕", "卖点花字"}
    main = tracks["主轨"]["segments"]
    assert [s["target_timerange"]["start"] for s in main] == [0, 2_000_000, 4_000_000]
    assert main[0]["source_timerange"]["start"] == 500_000
    assert len(content["materials"]["transitions"]) == 2
    assert all(Path(m["path"]).parent == installed / "materials" for m in content["materials"]["videos"])
    assert len(tracks["背景音乐"]["segments"]) == 3  # 2 秒音乐循环铺满 5.7 秒
    meta = json.loads((installed / "draft_meta_info.json").read_text(encoding="utf-8"))
    assert meta["draft_name"] == installed.name and meta["tm_duration"] == content["duration"]
    # 同名草稿不覆盖
    _, again = export_jianying(media_plan, tmp_path / "exports", root)
    assert again != installed and installed.exists()
    with zipfile.ZipFile(zip_path) as zf:
        assert f"{installed.name}/draft_content.json" in zf.namelist()


def test_export_jianying_without_app_only_zips(media_plan, tmp_path):
    zip_path, installed = export_jianying(media_plan, tmp_path / "exports", tmp_path / "not_installed")
    assert installed is None and zip_path.exists()


def test_export_resolve_bundle(media_plan, tmp_path):
    zip_path, bundle = export_resolve(media_plan, tmp_path / "exports")
    fcpxml = next(bundle.glob("*.fcpxml"))
    root = ET.parse(fcpxml).getroot()
    assert root.get("version") == "1.10"
    seq = root.find(".//sequence")
    assert seq.get("duration") == f"{media_plan.timeline().total}/24s" and seq.get("tcStart") == "0s"
    clips = root.findall(".//spine/asset-clip")
    assert [c.get("offset") for c in clips] == ["0s", "2s", "4s"]
    assert clips[0].get("start") == "1/2s"
    assert len(root.findall(".//spine/transition")) == 2
    srcs = [a.find("media-rep").get("src") for a in root.findall(".//asset")]
    assert all(s.startswith("file:///") and "/media/" in s for s in srcs)
    assert any(c.get("audioRole") == "music" for c in root.iter("asset-clip"))
    assert clips[2].find("adjust-volume") is not None  # 原声音量 0.8
    otio = json.loads(next(bundle.glob("*.otio")).read_text(encoding="utf-8"))
    names = [t["name"] for t in otio["tracks"]["children"]]
    assert names == ["V1", "A1 原声", "A2 背景音乐"]
    assert (bundle / "台词字幕.srt").exists() and (bundle / "卖点花字.srt").exists()
    with zipfile.ZipFile(zip_path) as zf:
        assert any(n.endswith("导入说明.txt") for n in zf.namelist())


def test_fill_scale():
    assert fill_scale(768, 1344, 1080, 1920) == pytest.approx(1.0159, abs=1e-3)
    assert fill_scale(768, 768, 1080, 1920) == pytest.approx(16 / 9, abs=1e-3)
    assert fill_scale(0, 0, 1080, 1920) == 1.0


# ---------------------------------------------------------------- 引擎（演示模式 + 真实 ffmpeg 分析）


async def test_engine_generates_plan_from_project_videos(tmp_path):
    if not ffmpeg_exe():
        pytest.skip("没有可用的 ffmpeg")
    rt = EngineRuntime(settings=Settings(workspace_dir=tmp_path / "ws"), force_demo=True, demo_delay=0)
    script = make_script(tmp_path)
    for i, shot in enumerate(script.shots):
        _color_clip(shot.playable_videos[-1].file_path, ["red", "blue", "green"][i], 5.2)
    project = Project(name="剪辑", category_id="pet_supplement", platform_id="taobao", video_script=script)
    plan, mode, sources = await EditEngine(rt).generate(project, script, None, "控制在 15 秒")
    assert mode == "演示模式" and len(plan.clips) == 3 and plan.user_edit_idea == "控制在 15 秒"
    assert all(s.info.has_audio and s.info.sound_spans for s in sources)
    assert plan.clips[0].shot_index == 1 and plan.width == 1080 and plan.height == 1920
