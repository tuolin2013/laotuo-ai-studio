"""【动效广告】「AI 生图」素材方式：AI 画面描述的校验、编排纠正、演示编排、生图绑定与渲染快照。

真实生图接口用 httpx.MockTransport 模拟，不联网、不消耗额度。
"""

import json
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.engine.compiler import compile_motion_director, supports_asset_modes
from app.engine.motion.encode import render_still
from app.engine.motion_engine import MotionEngine, MotionError, assemble_plan, default_scene
from app.engine.project_service import create_project
from app.engine.render_engine import RenderEngine, motion_reference
from app.engine.runtime import EngineRuntime
from app.engine.skill_loader import SkillRegistry
from app.engine.video_cloner import ffmpeg_exe
from app.providers.demo.motion import demo_motion
from app.providers.image import gpt_image_size
from app.schemas.motion import VISUAL_PLACEHOLDER, MotionAd, MotionAsset, MotionScene, visual_ratio
from app.settings import Settings
from app.ui.motion_tab import SCENE_HEADERS, apply_scene_rows, scenes_to_rows

TEA = "zhangjiajie_berry_tea"
needs_ffmpeg = pytest.mark.skipif(ffmpeg_exe() is None, reason="需要 ffmpeg（pip install imageio-ffmpeg）")


@pytest.fixture
def rt(tmp_path):
    return EngineRuntime(settings=Settings(workspace_dir=tmp_path / "ws"), force_demo=True, demo_delay=0)


def _png(path: Path, size=(400, 400), color=(180, 140, 90)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    for y in range(0, size[1], 40):
        img.paste((color[0] // 2, color[1] // 2, color[2] // 2), (0, y, size[0], y + 8))
    img.save(path)
    return path


def _asset(tmp_path, key, size=(400, 400), source="main_image", kind="image", label="") -> MotionAsset:
    p = _png(tmp_path / f"{key}.png", size)
    return MotionAsset(key=key, kind=kind, path=p, label=label or key, source=source, width=size[0], height=size[1])


def _ad(tmp_path) -> MotionAd:
    """钩子与收尾写了 AI 画面；商品主视觉只靠 AI 画面（没有项目素材）。"""
    ad = MotionAd.sized("9:16", title="测试", bpm=100, target_seconds=12, asset_mode="generate")
    ad.assets = [_asset(tmp_path, "A1"), _asset(tmp_path, "A2", label="参考图 product_package：包装",
                                                source="reference")]
    ad.replace_scenes([
        MotionScene(kind="hook", beats=4, headline="饭后油腻？", visual="清晨茶园薄雾，主体在下方",
                    transition_out="zoom"),
        MotionScene(kind="hero", beats=5, headline="峰云间莓茶", visual="商品罐居中，原木台面", visual_refs=["A2"]),
        MotionScene(kind="features", beats=6, headline="讲究", items=["芽头嫩叶", "白霜满披"], images=["A1"]),
        MotionScene(kind="cta", beats=6, headline="喜欢就带回家", items=["立即下单"], images=["A1"],
                    visual="办公桌上一杯莓茶", visual_refs=["A2"], transition_out="cut"),
    ])
    return ad


def _bind(ad: MotionAd, tmp_path, scene: MotionScene, name: str, visual: str | None = None) -> MotionAsset:
    p = _png(tmp_path / "gen" / f"{name}.png", (300, 400), (90, 160, 120))
    return ad.bind_visual(scene.scene_id, p, visual=visual or scene.visual, model="gpt-image-2", width=300, height=400)


# ---------------------------------------------------------------- 方案模型


def test_visual_field_validation():
    with pytest.raises(ValueError, match="不需要 AI 画面描述"):
        MotionScene(kind="stat", beats=5, headline="总黄酮", stat_value="39.3%", visual="分子模型")
    with pytest.raises(ValueError, match="不需要 AI 画面描述"):
        MotionScene(kind="showcase", beats=8, headline="详情", images=["A1"], visual="长图")
    with pytest.raises(ValueError, match="生图参考图最多 3 张"):
        MotionScene(kind="hook", beats=4, headline="钩子", visual="x", visual_refs="A1,A2,A3,A4")
    with pytest.raises(ValueError, match="AI 画面描述最多 200 个字"):
        MotionScene(kind="hook", beats=4, headline="钩子", visual="字" * 201)
    # 写了 AI 画面的分镜，生成的画面算作第一个素材：商品主视觉可以不填素材，图集至少再填 1 个
    assert MotionScene(kind="hero", beats=5, headline="商品", visual="商品罐").images == []
    with pytest.raises(ValueError, match="至少需要 1 个素材"):
        MotionScene(kind="gallery", beats=5, headline="图集", visual="茶园")
    with pytest.raises(ValueError, match="至少需要 1 个素材"):
        MotionScene(kind="hero", beats=5, headline="商品")  # 不写 AI 画面时规则不变


def test_visual_refs_must_be_existing_still_images(tmp_path):
    ad = _ad(tmp_path)
    ad.assets.append(_asset(tmp_path, "A3", kind="video", source="shot_video"))
    ad.assets.append(_asset(tmp_path, "A4", (400, 1600), source="detail_long"))
    sid = ad.scenes[0].scene_id
    with pytest.raises(ValueError, match="A9 不存在"):
        ad.update_scene(sid, visual_refs=["A9"])
    with pytest.raises(ValueError, match="A3、A4 是视频或长图"):
        ad.update_scene(sid, visual_refs=["A3", "A4"])


def test_render_blocked_until_visuals_generated_then_visual_goes_first(tmp_path):
    ad = _ad(tmp_path)
    hook, hero, _, cta = ad.scenes
    assert [ad.visual_state(s) for s in ad.scenes] == ["pending", "pending", "none", "pending"]
    with pytest.raises(ValueError, match="第 1、2、4 屏写了「AI 画面」但还没生成"):
        ad.require_renderable()
    for s, name in ((hook, "hook"), (hero, "hero"), (cta, "cta")):
        _bind(ad, tmp_path, s, name)
    ad.require_renderable()
    hook, hero, _, cta = ad.scenes
    assert ad.visual_state(hero) == "ready" and hero.images == []  # 表格的「素材」列不被改动
    snap = ad.render_copy()
    assert snap.scenes[1].images == [hero.visual_key]  # 渲染时生成的画面排第一
    assert snap.scenes[3].images == [cta.visual_key, "A1"]
    assert snap.scenes[2].images == ["A1"] and ad.scenes[3].images == ["A1"]  # 快照不改原方案
    assert ad.asset(hero.visual_key).source == "generated" and "AI 画面" in ad.asset(hero.visual_key).label


def test_changed_description_is_stale_and_regeneration_keeps_old_image(tmp_path):
    ad = _ad(tmp_path)
    hero = ad.scenes[1]
    first = _bind(ad, tmp_path, hero, "v1")
    ad.update_scene(hero.scene_id, visual="商品罐放在竹编托盘上")
    hero = ad.scenes[1]
    assert ad.visual_state(hero) == "stale" and ad.visual_label(hero).startswith("🔄")
    assert ad.render_copy().scenes[1].images == [first.key]  # 还没重新生成时先用旧画面
    assert [s.scene_id for s in ad.visual_targets()] == [ad.scenes[0].scene_id, hero.scene_id,
                                                         ad.scenes[3].scene_id]
    second = _bind(ad, tmp_path, hero, "v2")
    assert ad.scenes[1].visual_key == second.key and ad.visual_state(ad.scenes[1]) == "ready"
    assert first.exists and first.key in {a.key for a in ad.assets}  # 旧画面留在素材库，不删除


def test_reuse_identical_description_and_deleted_scene_keeps_image(tmp_path):
    ad = _ad(tmp_path)
    cta = ad.scenes[3]
    ad.remove_scene(cta.scene_id)
    orphan = _bind(ad, tmp_path, cta, "orphan")  # 生成期间分镜被删：画面仍收进素材库
    assert orphan.label.startswith("已删除的分镜") and orphan in ad.assets
    ad.insert_scene(MotionScene(kind="cta", beats=6, headline="带回家", items=["立即下单"], visual=cta.visual))
    assert ad.visual_state(ad.scenes[-1]) == "pending"
    assert ad.reuse_visuals() == 1 and ad.scenes[-1].visual_key == orphan.key  # 描述一字不差：直接沿用


def test_generated_assets_do_not_count_against_project_asset_limit(tmp_path):
    ad = _ad(tmp_path)
    for i in range(38):
        ad.add_asset(tmp_path / "A1.png", label=f"u{i}")
    with pytest.raises(ValueError, match="素材最多 40 个"):
        ad.add_asset(tmp_path / "A1.png")
    assert _bind(ad, tmp_path, ad.scenes[0], "over").key == "A41"


def test_motion_ad_with_visuals_round_trips_through_project_json(rt, tmp_path):
    p = create_project(rt, "动效", TEA, "taobao")
    p.motion_ad = _ad(tmp_path)
    key = _bind(p.motion_ad, tmp_path, p.motion_ad.scenes[1], "hero").key
    rt.store.save(p)
    back = rt.store.load(p.project_id).motion_ad
    assert back.asset_mode == "generate" and back.scenes[1].visual_key == key
    assert back.scenes[1].visual_refs == ["A2"] and back.asset(key).visual == back.scenes[1].visual


# ---------------------------------------------------------------- 编排纠正 / 卡带 / 演示


def test_assemble_plan_keeps_visuals_only_in_generate_mode(rt, tmp_path):
    cat, plat = rt.category(TEA), rt.platform("taobao")
    assets = [_asset(tmp_path, "A1"), _asset(tmp_path, "A2", kind="video", source="shot_video"),
              _asset(tmp_path, "A3", (400, 1800), "detail_long")]
    data = {"scenes": [
        {"kind": "hook", "beats": 4, "headline": "饭后油腻？", "visual": "茶园晨雾", "visual_refs": ["A1", "A2", "A3"]},
        {"kind": "hero", "beats": 6, "headline": "峰云间", "images": [], "visual": "商品罐居中", "visual_refs": "a1"},
        {"kind": "stat", "beats": 5, "headline": "总黄酮", "stat_value": "39.3%", "visual": "分子模型"},
        {"kind": "gallery", "beats": 5, "headline": "图集", "images": ["A1"], "visual": "冲泡"},
        {"kind": "cta", "beats": 6, "headline": "带回家", "items": ["立即下单"], "visual": "办公桌一杯茶"},
    ]}
    kw = dict(aspect_ratio="9:16", target_seconds=15, bpm=100, theme="keynote_dark", skill_id="x", idea=None)
    ad = assemble_plan(data, assets, cat, plat, asset_mode="generate", max_visuals=3, **kw)
    by_kind = {s.kind: s for s in ad.scenes}
    assert by_kind["hook"].visual == "茶园晨雾" and by_kind["hook"].visual_refs == ["A1"]  # 视频、长图不能当生图参考
    assert by_kind["hero"].images == [] and by_kind["hero"].visual_refs == ["A1"]
    assert by_kind["stat"].visual == ""  # 数据冲击不放图
    assert by_kind["gallery"].visual == "冲泡" and by_kind["cta"].visual == ""  # 最多 3 屏
    assert ad.asset_mode == "generate" and ad.max_visuals == 3

    ad = assemble_plan(data, assets, cat, plat, **kw)  # 项目素材方式：AI 画面一律丢弃
    assert ad.asset_mode == "project" and not ad.visual_scenes
    hero = next(s for s in ad.scenes if s.kind == "hero")
    assert hero.images == ["A1"]  # 没有 AI 画面时缺素材的分镜照旧补一个项目素材


def test_director_card_supports_asset_modes(tmp_path):
    reg = SkillRegistry()
    card = reg.get("motion_director", "default_motion_director")
    assert supports_asset_modes(card)
    rt_ = EngineRuntime(settings=Settings(workspace_dir=tmp_path), force_demo=True)
    common = dict(aspect_ratio="9:16", target_seconds=20, bpm=100, theme_label="发布会暗场", assets=[],
                  category=rt_.category(TEA), platform=rt_.platform("taobao"))
    gen = compile_motion_director(card, asset_mode="generate", max_visuals=3, **common)
    assert "素材方式：**AI 生图**（最多 `3` 屏" in gen and "{{" not in gen
    proj = compile_motion_director(card, **common)
    assert "素材方式：**项目素材**（最多 `0` 屏" in proj


def test_demo_director_writes_visuals_from_category_config(rt, tmp_path):
    cat, plat = rt.category(TEA), rt.platform("taobao")
    pack = _asset(tmp_path, "A1", label="参考图 product_package：包装", source="reference").payload()
    data = demo_motion([pack], cat, plat, {}, 20, 100, asset_mode="generate", max_visuals=4)
    visual = {s["kind"]: s for s in data["scenes"] if s["visual"]}
    assert "hook" in visual and "cta" in visual
    assert visual["cta"]["visual_refs"] == ["A1"]  # 商品包装参考图传给生图接口
    text = json.dumps(data, ensure_ascii=False)
    assert not any(w in text for w in cat.forbidden_words)
    assert all("不要出现任何文字" in s["visual"] and len(s["visual"]) <= 200 for s in visual.values())
    assert not any(s["visual"] for s in demo_motion([pack], cat, plat, {}, 20, 100)["scenes"])  # 项目素材方式不写
    assert sum(bool(s["visual"]) for s in demo_motion([pack], cat, plat, {}, 20, 100, asset_mode="generate",
                                                       max_visuals=1)["scenes"]) == 1


async def test_generate_mode_works_without_any_project_images(rt):
    p = create_project(rt, "空项目", TEA, "taobao")
    eng = MotionEngine(rt)
    with pytest.raises(MotionError, match="还没有可用的素材"):
        await eng.generate(p)
    ad, mode = await eng.generate(p, asset_mode="generate", max_visuals=4, target_seconds=15)
    kinds = [s.kind for s in ad.scenes]
    assert mode == "演示模式" and kinds[0] == "hook" and kinds[-1] == "cta" and "hero" in kinds
    assert not ad.assets and ad.visual_targets()  # 全部画面等用户确认后生成
    hero = next(s for s in ad.scenes if s.kind == "hero")
    assert hero.visual and hero.images == []


def test_default_scene_uses_placeholder_visual_when_assets_are_short(tmp_path):
    ad = MotionAd.sized("9:16", asset_mode="generate")
    ad.replace_scenes([MotionScene(kind="hook", beats=4, headline="钩子")])
    s = ad.insert_scene(default_scene("hero", ad))
    assert s.visual == VISUAL_PLACEHOLDER and s.images == []
    assert ad.visual_label(s) == "✏️ 请先填写描述"
    assert ad.visual_targets() == [] and ad.draft_visuals == [2]  # 占位文字不生成
    with pytest.raises(ValueError, match="还是占位文字"):
        ad.visual_targets([s.scene_id])
    with pytest.raises(MotionError, match="素材不够"):
        default_scene("hero", MotionAd.sized("9:16"))  # 项目素材方式照旧要求素材


def test_table_round_trips_visual_columns(tmp_path):
    ad = _ad(tmp_path)
    rows = scenes_to_rows(ad)
    assert all(len(r) == len(SCENE_HEADERS) for r in rows)
    assert rows[1][12] == "商品罐居中，原木台面" and rows[1][13] == "A2" and rows[1][14] == "⏳ 待生成"
    rows[1][12] = "商品罐放在竹编托盘上"
    rows[2][12] = "茶汤特写"  # 卖点清单也可以写 AI 画面
    rows[2][13] = "a1，a2"
    assert apply_scene_rows(ad, rows) == 2
    assert ad.scenes[1].visual == "商品罐放在竹编托盘上" and ad.scenes[2].visual_refs == ["A1", "A2"]
    rows = scenes_to_rows(ad)
    rows[0][12] = ""  # 清空 AI 画面：这一屏回到只用项目素材
    apply_scene_rows(ad, rows)
    assert ad.visual_state(ad.scenes[0]) == "none"
    old = [r[:12] for r in scenes_to_rows(ad)]  # 旧版 12 列表格照样能保存，AI 画面描述保持不变
    old[1][4] = "新标题"
    assert apply_scene_rows(ad, old) == 1
    assert ad.scenes[1].headline == "新标题" and ad.scenes[1].visual == "商品罐放在竹编托盘上"


# ---------------------------------------------------------------- 生图（演示 / 模拟真实接口）


def _project_with_ad(rt, tmp_path):
    p = create_project(rt, "动效生图", TEA, "taobao")
    p.motion_ad = _ad(tmp_path)
    return p


async def test_render_motion_visuals_demo_binds_each_scene(rt, tmp_path):
    p = _project_with_ad(rt, tmp_path)
    ad = p.motion_ad
    ad.scenes[2] = ad.scenes[2].model_copy(update={"visual": VISUAL_PLACEHOLDER})
    eng = RenderEngine(rt)
    events = [ev async for ev in eng.render_motion_visuals(p, ad, "gpt-image-2.5-flare", "gpt_image_2")]
    msgs = "\n".join(e.message for e in events)
    assert "开始生成 3 屏 AI 画面" in msgs and "第 3 屏的「AI 画面」还是占位文字，本轮跳过" in msgs
    assert events[-1].done and "3/3 屏" in events[-1].message
    assert sum(1 for e in events if e.image_path) == 3
    states = [ad.visual_state(s) for s in ad.scenes]
    assert states == ["ready", "ready", "pending", "ready"]
    hero_asset = ad.asset(ad.scenes[1].visual_key)
    assert hero_asset.path.parent.name == "visuals" and hero_asset.model == "gpt-image-2.5-flare"
    w, h = Image.open(hero_asset.path).size
    assert visual_ratio("hero", "9:16") == "3:4" and w * 4 == h * 3  # 按版式图片框选比例出图
    hook_w, hook_h = Image.open(ad.asset(ad.scenes[0].visual_key).path).size
    assert hook_w * 16 == hook_h * 9  # 钩子是全屏背景，与成片同比例
    prompt = (rt.store.asset_dir(p.project_id, "prompts") / f"motion_{ad.scenes[1].scene_id}.txt").read_text("utf-8")
    assert "商品罐居中" in prompt and "不要出现任何文字" in prompt
    assert rt.store.load(p.project_id).motion_ad.scenes[1].visual_key == ad.scenes[1].visual_key  # 已落盘

    again = [ev async for ev in eng.render_motion_visuals(p, ad, "gpt-image-2.5-flare", "gpt_image_2")]
    assert not any(e.image_path for e in again) and "没有需要生成的 AI 画面" in again[-1].message
    redo = [ev async for ev in eng.render_motion_visuals(p, ad, "gpt-image-2.5-flare", "gpt_image_2",
                                                         [ad.scenes[1].scene_id])]
    assert sum(1 for e in redo if e.image_path) == 1 and ad.scenes[1].visual_key != hero_asset.key


def _png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (12, 16), (200, 100, 50)).save(buf, "PNG")
    return buf.getvalue()


async def test_render_motion_visuals_calls_real_image_api_with_references(tmp_path):
    """真实画图通道（LLM 走演示）：有生图参考的屏走 /v1/images/edits 并带上参考图，尺寸按版式比例。"""
    sent = []

    def handler(req: httpx.Request):
        if req.url.host == "cdn.example.com":
            return httpx.Response(200, content=_png_bytes())
        assert req.headers["authorization"] == "Bearer sk-hq"
        if req.method == "POST":
            sent.append((req.url.path, json.loads(req.content)))
            return httpx.Response(200, json={"task_id": f"t{len(sent)}", "status": "processing"})
        return httpx.Response(200, json={"created": 1, "data": [{"url": "https://cdn.example.com/x.png"}]})

    s = Settings(workspace_dir=tmp_path / "ws", image_key="sk-hq", image_poll_interval=0)
    rt = EngineRuntime(settings=s, demo_delay=0)
    real_client = rt.image_client

    def client_with_mock(base_url=None):
        c = real_client(base_url)
        c._transport = httpx.MockTransport(handler)
        return c

    rt.image_client = client_with_mock
    p = _project_with_ad(rt, tmp_path)
    ad = p.motion_ad
    hero = ad.scenes[1]
    events = [ev async for ev in RenderEngine(rt).render_motion_visuals(
        p, ad, "gpt-image-2.5-flare", "gpt_image_2", [hero.scene_id], resolution="2K")]
    assert "消耗画图额度" in events[0].message and events[-1].done
    assert len(sent) == 1
    path, body = sent[0]
    assert path == "/v1/images/edits" and len(body["images"]) == 1  # 生图参考 A2 传给接口
    assert body["size"] == gpt_image_size("3:4", "2K") and body["n"] == 1
    assert ad.asset(ad.scenes[1].visual_key).path.read_bytes() == _png_bytes()


def test_motion_reference_roles():
    ref = MotionAsset(key="A2", path=Path("x.png"), label="参考图 product_package：包装", source="reference")
    assert motion_reference(ref).role == "product_package"
    assert motion_reference(MotionAsset(key="A1", path=Path("y.png"), source="main_image")).role == \
        "product_main_image"


@needs_ffmpeg
def test_render_still_uses_generated_visual(tmp_path):
    ad = _ad(tmp_path)
    for s, name in zip(ad.visual_scenes, ("a", "b", "c"), strict=True):
        _bind(ad, tmp_path, s, name)
    out = render_still(ad.render_copy(), tmp_path / "stills" / "s.png", 1, ffmpeg_exe(), scale=0.25)
    assert Image.open(out).size == (270, 480)
