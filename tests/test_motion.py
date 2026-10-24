"""【动效广告】零成本代码动效：方案模型、缓动 / 弹簧、排版、Skill 输出纠正、演示编排、渲染与自检。

全部在本机完成，不联网。
"""

import json
import wave
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.engine.compiler import compile_motion_director
from app.engine.motion import audio
from app.engine.motion import easing as ez
from app.engine.motion.encode import render_motion, render_still
from app.engine.motion.renderer import MotionRenderer
from app.engine.motion.review import find_pops, review, sample_frames
from app.engine.motion.typeset import TextStyle, font, layout_lines
from app.engine.motion_engine import (
    MotionEngine,
    assemble_plan,
    default_scene,
    preflight,
    real_product_info,
    stitch_long_image,
)
from app.engine.project_service import create_project
from app.engine.runtime import EngineRuntime
from app.engine.skill_loader import SkillRegistry
from app.engine.video_cloner import ffmpeg_exe
from app.providers.demo.motion import demo_motion
from app.schemas.motion import MotionAd, MotionAsset, MotionScene
from app.schemas.plan import CandidateImage, ImagePlan, ImageSlot
from app.settings import Settings

TEA = "zhangjiajie_berry_tea"
needs_ffmpeg = pytest.mark.skipif(ffmpeg_exe() is None, reason="需要 ffmpeg（pip install imageio-ffmpeg）")


@pytest.fixture
def rt(tmp_path):
    return EngineRuntime(settings=Settings(workspace_dir=tmp_path / "ws"), force_demo=True, demo_delay=0)


def _png(path: Path, size=(400, 400), color=(180, 140, 90)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    for y in range(0, size[1], 40):  # 加点纹理，自检的帧差才有意义
        img.paste((color[0] // 2, color[1] // 2, color[2] // 2), (0, y, size[0], y + 8))
    img.save(path)
    return path


def _asset(tmp_path, key, size=(400, 400), source="main_image") -> MotionAsset:
    p = _png(tmp_path / f"{key}.png", size)
    return MotionAsset(key=key, path=p, label=key, source=source, width=size[0], height=size[1])


def _ad(tmp_path, aspect="9:16", bpm=100) -> MotionAd:
    ad = MotionAd.sized(aspect, title="测试", bpm=bpm, target_seconds=12)
    ad.assets = [_asset(tmp_path, "A1"), _asset(tmp_path, "A2"), _asset(tmp_path, "A3", (400, 1600), "detail_long")]
    ad.replace_scenes([
        MotionScene(kind="hook", beats=4, headline="饭后油腻？", accent="油腻", transition_out="zoom"),
        MotionScene(kind="hero", beats=5, headline="峰云间莓茶", subline="张家界武陵山", images=["A1"]),
        MotionScene(kind="stat", beats=5, headline="总黄酮", stat_value="39.3%", subline="标准 ≥24%",
                    transition_out="wipe"),
        MotionScene(kind="showcase", beats=6, headline="详情看清楚", images=["A3"], camera="still"),
        MotionScene(kind="cta", beats=6, headline="喜欢就带回家", accent="带回家", stat_value="买2发3",
                    items=["立即下单"], images=["A2"], transition_out="cut"),
    ])
    return ad


# ---------------------------------------------------------------- 方案模型


def test_scene_validation_limits_text_and_requires_assets():
    with pytest.raises(ValueError, match="主标题最多 16 个字"):
        MotionScene(kind="hook", beats=4, headline="一" * 17)
    with pytest.raises(ValueError, match="强调词"):
        MotionScene(kind="hook", beats=4, headline="白霜满披", accent="回甘")
    with pytest.raises(ValueError, match="至少需要 1 个素材"):
        MotionScene(kind="hero", beats=5, headline="商品")
    with pytest.raises(ValueError, match="拍数应在"):
        MotionScene(kind="hook", beats=12, headline="钩子")
    with pytest.raises(ValueError, match="数据冲击需要填写数字"):
        MotionScene(kind="stat", beats=5, headline="总黄酮")
    s = MotionScene(kind="features", beats=6, items="芽头嫩叶、条索完整｜检测报告可查", images="a1, A2，a1")
    assert s.items == ["芽头嫩叶、条索完整", "检测报告可查"] and s.images == ["A1", "A2"]


def test_timeline_lands_every_cut_on_a_beat(tmp_path):
    ad = _ad(tmp_path, bpm=97)
    beat = 60 / 97
    beats = 0
    for sp in ad.timeline():
        assert sp.start == round(beats * beat * ad.fps)
        beats += sp.scene.beats
    assert ad.total_frames == round(beats * beat * ad.fps)
    assert ad.total_seconds == pytest.approx(beats * beat, abs=1 / ad.fps)


def test_update_scene_validates_and_logs(tmp_path):
    ad = _ad(tmp_path)
    sid = ad.scenes[1].scene_id
    ad.update_scene(sid, headline="新标题")
    assert ad.get_scene(sid).headline == "新标题" and ad.edit_log[-1].action == "update_scene"
    with pytest.raises(ValueError, match="素材 A9 不存在"):
        ad.update_scene(sid, images=["A9"])
    assert ad.get_scene(sid).images == ["A1"]  # 不合法的修改不生效
    ad.set_options(bpm=120, aspect_ratio="16:9")
    assert (ad.width, ad.height, ad.bpm) == (1920, 1080, 120)
    with pytest.raises(ValueError, match="节拍速度"):
        ad.set_options(bpm=200)


def test_motion_ad_round_trips_through_project_json(rt, tmp_path):
    p = create_project(rt, "动效", TEA, "taobao")
    p.motion_ad = _ad(tmp_path)
    rt.store.save(p)
    back = rt.store.load(p.project_id).motion_ad
    assert [s.headline for s in back.scenes] == [s.headline for s in p.motion_ad.scenes]
    assert back.asset("A3").shape == "长图"


# ---------------------------------------------------------------- 缓动 / 弹簧 / 排版


def test_easing_curves_and_springs_follow_the_guide():
    for ease in (ez.EASE_OUT, ez.EASE_IN, ez.EASE_IN_OUT, ez.POWER4_IN, ez.POWER4_OUT):
        assert ease(0) == 0 and ease(1) == 1
        vals = [ease(i / 50) for i in range(51)]
        assert all(b >= a - 1e-9 for a, b in pairwise(vals))  # 单调，不回退
    assert ez.EASE_OUT(0.25) > 0.6  # 入场快进慢停
    assert ez.EASE_IN(0.25) < 0.05  # 退场慢起快走
    assert max(ez.LANDING(i / 100) for i in range(101)) > 1.0  # 主角落地有轻微过冲
    for preset in (ez.SPRING_SNAPPY, ez.SPRING_DEFAULT, ez.SPRING_HEAVY):
        assert ez.damping_ratio(*preset) >= 0.72  # 法则：ζ ≥ 0.72 才不廉价
        assert ez.spring_settle_seconds(*preset) < 1.0
    assert ez.damping_ratio(*ez.SPRING_POP) < 0.72  # 唯一的回弹时刻
    assert max(ez.spring(t / 100, *ez.SPRING_POP) for t in range(100)) > 1.05


def test_layout_breaks_at_spaces_and_balances_lines():
    f = font(100)
    assert layout_lines("霜多叶嫩 看得见", f, 520) == ["霜多叶嫩", "看得见"]
    lines = layout_lines("一杯里的讲究一杯里的", f, 600)
    assert len(lines) == 2 and abs(len(lines[0]) - len(lines[1])) <= 1  # 均分，不留孤字
    assert not any(ln.startswith(("，", "。", "！")) for ln in layout_lines("白霜满披，才是好莓茶！", f, 330))
    # 分隔符落在断行处时省略，各行尽量等宽（不是 4 个短语 + 1 个孤行）
    small = font(40, "regular")
    lines = layout_lines("主图 · 详情页 · 带货视频 · 配音 · 动效广告", small, 600)
    assert lines == ["主图 · 详情页 · 带货视频", "配音 · 动效广告"]
    assert not any(ln.endswith("·") or ln.startswith("·") for ln in lines)


def test_kinetic_text_rises_per_character_and_settles(tmp_path):
    from app.engine.motion.typeset import KineticText

    kt = KineticText("白霜满披", TextStyle(size=80), 1000)
    assert kt.char_count == 4 and kt.settle_time(0, 0.05, 0.7) == pytest.approx(0.85)
    early, late = Image.new("RGBA", (600, 200)), Image.new("RGBA", (600, 200))
    kt.draw(early, 0, 0, 0.1, stagger=0.05)
    kt.draw(late, 0, 0, 2.0, stagger=0.05)
    a_early = np.asarray(early)[..., 3].astype(int).sum()
    a_late = np.asarray(late)[..., 3].astype(int).sum()
    assert 0 < a_early < a_late  # 入场中只露出一部分，落定后完整


# ---------------------------------------------------------------- Skill 输出纠正 / 演示编排


def test_assemble_plan_corrects_untrusted_skill_output(rt, tmp_path):
    cat, plat = rt.category(TEA), rt.platform("taobao")
    assets = [_asset(tmp_path, "A1"), _asset(tmp_path, "A2", (400, 1800), "detail_long")]
    data = {"title": "最好的莓茶", "scenes": [
        {"kind": "hero", "beats": 99, "headline": "第一名的莓茶 霜多叶嫩", "images": ["A1", "Z9"],
         "transition_out": "fade"},
        {"kind": "showcase", "beats": 2, "headline": "详情", "images": []},
        {"kind": "unknown", "beats": 4, "headline": "丢掉"},
        {"kind": "features", "beats": 6, "headline": "讲究", "items": ["只有一条"]},
        {"kind": "stat", "beats": 5, "headline": "黄酮", "stat_value": ""},
        {"kind": "hook", "beats": 8, "headline": "饭后油腻？", "accent": "不存在", "transition_out": "wipe"},
        {"kind": "gallery", "beats": 5, "headline": "图集", "images": ["A1"], "transition_out": "wipe"},
    ]}
    ad = assemble_plan(data, assets, cat, plat, aspect_ratio="9:16", target_seconds=15, bpm=100,
                       theme="keynote_dark", skill_id="default_motion_director", idea=None)
    kinds = [s.kind for s in ad.scenes]
    assert kinds[0] == "hook" and kinds[-1] == "cta"  # 钩子排第一，缺收尾行动时自动补上
    assert "unknown" not in kinds and "features" not in kinds and "stat" not in kinds  # 不合法的分镜丢弃
    hook = ad.scenes[0]
    assert hook.beats * 60 / 100 <= 3.0 and hook.accent == ""  # 黄金 3 秒；强调词不在标题里时清空
    hero = next(s for s in ad.scenes if s.kind == "hero")
    assert hero.images == ["A1"] and hero.transition_out == "push"  # 未知素材剔除，叠化改为动势推移
    assert "第一" not in hero.headline and "最好" not in ad.title  # 违禁词去除
    assert hero.beats == 10  # 拍数限制在类型范围内
    showcase = next(s for s in ad.scenes if s.kind == "showcase")
    assert showcase.images == ["A2"]  # 缺素材时自动补长图
    assert showcase.beats * 0.6 >= showcase.reading_seconds  # 拍数至少能读完
    assert sum(s.transition_out == "wipe" for s in ad.scenes) <= 1  # 色块擦除全片最多一次
    assert ad.scenes[-1].transition_out == "cut"


def test_demo_motion_uses_only_real_facts(rt, tmp_path):
    cat, plat = rt.category(TEA), rt.platform("taobao")
    assets = [_asset(tmp_path, f"A{i}").payload() for i in range(1, 5)]
    data = demo_motion(assets, cat, plat, {"price_offer": "买2发3"}, 20, 100)
    kinds = [s["kind"] for s in data["scenes"]]
    assert kinds[0] == "hook" and kinds[-1] == "cta" and "hero" in kinds
    stat = next(s for s in data["scenes"] if s["kind"] == "stat")
    assert stat["stat_value"] == "39.3%"  # 数字取自类目卖点，没有编造
    assert data["scenes"][-1]["stat_value"] == "买2发3"
    text = json.dumps(data, ensure_ascii=False)
    assert not any(w in text for w in cat.forbidden_words)
    ad = assemble_plan(data, [_asset(tmp_path, f"A{i}") for i in range(1, 5)], cat, plat, aspect_ratio="9:16",
                       target_seconds=20, bpm=100, theme="keynote_dark", skill_id="x", idea=None)
    assert 15 <= ad.total_seconds <= 25 and preflight(ad, cat, plat) == []


def test_placeholder_product_info_is_not_treated_as_fact(rt):
    cat = rt.category(TEA)
    p = create_project(rt, "莓茶", TEA, "taobao", {"spec": "127g/袋", "price_offer": "如：买 2 罐送茶具",
                                                   "flavonoid": cat.product_form_fields[2].placeholder})
    assert real_product_info(p, cat) == {"spec": "127g/袋"}


def test_compile_motion_director_renders_all_variables(tmp_path):
    reg = SkillRegistry()
    card = reg.get("motion_director", "default_motion_director")
    rt_ = EngineRuntime(settings=Settings(workspace_dir=tmp_path), force_demo=True)
    text = compile_motion_director(card, aspect_ratio="9:16", target_seconds=20, bpm=100, theme_label="发布会暗场",
                                   assets=[{"key": "A1", "type": "图片"}], category=rt_.category(TEA),
                                   platform=rt_.platform("taobao"), product_info={}, user_motion_idea=None)
    assert "{{" not in text and '"key": "A1"' in text and "# 附属指南" in text and "动势推移" in text
    assert str(tmp_path) not in text  # 素材清单不含本机路径


def test_default_scene_inserts_placeholders_not_fake_numbers(tmp_path):
    ad = _ad(tmp_path)
    s = default_scene("stat", ad)
    assert s.stat_value == "0" and "请填写" in s.headline
    assert default_scene("showcase", ad).images == ["A3"]  # 长图优先


# ---------------------------------------------------------------- 素材收集


async def test_engine_collects_project_assets_and_keeps_keys(rt, tmp_path):
    p = create_project(rt, "峰云间莓茶", TEA, "taobao", {"spec": "127g/袋"})
    slots = []
    for i in range(3):
        img = _png(tmp_path / "gen" / f"main{i}.png")
        slots.append(ImageSlot(title=f"主图{i}", visual_description="x", generation_mode="text_to_image",
                               aspect_ratio="1:1", candidates=[CandidateImage(file_path=img, model="m")]))
    p.main_image_plan = ImagePlan(deliverable_type="main_image", platform_id="taobao", category_id=TEA,
                                  planner_skill_id="x", default_aspect_ratio="1:1", slots=slots)
    detail = [ImageSlot(title=f"第{i}屏", visual_description="x", generation_mode="text_to_image", aspect_ratio="3:4",
                        candidates=[CandidateImage(file_path=_png(tmp_path / "gen" / f"d{i}.png", (300, 400)),
                                                   model="m")]) for i in range(3)]
    p.detail_page_plan = ImagePlan(deliverable_type="detail_page", platform_id="taobao", category_id=TEA,
                                   planner_skill_id="x", default_aspect_ratio="3:4", slots=detail)
    eng = MotionEngine(rt)
    ad, mode = await eng.generate(p, target_seconds=18)
    assert mode == "演示模式"
    sources = [a.source for a in ad.assets]
    assert sources.count("main_image") == 3 and sources.count("detail_page") == 3 and "detail_long" in sources
    long = next(a for a in ad.assets if a.source == "detail_long")
    assert long.height == 3 * 1440 and long.shape == "长图"  # 3 屏按 1080 宽拼接
    assert any(s.kind == "showcase" and s.images == [long.key] for s in ad.scenes)
    p.motion_ad = ad
    up = await eng.add_upload(p, ad, _png(tmp_path / "up.png"))
    keys = {a.path: a.key for a in ad.assets}
    ad2, _ = await eng.generate(p, target_seconds=18)
    assert {a.path: a.key for a in ad2.assets if a.path in keys} == keys  # 重新编排时素材编号不变
    assert up.key in {a.key for a in ad2.assets}


def test_stitch_long_image_caches_by_content(tmp_path):
    paths = [_png(tmp_path / f"s{i}.png", (540, 720)) for i in range(2)]
    a = stitch_long_image(paths, tmp_path)
    assert Image.open(a).size == (1080, 2880) and stitch_long_image(paths, tmp_path) == a
    assert stitch_long_image(paths[:1], tmp_path) is None


# ---------------------------------------------------------------- 渲染 / 自检 / 声音


def test_renderer_frames_and_self_review(tmp_path):
    from app.engine.motion.encode import load_media

    ad = _ad(tmp_path)
    r = MotionRenderer(ad, load_media(ad, tmp_path, ffmpeg_exe()), scale=0.25, grain=False)
    first = r.frame(0)
    assert first.size == (r.W, r.H) and np.asarray(first).max() > 24  # 第 0 帧不是黑屏
    # 剪辑点那一帧新画面已经在动（入场比剪辑点略早），不是空背景
    cut = r.spans[1].start
    bg = np.asarray(r.bg.frame(cut / r.fps).convert("RGB"), float)
    assert np.abs(np.asarray(r.frame(cut), float) - bg).mean() > 1
    rv = review(r)
    titles = {c.title: c.ok for c in rv.checks}
    assert titles["剪辑点全部落在节拍上"] and titles["黄金 3 秒钩子"] and titles["收尾行动停留 ≥ 3 秒"]
    assert titles["文字都在安全区内"] and titles["文字互不遮挡"] and titles["转场种类克制（≤3 种）"]
    assert len(sample_frames(r)) == len(ad.scenes) + 1
    kinds = [e.kind for e in r.sound_events()]
    assert kinds.count("riser") == 1 and kinds.count("impact") == 1  # 全片只有一次起势
    assert kinds.count("whoosh") == 4  # 每个非硬切转场一声呼啸（缩放、推移、擦除、推移）


def test_pop_detection_flags_single_frame_flash():
    thumbs = [np.full((48, 48), 40.0) for _ in range(30)]
    thumbs[15] = np.full((48, 48), 200.0)
    assert find_pops(thumbs, set(), set()) == [15, 16]
    assert find_pops(thumbs, set(), {15, 16}) == []  # 剪辑点上的突变是预期之内的


def test_soundtrack_is_synthesized_locally(tmp_path):
    left, right = audio.compose(6.0, 100, 62, "major", drop=1.8, outro=4.0, music=True,
                                events=[], sfx=False)
    assert len(left) == len(right) == 6 * audio.SR and np.abs(left).max() <= 1.0
    before = np.sqrt(np.mean(left[: int(1.6 * audio.SR)] ** 2))
    after = np.sqrt(np.mean(left[int(2.0 * audio.SR): int(3.6 * audio.SR)] ** 2))
    assert after > before * 1.3  # 起势点之后进鼓点和贝斯，明显更满
    path = audio.write_wav(tmp_path / "a.wav", left, right)
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getframerate()) == (2, audio.SR)


@needs_ffmpeg
def test_render_motion_writes_mp4_contact_sheet_and_report(tmp_path):
    ad = _ad(tmp_path, aspect="1:1")
    res = render_motion(ad, tmp_path / "out", ffmpeg_exe(), scale=0.25, crf=28, stem="t")
    assert res.video_path.exists() and res.contact_sheet_path.exists()
    assert (tmp_path / "out" / "t_review.md").read_text(encoding="utf-8").startswith("### 🔍 自检报告")
    assert not list((tmp_path / "out").glob("frames_*"))  # 临时帧已清理
    titles = {c.title: c for c in res.review.checks}
    assert titles["第 0 帧不是黑屏"].ok and titles["没有画面突跳或单帧闪烁"].ok
    assert titles["整片响度统一"].ok, titles["整片响度统一"].detail
    assert res.seconds == pytest.approx(ad.total_seconds, abs=0.05)


@needs_ffmpeg
def test_render_still_previews_one_scene(tmp_path):
    ad = _ad(tmp_path)
    out = render_still(ad, tmp_path / "stills" / "s.png", 2, ffmpeg_exe(), scale=0.25)
    assert Image.open(out).size == (270, 480)


@needs_ffmpeg
async def test_engine_render_appends_versions(rt, tmp_path):
    p = create_project(rt, "动效", TEA, "taobao")
    p.motion_ad = _ad(tmp_path, aspect="1:1")
    eng = MotionEngine(rt)
    events = [ev async for ev in eng.render(p, p.motion_ad, "preview")]
    assert events[-1].done and events[-1].video_path.exists()
    assert any("渲染进度" in ev.message for ev in events)
    first = p.motion_ad.latest_render
    assert first.quality == "preview" and first.width == 540
    [ev async for ev in eng.render(p, p.motion_ad, "preview")]
    assert len(p.motion_ad.renders) == 2 and first.video_path.exists()  # 每次渲染都新建，不覆盖
    assert rt.store.load(p.project_id).motion_ad.latest_render is not None
