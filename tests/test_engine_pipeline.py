"""全链路集成测试（演示模式，零 Key）：想法 -> 草稿 -> 编辑 -> 确认 -> 传图/复用 -> 编译 -> 异步生成。"""

import zipfile

import pytest
from PIL import Image

from app.engine.compiler import I2VA_INSTRUCTION, h3_structure_issues
from app.engine.planner_engine import PlannerEngine
from app.engine.project_service import create_project, export_winners, reuse_references, winners_in_order
from app.engine.prompt_compiler import PromptCompiler, auto_h3_mode
from app.engine.render_engine import RenderEngine
from app.engine.runtime import EngineRuntime
from app.engine.script_engine import ScriptEngine
from app.providers.video import DemoVideoProvider
from app.schemas import PlanStateError
from app.settings import Settings

TEA = "zhangjiajie_berry_tea"
IDEA = "中秋主题，暖金色调，突出白霜"


@pytest.fixture
def rt(tmp_path):
    return EngineRuntime(settings=Settings(workspace_dir=tmp_path), demo_delay=0)


@pytest.fixture
def png(tmp_path):
    def make(name, color):
        p = tmp_path / "uploads" / name
        p.parent.mkdir(exist_ok=True)
        Image.new("RGB", (300, 400), color).save(p)
        return p
    return make


async def collect(gen):
    return [ev async for ev in gen]


async def test_main_image_full_pipeline(rt, png):
    project = create_project(rt, "莓茶中秋", TEA, "taobao", {"grade": "特级芽尖", "frost": "霜厚"})
    plan, mode = await PlannerEngine(rt).generate(
        deliverable_type="main_image", category_id=TEA, platform_id="taobao", aspect_ratio="1:1",
        count=5, user_creative_idea=IDEA, product_info=project.product_info,
    )
    assert mode == "演示模式" and plan.plan_status == "draft" and len(plan.slots) == 5
    # 未指定 Skill 时自动选用类目专属策划卡带
    assert plan.planner_skill_id == "laotuo_zhangjiajie_meicha_planner"
    assert plan.user_creative_idea == IDEA
    assert IDEA in plan.slots[0].visual_description and "特级芽尖" in plan.slots[0].visual_description
    modes = {s.generation_mode for s in plan.slots}
    assert modes == {"text_to_image", "single_image_edit", "multi_image_fusion"}
    project.main_image_plan = plan

    # 手工编辑：第 1 张改成多图融合
    first = plan.slots[0]
    plan.update_slot(first.slot_id, title="中秋霜礼", generation_mode="multi_image_fusion",
                     required_references=[{"role": "product_package"}, {"role": "dry_leaf_closeup"}])
    # 单独重新生成第 2 张
    old_id = plan.slots[1].slot_id
    await PlannerEngine(rt).regenerate_slot(plan, old_id)
    assert plan.slots[1].slot_id == old_id

    checklist = plan.confirm_plan()
    roles = [c.role for c in checklist]
    assert "dry_leaf_closeup" in roles and "product_package" in roles
    for role in roles:
        plan.upload_reference(role, png(f"{role}.png", (120, 30, 30)))
    assert plan.missing_references() == []

    events = await collect(RenderEngine(rt).render_plan(project, plan, "gpt-image-2.5-flare", "gpt_image_2"))
    images = [e.image_path for e in events if e.image_path]
    assert len(images) == 5 and all(p.exists() for p in images)
    assert events[-1].done and not any(e.error for e in events)
    assert all(s.compiled_prompt for s in plan.slots)
    # 多图融合提示词标注图1/图2，且锁定包装与中文字体
    fused = plan.get_slot(first.slot_id).compiled_prompt
    assert "Image 1" in fused and "Image 2" in fused and "Chinese typography" in fused
    zero = next(s for s in plan.slots if not plan.references_for(s.slot_id))
    assert "Image 1" not in zero.compiled_prompt

    # 勾选定稿并导出
    plan.select_candidate(first.slot_id, plan.get_slot(first.slot_id).candidates[0].candidate_id)
    zp = export_winners(rt, project, "main_image_plan")
    with zipfile.ZipFile(zp) as zf:
        assert len([n for n in zf.namelist() if n.endswith(".png")]) == 5

    loaded = rt.store.load(project.project_id)
    assert sum(len(s.candidates) for s in loaded.main_image_plan.slots) == 5


async def test_detail_page_auto_screens_and_cross_module_reuse(rt, png):
    project = create_project(rt, "莓茶详情", TEA, "taobao")
    main, _ = await PlannerEngine(rt).generate(
        deliverable_type="main_image", category_id=TEA, platform_id="taobao", aspect_ratio="1:1", count=3,
        user_creative_idea=None)
    project.main_image_plan = main
    main.confirm_plan()
    for c in main.reference_checklist:
        main.upload_reference(c.role, png(f"main_{c.role}.png", (20, 120, 60)))

    detail, _ = await PlannerEngine(rt).generate(
        deliverable_type="detail_page", category_id=TEA, platform_id="taobao", aspect_ratio="3:4", count=None,
        user_creative_idea=None)
    assert 3 <= len(detail.slots) <= 20
    assert any(s.generation_mode == "text_to_image" for s in detail.slots)
    assert all(s.aspect_ratio == "3:4" for s in detail.slots)
    project.detail_page_plan = detail
    detail.confirm_plan()

    reused = reuse_references(project, "detail_page_plan")
    assert "product_package" in reused
    events = await collect(RenderEngine(rt).render_plan(project, detail, "gemini-3-pro-image-preview",
                                                        "gemini_nano_banana"))
    assert sum(1 for e in events if e.image_path) == len(detail.slots)
    assert len(winners_in_order(project, "detail_page_plan")) == len(detail.slots)
    # Gemini Skill 生成中文叙述式提示词
    assert "比例" in detail.slots[0].compiled_prompt


async def test_manual_detail_screens_count(rt):
    detail, _ = await PlannerEngine(rt).generate(
        deliverable_type="detail_page", category_id="pet_supplement", platform_id="pinduoduo",
        aspect_ratio="9:16", count=4, user_creative_idea="猫咪挑食")
    assert len(detail.slots) == 4


async def test_video_full_pipeline(rt, png):
    project = create_project(rt, "宠物视频", "pet_supplement", "wechat_shop")
    script, mode = await ScriptEngine(rt).generate(
        category_id="pet_supplement", platform_id="wechat_shop", video_type="lifestyle_seeding",
        aspect_ratio="9:16", shot_count=5, user_creative_idea="猫咪听到撕袋声跑过来")
    project.video_script = script
    assert mode == "演示模式" and len(script.shots) == 5 and script.plan_status == "draft"
    hook = script.shots[0]
    assert hook.shot_purpose == "hook" and hook.duration_seconds <= 3
    assert any(s.character_dialogue for s in script.shots)
    assert any(s.audio_mode == "ambient_sfx" and s.ambient_sound_description for s in script.shots)

    # 用户修改台词与环境音
    talk = next(s for s in script.shots if s.character_dialogue)
    script.update_shot(talk.shot_id, character_dialogue="铲屎官笑着说：“它自己就跑过来了！”")
    other = next(s for s in script.shots[1:] if s.shot_id != talk.shot_id)
    await ScriptEngine(rt).regenerate_shot(script, other.shot_id)
    assert "它自己就跑过来了" in script.get_shot(talk.shot_id).character_dialogue

    # 未确认剧本：拦截首帧
    with pytest.raises(PlanStateError):
        await collect(RenderEngine(rt).render_first_frames(project, script, "gpt-image-2", "gpt_image_2"))

    script.confirm_plan()
    for c in script.reference_checklist:
        script.upload_reference(c.role, png(f"v_{c.role}.png", (200, 160, 60)))
    events = await collect(RenderEngine(rt).render_first_frames(project, script, "gpt-image-2", "gpt_image_2"))
    assert sum(1 for e in events if e.image_path) == 5
    for shot in script.shots:
        frame = shot.first_frame_candidates[0]
        assert frame.aspect_ratio == "9:16"
        with Image.open(frame.file_path) as im:
            assert abs(im.width / im.height - 9 / 16) < 0.01

    compiler = PromptCompiler(rt)
    first = script.shots[0]
    # 首帧未锁定：拦截 H3
    with pytest.raises(PlanStateError):
        await compiler.h3_prompt(script, first)
    # 最后一个分镜移除全部参考图，覆盖「只用首帧」的 I2VA 写法
    last = script.shots[-1]
    for a in script.references_for(last.shot_id):
        script.remove_slot_reference(last.shot_id, a.asset_id)
    for shot in script.shots:
        script.confirm_first_frame(shot.shot_id, shot.first_frame_candidates[0].frame_id)
        # 旧版自定义卡带仍按原格式编译
        legacy = await compiler.h3_prompt(script, shot, "minimax_h3_prompt")
        assert legacy.startswith("[") and "9:16" in legacy and "画外音" not in legacy
        assert script.get_shot(shot.shot_id).h3_official_mode is None
        # 默认官方卡带：自动选择写法（绑定了商品参考图 → Ref2VA，否则 → I2VA）
        prompt = await compiler.h3_prompt(script, shot)
        mode = auto_h3_mode(script, shot)
        assert mode == ("ref2va" if script.references_for(shot.shot_id) else "i2va")
        assert h3_structure_issues(prompt, mode) == []
        assert prompt.startswith(I2VA_INSTRUCTION) == (mode == "i2va")
        assert script.get_shot(shot.shot_id).h3_official_mode == mode
    assert {auto_h3_mode(script, s) for s in script.shots} == {"i2va", "ref2va"}  # 两种写法都覆盖到
    talk_prompt = script.get_shot(talk.shot_id).h3_prompt
    assert "<d>[Chinese] 它自己就跑过来了！</d>" in talk_prompt

    if DemoVideoProvider.ffmpeg_path():
        evs = await collect(RenderEngine(rt).render_video(project, script, first.shot_id))
        video = next(e.video_path for e in evs if e.video_path)
        assert video.exists() and script.get_shot(first.shot_id).videos


async def test_video_auto_prompt_and_versions_never_overwrite(rt, png):
    """生成视频时自动编写 H3 提示词；同一分镜重复生成追加新版本，不同分镜的视频互不覆盖。"""
    if not DemoVideoProvider.ffmpeg_path():
        pytest.skip("没有可用的 ffmpeg")
    project = create_project(rt, "多版本视频", TEA, "taobao")
    script, _ = await ScriptEngine(rt).generate(
        category_id=TEA, platform_id="taobao", video_type="product_showcase", aspect_ratio="9:16", shot_count=3,
        user_creative_idea="冲泡特写")
    project.video_script = script
    script.confirm_plan()
    await collect(RenderEngine(rt).render_first_frames(project, script, "gpt-image-2", "gpt_image_2"))
    for shot in script.shots:
        script.confirm_first_frame(shot.shot_id, shot.first_frame_candidates[0].frame_id)
        assert shot.h3_prompt is None

    a, b = script.shots[0].shot_id, script.shots[1].shot_id
    evs = await collect(RenderEngine(rt).render_video(project, script, a))
    assert any("自动编写 H3" in e.message for e in evs)
    prompt_file = rt.store.asset_dir(project.project_id, "prompts") / f"h3_{a}.txt"
    assert script.get_shot(a).h3_prompt
    assert prompt_file.read_text(encoding="utf-8") == script.get_shot(a).h3_prompt
    await collect(RenderEngine(rt).render_video(project, script, b))
    evs = await collect(RenderEngine(rt).render_video(project, script, a))
    assert any("第 2 版" in e.message for e in evs)

    va, vb = script.get_shot(a).playable_videos, script.get_shot(b).playable_videos
    assert len(va) == 2 and len(vb) == 1
    paths = {v.file_path for v in [*va, *vb]}
    assert len(paths) == 3 and all(p.exists() for p in paths)  # 三个文件各自独立，没有被覆盖
    assert va[0].file_path.parent != vb[0].file_path.parent  # 每个分镜单独一个目录
    # 重新加载项目后版本仍在
    loaded = rt.store.load(project.project_id).video_script
    assert [len(s.videos) for s in loaded.shots] == [2, 1, 0]


async def test_force_demo_overrides_keys(tmp_path, monkeypatch):
    import app.providers.video.modal_minimax_h3 as h3
    import app.settings as settings_mod
    # 只模拟「已登录 Modal」，不会真正调用（本测试不生成视频）
    monkeypatch.setattr(settings_mod, "_modal_logged_in", lambda: True)
    monkeypatch.setattr(h3, "modal_logged_in", lambda: True)
    rt = EngineRuntime(settings=Settings(workspace_dir=tmp_path, llm_key="x", image_key="y"), demo_delay=0)
    assert rt.is_live("llm") and rt.is_live("image") and rt.is_live("modal_h3")
    assert rt.video_provider("modal_minimax_h3").provider_id == "modal_minimax_h3"
    rt.force_demo = True
    assert not rt.is_live("llm") and "演示模式" in rt.mode_summary()
    assert rt.video_provider("modal_minimax_h3").provider_id == "demo"
