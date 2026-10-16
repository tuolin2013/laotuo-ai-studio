"""全链路集成测试（演示模式，零 Key）：想法 -> 草稿 -> 编辑 -> 确认 -> 传图/复用 -> 编译 -> 异步生成。"""

import zipfile

import pytest
from PIL import Image

from app.engine.planner_engine import PlannerEngine
from app.engine.project_service import create_project, export_winners, reuse_references, winners_in_order
from app.engine.prompt_compiler import PromptCompiler, global_continuous_prompt
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
        category_id="pet_supplement", platform_id="wechat_shop", video_type="immersive_demo",
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
    for shot in script.shots:
        script.confirm_first_frame(shot.shot_id, shot.first_frame_candidates[0].frame_id)
        prompt = await compiler.h3_prompt(script, shot)
        assert prompt.startswith("[") and "9:16" in prompt and "画外音" not in prompt
    talk_prompt = script.get_shot(talk.shot_id).h3_prompt
    assert "它自己就跑过来了" in talk_prompt and "口型" in talk_prompt

    total = global_continuous_prompt(script)
    assert "第 1 分镜锁定首帧" in total and "镜头 5" in total and "不使用画外音" in total

    if DemoVideoProvider.ffmpeg_path():
        evs = await collect(RenderEngine(rt).render_video(project, script, first.shot_id, "modal_minimax_h3"))
        video = next(e.video_path for e in evs if e.video_path)
        assert video.exists() and script.get_shot(first.shot_id).videos
    # ComfyUI 手动桥接：给出操作说明
    evs = await collect(RenderEngine(rt).render_video(project, script, first.shot_id, "manual_comfyui"))
    assert any("ComfyUI" in e.message for e in evs)


async def test_force_demo_overrides_keys(tmp_path):
    rt = EngineRuntime(settings=Settings(workspace_dir=tmp_path, llm_key="x", openai_image_key="y",
                                         modal_h3_url="https://m.run"), demo_delay=0)
    assert rt.is_live("llm") and rt.is_live("modal_h3")
    rt.force_demo = True
    assert not rt.is_live("llm") and "演示模式" in rt.mode_summary()
    assert rt.video_provider("modal_minimax_h3").provider_id == "demo"
