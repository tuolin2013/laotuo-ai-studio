"""【配音】选项卡：文案分段与写法检查、音色设定、VoxCPM2 后端调用约定、逐段生成、拼接导出、音色库。

全部在演示模式下运行，不调用 Modal。
"""

import importlib.util
import io
import wave
import zipfile
from pathlib import Path

import pytest

from app.engine.config_loader import load_categories, load_platforms
from app.engine.project_service import create_project
from app.engine.runtime import EngineRuntime
from app.engine.video_cloner import ffmpeg_exe
from app.engine.voice_engine import VoiceEngine, VoiceError, parse_script, subtitle_cues, trim_silence
from app.providers.voice import DemoVoiceProvider, ModalVoxCPMProvider, VoiceProviderError, VoiceRequest
from app.providers.voice.demo_voice import render_demo_wav
from app.schemas.video import VideoScript, shot_from_llm
from app.schemas.voice import (
    VoiceStudio,
    compose_control,
    display_text,
    has_phonemes,
    segment_warnings,
    split_leading_note,
    split_script,
    spoken_len,
    tts_text,
    unknown_tags,
)
from app.settings import Settings
from app.storage.voice_library import VoiceLibrary
from app.ui.voice_tab import apply_segment_rows, segments_to_rows

CAT = load_categories()["zhangjiajie_berry_tea"]
PLAT = load_platforms()["taobao"]
SCRIPT = ("(热情)家人们！今天给大家带来张家界莓茶。表面这层白霜，懂茶的人一看就知道。"
          "现在下单只要99元，买2送1，24小时发货。点下方链接，赶紧抢！")
needs_ffmpeg = pytest.mark.skipif(not ffmpeg_exe(), reason="没有可用的 ffmpeg")


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


@pytest.fixture
def rt(tmp_path):
    return EngineRuntime(settings=Settings(workspace_dir=tmp_path / "ws"), force_demo=True, demo_delay=0)


@pytest.fixture
def project(rt):
    return create_project(rt, "配音测试", "zhangjiajie_berry_tea", "taobao", {"price_offer": "买2送1"})


async def drain(gen) -> list:
    return [ev async for ev in gen]


# ---------------------------------------------------------------- 文本规则


def test_polyphone_annotation_and_tags():
    text = "这次重{chong2}新升级[laughing]哈哈"
    assert tts_text(text) == "这次{chong2}新升级[laughing]哈哈"  # 发给模型：字换成拼音
    assert display_text(text) == "这次重新升级哈哈"  # 字幕：保留原字，去掉标签
    assert has_phonemes(text) and not has_phonemes("普通文本 99 元")
    assert unknown_tags("[laughing][Laughter][sigh][Wow]") == ["[Laughter]", "[Wow]"]


def test_split_script_one_sentence_per_segment_and_merges_tiny_ones():
    segs = split_script(SCRIPT + "好的。")
    assert segs[0].startswith("(热情)家人们！今天")  # 「家人们！」太短，与下一句合并
    assert segs[-1].endswith("赶紧抢！好的。")  # 结尾太短的句子并入上一段
    assert all(8 <= spoken_len(s) <= 40 for s in segs)
    # 英文按空格后的句号切，不切断 3.5 这样的数字，合并时补空格
    en = split_script("Still looking for something? Meet our best seller. It is great. "
                      "Only 3.5 dollars today! Tap the link below and try it today!")
    assert len(en) == 2 and "3.5" in en[1] and "something? Meet" in en[0]


def test_split_script_breaks_long_sentence_at_commas():
    long = "，".join(["这一句话特别特别长没有句号"] * 6) + "。"
    segs = split_script(long)
    assert len(segs) > 1 and all(spoken_len(s) <= 40 for s in segs)


def test_leading_note_and_control_composition():
    assert split_leading_note("（语速放慢）今天先讲产地") == ("今天先讲产地", "语速放慢")
    assert split_leading_note("今天（真的）很划算") == ("今天（真的）很划算", "")
    # 方言名 + 音色 + 本段风格，去重、去括号
    assert compose_control("广东话", "（中年男性）", "广东话", "语速稍快") == "广东话，中年男性，语速稍快"


def test_segment_warnings_follow_best_practices():
    tips = " ".join(segment_warnings("（笑）真的吗[Laughter][sigh][laughing]"))
    assert "[Laughter]" in tips and "太多" in tips and "括号" in tips
    assert "太短" in " ".join(segment_warnings("好的"))
    assert segment_warnings("今天给大家带来张家界莓茶，霜越多越嫩。") == []


# ---------------------------------------------------------------- 模型方法


def test_studio_segment_editing_and_validation():
    st = VoiceStudio()
    st.set_script([{"text": "第一段文案内容", "control": "(热情)"}, {"text": "第二段文案内容", "pause_after": 9}][:1])
    assert st.segments[0].control == "热情"  # 括号被去掉
    second = st.add_segment(st.segments[0].segment_id, "第二段文案内容")
    st.move_segment(second.segment_id, -1)
    assert st.segments[0].segment_id == second.segment_id
    with pytest.raises(ValueError):
        st.update_segment(second.segment_id, pause_after=9)  # 段后停顿最多 5 秒
    with pytest.raises(ValueError):
        st.update_segment(second.segment_id, text="[laughing]")  # 只有标签、没有正文
    st.delete_segment(second.segment_id)
    with pytest.raises(ValueError, match="至少保留一段"):
        st.delete_segment(st.segments[0].segment_id)
    with pytest.raises(ValueError):
        st.set_params(seed=1, voice_id="x")  # 不在白名单的字段


def test_table_round_trip_moves_leading_note_into_style():
    st = VoiceStudio()
    st.set_script([{"text": "今天给大家带来张家界莓茶"}, {"text": "点下方链接赶紧抢"}])
    rows = segments_to_rows(st)
    rows[0][1] = "（放慢语速）今天先讲讲张家界莓茶"
    rows[1][3] = 0
    assert apply_segment_rows(st, rows) == 2
    assert st.segments[0].text == "今天先讲讲张家界莓茶" and st.segments[0].control == "放慢语速"
    assert st.segments[1].pause_after == 0
    with pytest.raises(Exception, match="行数"):
        apply_segment_rows(st, rows[:1])


def test_parse_script_cleans_model_output():
    data = {"segments": [
        {"text": "（热情）家人们看过来，今天这款茶真的值得一试", "control": "", "pause_after": 9},
        "第二段只给了字符串也能识别",
        {"text": "，".join(["这一段写得特别特别长需要再切开"] * 6), "control": "(语速稍快)"},
        {"text": "   "},
        {"text": "这是全网最好的莓茶"},
    ]}
    segs, warnings = parse_script(data, CAT, PLAT)
    assert segs[0]["control"] == "热情" and segs[0]["pause_after"] == 1.5
    assert segs[1]["text"] == "第二段只给了字符串也能识别"
    assert len(segs) >= 5 and all(spoken_len(s["text"]) <= 60 for s in segs)
    assert any("最好" in w for w in warnings)
    with pytest.raises(VoiceError):
        parse_script({"segments": []}, CAT, PLAT)


# ---------------------------------------------------------------- 后端调用约定


class FakeRemote:
    def __init__(self, fn):
        self.fn = fn
        self.calls = []

    async def aio(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.fn(*args, **kwargs)


class FakeMethod:
    def __init__(self, fn):
        self.remote = FakeRemote(fn)


class FakeService:
    def __init__(self, fail: bool = False):
        def synth(**kw):
            if fail:
                raise RuntimeError("参考音频无法解码")
            return render_demo_wav(VoiceRequest(text=kw["text"], control=kw.get("control") or ""))

        self.synthesize = FakeMethod(synth)
        self.transcribe = FakeMethod(lambda audio: " 识别出的文字 ")
        self.preview_normalize = FakeMethod(lambda texts: [t.replace("99", "九十九") for t in texts])


async def test_modal_provider_sends_voxcpm_arguments(tmp_path):
    svc = FakeService()
    prov = ModalVoxCPMProvider(service_factory=lambda: svc)
    ref = tmp_path / "ref.wav"
    ref.write_bytes(render_demo_wav(VoiceRequest(text="参考音频")))
    req = VoiceRequest(text="今天只要99元", reference_path=ref, prompt_text="参考音频", normalize=True, seed=7)
    data = await prov.synthesize(req)
    assert data[:4] == b"RIFF"
    _, kw = svc.synthesize.remote.calls[0]
    assert kw["reference_audio"] == ref.read_bytes() and kw["prompt_text"] == "参考音频"
    assert kw["control"] is None and kw["normalize"] is True and kw["seed"] == 7
    assert await prov.transcribe(b"x") == "识别出的文字"
    assert await prov.normalize_preview(["99元"]) == ["九十九元"]
    assert (await prov.warmup()).ready
    with pytest.raises(VoiceProviderError, match="无法解码"):
        await ModalVoxCPMProvider(service_factory=lambda: FakeService(fail=True)).synthesize(VoiceRequest(text="x"))


async def test_runtime_picks_demo_without_modal_login(rt):
    assert isinstance(rt.voice_provider(), DemoVoiceProvider)
    assert "配音 VoxCPM2" in rt.mode_summary()
    assert not (await ModalVoxCPMProvider().warmup()).ready  # 测试环境视为未登录 Modal


def test_demo_voice_same_voice_same_pitch():
    a = render_demo_wav(VoiceRequest(text="今天给大家带来一款好东西", control="年轻女主播"))
    b = render_demo_wav(VoiceRequest(text="今天给大家带来一款好东西", control="年轻女主播"))
    c = render_demo_wav(VoiceRequest(text="今天给大家带来一款好东西", control="中年男性"))
    assert a == b and a != c
    with wave.open(io.BytesIO(a)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 48000)
        assert 2.0 < w.getnframes() / 48000 < 5.0  # 11 个字约 2.5 秒


def test_normalize_text_keeps_clause_boundaries_and_phonemes():
    """服务端按小句规范化：2026-10-23 实测整句规范化会把「买2送1，24小时」读成「买二送一百二十四小时」。"""
    path = Path(__file__).resolve().parents[1] / "modal" / "voxcpm_service.py"
    if not path.is_file():  # modal/ 是用户自有部署脚本，不入 git，新克隆的仓库里没有
        pytest.skip("没有 modal/voxcpm_service.py")
    spec = importlib.util.spec_from_file_location("voxcpm_service_under_test", path)
    svc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(svc)

    class Stub:
        def normalize(self, text):
            return f"<{text}>"

    svc._normalizer = Stub()
    assert svc.normalize_text("买2送1，24小时发货。") == "<买2送1>，<24小时发货>。"
    assert svc.normalize_text("PH值5.5，12:30开播") == "<PH值5.5>，<12:30开播>"
    assert svc.normalize_text("这次{chong2}新[laughing]哈") == "<这次>{chong2}<新>[laughing]<哈>"


# ---------------------------------------------------------------- 引擎：逐段生成与拼接


@needs_ffmpeg
async def test_design_flow_auto_locks_voice_then_mixes(rt, project):
    eng = VoiceEngine(rt)
    assert eng.split_pasted(project, SCRIPT) == 4
    st = project.voice_studio
    assert st.segments[0].control == "热情"
    st.set_source(mode="design", description="年轻女主播，声音明亮甜美")

    # 没锁定就开始生成：先自动生成样音并锁定，之后每段都以它为参考音频
    events = await drain(eng.render(project))
    assert st.source.design_locked and not st.missing_segments()
    assert any("自动锁定" in e.message for e in events)
    take = st.segments[0].active_take
    assert take.hifi and take.voice_label == "已锁定的设计音色" and take.seed == 42

    # 改一段文案：只重新生成这一段，旧版本保留
    st.update_segment(st.segments[1].segment_id, text="表面这层天然白霜，懂茶的人一看就知道。")
    assert st.stale_segments() == [2]
    assert eng.targets(st, "pending", None) == [st.segments[1].segment_id]
    await drain(eng.render(project))
    seg = st.segments[1]
    assert len(seg.playable_takes) == 2 and seg.active_take.seed == 43  # 重做自动换种子
    st.choose_take(seg.segment_id, seg.playable_takes[0].take_id)
    assert st.stale_segments() == [2]  # 改回旧版本：文案与旧版不一致

    events = await drain(eng.mix(project))
    mix = st.latest_mix
    assert events[-1].final and mix.segment_count == 4
    assert {p.suffix for p in mix.files} == {".wav", ".mp3", ".srt", ".zip"}
    assert abs(wav_seconds(mix.wav_path) - mix.duration_seconds) < 0.2
    srt = mix.srt_path.read_text(encoding="utf-8-sig")
    assert "今天给大家带来张家界莓茶" in srt and "[" not in srt and "{" not in srt
    names = zipfile.ZipFile(mix.zip_path).namelist()
    assert "配音文案.txt" in names and sum(n.startswith("分段音频/") for n in names) == 4

    # 再合成一次：新建一份成品，不覆盖
    await drain(eng.mix(project))
    assert len(st.mixes) == 2 and st.mixes[0].wav_path.exists()
    loaded = rt.store.load(project.project_id).voice_studio
    assert [len(s.takes) for s in loaded.segments] == [1, 2, 1, 1] and len(loaded.mixes) == 2


async def test_mix_requires_every_segment(rt, project):
    eng = VoiceEngine(rt)
    eng.split_pasted(project, SCRIPT)
    with pytest.raises(VoiceError, match="还没有配音"):
        await drain(eng.mix(project))


async def test_design_requires_description(rt, project):
    eng = VoiceEngine(rt)
    eng.split_pasted(project, SCRIPT)
    with pytest.raises(VoiceError, match="音色描述"):
        await drain(eng.render(project))


async def test_lock_rejects_preview_of_old_description(rt, project):
    eng = VoiceEngine(rt)
    st = eng.studio(project)
    st.set_source(mode="design", description="年轻女主播")
    await drain(eng.preview(project))
    st.set_source(description="中年男性")
    with pytest.raises(VoiceError, match="改过"):
        eng.lock_preview(project)


@needs_ffmpeg
async def test_clone_reference_transcribe_and_library(rt, project, tmp_path):
    eng = VoiceEngine(rt)
    src = tmp_path / "主播原声.wav"
    src.write_bytes(render_demo_wav(VoiceRequest(text="大家好，欢迎来到直播间，今天给大家带来一款好东西，用过的都说好。")))
    source = await eng.prepare_reference(project, src)
    assert source.mode == "clone" and source.reference_path.exists()
    assert wav_seconds(source.reference_path) == pytest.approx(source.reference_seconds, abs=0.1)
    with wave.open(str(source.reference_path)) as w:
        assert (w.getnchannels(), w.getframerate()) == (1, 16000)

    # 演示模式识别不了真实语音：给出提示文字，用户手动改
    events = await drain(eng.transcribe_reference(project))
    assert events[-1].final and project.voice_studio.source.prompt_text
    project.voice_studio.set_source(prompt_text="大家好，欢迎来到直播间")
    req, label = eng.build_request(project.voice_studio, "今天只要99元", "语速稍快", seed=1)
    assert req.mode_label == "高保真克隆" and req.control == "" and label.startswith("克隆")
    project.voice_studio.set_source(hifi=False, style="热情")
    req, _ = eng.build_request(project.voice_studio, "今天只要99元", "语速稍快", seed=1)
    assert req.mode_label == "可控克隆" and req.control == "热情，语速稍快"

    # 存进音色库，换个项目也能用
    profile = eng.save_to_library(project, "莓茶主播")
    other = create_project(rt, "另一个项目", "pet_supplement", "taobao")
    eng.use_library_voice(other, profile.voice_id)
    req, label = eng.build_request(other.voice_studio, "你好呀，欢迎光临", "", seed=None)
    assert req.reference_path == profile.sample_path and req.prompt_text == "大家好，欢迎来到直播间"
    assert "莓茶主播" in label
    VoiceLibrary(rt.settings.workspace_dir).delete(profile.voice_id)
    with pytest.raises(ValueError, match="找不到"):
        eng.build_request(other.voice_studio, "你好", "", seed=None)


@needs_ffmpeg
async def test_reference_without_voice_is_rejected(rt, project, tmp_path):
    silent = tmp_path / "silent.wav"
    with wave.open(str(silent), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * 3)
    with pytest.raises(VoiceError, match="没有读到人声"):
        await VoiceEngine(rt).prepare_reference(project, silent)


def test_import_video_dialogue_lines(rt, project):
    eng = VoiceEngine(rt)
    with pytest.raises(VoiceError, match="还没有视频分镜剧本"):
        eng.import_video_lines(project)
    hook = {"duration_seconds": 3, "scene_description": "办公室", "first_frame_description": "女生端茶",
            "audio_mode": "native_dialogue", "shot_purpose": "hook",
            "character_dialogue": "女生对着镜头惊讶地说：“这杯茶居然有白霜！”"}
    quiet = {**hook, "audio_mode": "ambient_sfx", "character_dialogue": None, "shot_purpose": "selling_point",
             "ambient_sound_description": "倒水声"}
    script = VideoScript(platform_id="taobao", category_id="zhangjiajie_berry_tea",
                         scriptwriter_skill_id="default_scriptwriter", video_type="product_showcase",
                         aspect_ratio="9:16")
    script.replace_all_items([shot_from_llm(hook), shot_from_llm(quiet)], None)
    project.video_script = script
    assert eng.import_video_lines(project) == 1
    assert project.voice_studio.segments[0].text == "这杯茶居然有白霜！"


def test_trim_and_subtitle_timing():
    from array import array

    samples = array("h", [0] * 4800 + [5000] * 9600 + [0] * 4800)
    trimmed = trim_silence(samples)
    assert 9600 <= len(trimmed) <= 9600 + 2 * 48000 * 0.05
    cues = subtitle_cues(["今天给大家带来张家界莓茶，表面这层白霜，懂茶的人一看就知道"], [(1.0, 5.0)], cjk=True)
    assert cues[0].start == 1000 and cues[-1].end == 5000
    assert all(len(c.text) <= 14 for c in cues)
    en = subtitle_cues(["Meet our best seller, made with carefully selected ingredients today"], [(0, 3)], cjk=False)
    assert all(len(c.text) <= 42 for c in en) and en[-1].end == 3000


def test_compose_control_is_used_for_dialects(rt, project):
    st = VoiceEngine(rt).studio(project)
    st.set_meta(language="cantonese")
    st.set_source(mode="design", description="中年男性，声音浑厚")
    req, _ = VoiceEngine(rt).build_request(st, "伙計，唔該一個A餐", "語速稍快", seed=1, for_preview=True)
    assert req.control.startswith("广东话，中年男性") and req.reference_path is None
    assert display_text(req.text) == "伙計，唔該一個A餐"
