"""公共免 Key 在线演示（LAOTUO_PUBLIC_DEMO=1）：锁定演示模式、样板项目、访客独立项目、30 秒上限、渲染排队与过期清理。"""

import os
import shutil
import socket
import time
from pathlib import Path

import pytest
from gradio_client import Client

from app.engine.motion_engine import MotionEngine
from app.engine.public_demo import TEMPLATE_ID, PublicDemo, RenderGate, fit_motion
from app.engine.runtime import EngineRuntime
from app.engine.skill_loader import SKILLS_DIR, SkillRegistry
from app.schemas.motion import BEAT_RANGE, MotionAd, MotionScene
from app.settings import PUBLIC_MOTION_MAX_SECONDS, PUBLIC_WORKSPACE_SUBDIR, Settings, load_settings

FAKE_KEY = "sk-" + "Demo7" * 8


@pytest.fixture
def public_rt(tmp_path):
    ws = tmp_path / "workspace" / PUBLIC_WORKSPACE_SUBDIR
    return EngineRuntime(settings=Settings(workspace_dir=ws, demo_mode=True, public_demo=True),
                         skills=SkillRegistry(SKILLS_DIR, pro_dir=tmp_path / "no_pro"), demo_delay=0, force_demo=True)


@pytest.fixture
def public(public_rt):
    pub = PublicDemo(public_rt)
    pub.prepare()
    return pub


# ---------------------------------------------------------------- 配置锁


def test_public_flag_locks_demo_mode_and_drops_keys(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    for key in ("RIGHTAPI_LLM_KEY", "RIGHTAPI_IMAGE_KEY", "RIGHTAPI_IMAGE_STD_KEY", "FAL_KEY"):
        monkeypatch.setenv(key, FAKE_KEY)
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("LAOTUO_PUBLIC_DEMO", "1")
    s = load_settings(env)
    assert s.public_demo and s.demo_mode
    assert not (s.llm_key or s.image_key or s.image_std_key or s.fal_key)
    assert not any(s.channel_live(ch) for ch in ("llm", "image", "image_std", "modal_h3", "modal_voxcpm", "fal_ai"))
    # 公共演示的工作区与本机真实项目分开
    assert s.workspace_dir == tmp_path / "ws" / PUBLIC_WORKSPACE_SUBDIR

    monkeypatch.setenv("LAOTUO_PUBLIC_DEMO", "0")
    s = load_settings(env)
    assert not s.public_demo and s.llm_key == FAKE_KEY and s.workspace_dir == tmp_path / "ws"


def test_public_demo_refuses_a_normal_workspace(tmp_path):
    rt = EngineRuntime(settings=Settings(workspace_dir=tmp_path / "workspace"), demo_delay=0, force_demo=True)
    with pytest.raises(ValueError, match=PUBLIC_WORKSPACE_SUBDIR):
        PublicDemo(rt)  # 过期清理会删目录：只允许在 public_demo/ 里进行


# ---------------------------------------------------------------- 样板项目与访客项目


def test_template_has_sample_images_and_motion_assets(public, public_rt):
    tpl = public_rt.store.load(TEMPLATE_ID)
    for plan in (tpl.main_image_plan, tpl.detail_page_plan):
        assert plan.plan_status == "confirmed" and len(plan.slots) == 4
        assert all(s.candidates and s.candidates[-1].file_path.exists() for s in plan.slots)
    assets = tpl.motion_ad.assets
    assert len(assets) >= 8 and all(a.exists for a in assets)
    assert {a.source for a in assets} >= {"main_image", "detail_page", "detail_long"}
    # 只写外观、产地与规格，不编造检测数据与价格
    assert set(tpl.product_info) <= {"grade", "frost", "origin", "harvest_season", "spec"}


def test_each_visitor_gets_an_independent_copy(public, public_rt):
    a, b = public.new_visitor_project(), public.new_visitor_project()
    assert a.project_id != b.project_id and TEMPLATE_ID not in (a.project_id, b.project_id)
    for p in (a, b):
        d = public_rt.store.project_dir(p.project_id)
        assert TEMPLATE_ID not in (d / "project.json").read_text(encoding="utf-8")  # 路径全部换成自己的目录
        files = [c.file_path for s in p.main_image_plan.slots for c in s.candidates] + \
                [x.path for x in p.motion_ad.assets]
        assert files and all(f.exists() and f.is_relative_to(d) for f in files)
    # 一位访客删掉自己的素材，不影响另一位和样板
    shutil.rmtree(public_rt.store.project_dir(a.project_id) / "candidates")
    assert all(c.file_path.exists() for s in b.main_image_plan.slots for c in s.candidates)
    assert public_rt.store.load(TEMPLATE_ID).main_image_plan.slots[0].candidates[0].file_path.exists()


def test_template_is_rebuilt_when_missing(public, public_rt):
    shutil.rmtree(public.template_dir)
    public._ready = False
    p = public.new_visitor_project()
    assert (public.template_dir / "project.json").exists() and p.main_image_plan is not None


# ---------------------------------------------------------------- 过期清理


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_cleanup_removes_stale_projects_and_old_renders(public, public_rt):
    forgotten = []
    public._forget = forgotten.append
    old, fresh = public.new_visitor_project(), public.new_visitor_project()
    old_dir, fresh_dir = (public_rt.store.project_dir(p.project_id) for p in (old, fresh))
    _age(old_dir / "project.json", 2 * 3600)
    stale_render = fresh_dir / "motion" / "render_0101_000000_preview"
    new_render = fresh_dir / "motion" / "render_0101_000001_preview"
    for d in (stale_render, new_render):
        d.mkdir(parents=True)
        (d / "a.mp4").write_bytes(b"0")
    _age(stale_render, 2 * 3600)
    stills = fresh_dir / "motion" / "stills"
    stills.mkdir(parents=True, exist_ok=True)
    (stills / "old.png").write_bytes(b"0")
    _age(stills / "old.png", 2 * 3600)
    _age(public.template_dir / "project.json", 30 * 24 * 3600)

    assert public.cleanup() == 1
    assert not old_dir.exists() and forgotten == [old.project_id]
    assert fresh_dir.exists() and new_render.exists() and not stale_render.exists()
    assert not (stills / "old.png").exists()
    assert public.template_dir.exists()  # 样板项目不删


# ---------------------------------------------------------------- 30 秒上限与渲染排队


async def test_fit_motion_caps_every_plan_at_30_seconds(public, public_rt):
    p = public.new_visitor_project()
    eng = MotionEngine(public_rt)
    for target, bpm in ((30, 100), (30, 128), (30, 72), (20, 72)):
        ad, _ = await eng.generate(p, aspect_ratio="9:16", target_seconds=target, bpm=bpm)
        hook, cta = ad.scenes[0], ad.scenes[-1]
        note = fit_motion(ad)
        assert ad.total_seconds <= PUBLIC_MOTION_MAX_SECONDS
        assert ad.scenes[0] is hook and ad.scenes[-1] is cta and len(ad.scenes) >= 3
        assert all(BEAT_RANGE[s.kind][0] <= s.beats <= BEAT_RANGE[s.kind][1] for s in ad.scenes)
        assert all(s.reading_seconds < s.beats * ad.beat_seconds for s in ad.scenes)  # 还能读完
        assert ("30 秒" in note) == bool(note)


def test_fit_motion_drops_middle_scenes_when_beats_are_not_enough():
    ad = MotionAd.sized("9:16", bpm=72)
    scenes = [MotionScene(kind="hook", beats=3, headline="饭后油腻？")]
    # 每屏已是最少拍数（4 拍），减拍数不够，只能删中间分镜
    scenes += [MotionScene(kind="stat", beats=4, headline=f"数据 {i}", stat_value="39.3%") for i in range(8)]
    scenes.append(MotionScene(kind="cta", beats=6, headline="带回家", items=["立即下单"]))
    ad.replace_scenes(scenes)
    assert ad.total_seconds > 30
    note = fit_motion(ad, 30)
    assert ad.total_seconds <= 30 and "删掉中间" in note
    assert ad.scenes[0].kind == "hook" and ad.scenes[-1].kind == "cta"
    assert fit_motion(ad, 30) == ""  # 已在上限内：不改


def test_visitor_projects_are_capped(public, public_rt, monkeypatch):
    import app.engine.public_demo as pd

    monkeypatch.setattr(pd, "MAX_VISITOR_PROJECTS", 3)
    first = public.new_visitor_project()
    _age(public_rt.store.project_dir(first.project_id) / "project.json", 600)  # 最久没有操作
    for _ in range(4):
        public.new_visitor_project()
    left = [d.name for d in public_rt.store.root.iterdir() if d.name != TEMPLATE_ID]
    assert len(left) == 3 and first.project_id not in left


def test_linux_noto_fonts_use_simplified_chinese_face():
    """Docker 镜像里的 fonts-noto-cjk 是字体集合：第 0 个字形是日文，必须取第 2 个（简体中文）。"""
    from app.providers.demo.fonts import ttc_index

    assert ttc_index("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc") == 2
    assert ttc_index("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc") == 2
    assert ttc_index("C:/Windows/Fonts/msyh.ttc") == 0


def test_render_gate_limits_waiting_renders():
    gate = RenderGate(max_size=2)
    assert gate.enter() == 0 and gate.enter() == 1 and gate.enter() is None
    gate.leave()
    assert gate.enter() == 1 and gate.size == 2


# ---------------------------------------------------------------- 界面


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def public_server(public_rt, monkeypatch):
    from app.ui.app import build_app

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    demo = build_app(public_rt.settings, runtime=public_rt)
    demo.public.prepare()
    demo.queue().launch(server_name="127.0.0.1", server_port=_free_port(), prevent_thread_lock=True, quiet=True,
                        allowed_paths=[str(public_rt.settings.workspace_dir)])
    yield demo
    demo.close()


def _labels(demo) -> set[str]:
    return {str(c.get("props", {}).get("label") or "") for c in demo.config["components"]}


def test_public_ui_hides_api_keys_and_shared_lists(public_server, public_rt):
    labels = _labels(public_server)
    assert not any("KEY" in label or "BASE_URL" in label for label in labels)  # API 配置面板整个不创建
    assert "🧪 本地演示测试模式（勾选后即使填了 Key 也不调用真实接口）" not in labels
    radios = [c["props"] for c in public_server.config["components"] if c.get("props", {}).get("label") == "音色来源"]
    assert radios and all("library" not in [ch[1] for ch in r["choices"]] for r in radios)
    sliders = [c["props"] for c in public_server.config["components"]  # 动效广告的时长滑块（视频选项卡另有一个）
               if c.get("props", {}).get("label") == "目标时长（秒）" and c["props"].get("minimum") == 8]
    assert len(sliders) == 1 and sliders[0]["maximum"] == PUBLIC_MOTION_MAX_SECONDS
    assert public_server.public.rt is public_rt and "公共免 Key 在线演示" in public_rt.mode_summary()


def test_visitor_runs_motion_preview_within_limits(public_server, public_rt):
    """访客打开页面得到示例项目 → AI 编排（演示）→ 快速预览：不超过 30 秒；排队满了直接提示。"""
    from app.engine.video_cloner import ffmpeg_exe
    from app.ui.motion_tab import REFRESH_KEYS

    client = Client(public_server.local_url, verbose=False)
    out = client.predict(api_name="/public_visit")
    pid = next(p.project_id for p in public_rt.store.list_projects() if p.project_id != TEMPLATE_ID)
    assert pid in out[0]  # gradio_client 不返回 State 输出：[项目信息, 项目名称]
    out = client.predict("9:16", 30, 100, "keynote_dark", "", "default_motion_director", "", "project", 4,
                         api_name="/motion_plan")
    ad = public_rt.store.load(pid).motion_ad
    assert "动效方案完成（演示模式）" in out[0] and ad.total_seconds <= PUBLIC_MOTION_MAX_SECONDS
    table = out[1 + REFRESH_KEYS.index("table")]
    opts = ("9:16", 30, 100, "keynote_dark", False, False, False)

    gate = public_server.public.render_gate
    gate._count = gate.max_size  # 模拟排满
    out = client.predict(table, *opts, out[0], api_name="/motion_preview")
    assert "排队渲染的人太多" in out[0] and public_rt.store.load(pid).motion_ad.latest_render is None
    gate._count = 0

    if ffmpeg_exe():
        out = client.predict(table, *opts, out[0], api_name="/motion_preview")
        last = public_rt.store.load(pid).motion_ad.latest_render
        assert "快速预览完成" in out[0] and last.seconds <= PUBLIC_MOTION_MAX_SECONDS + 0.05
        assert gate.size == 0


def test_main_applies_public_queue_limits(public_rt, monkeypatch):
    import app.main as entry
    from app.settings import PUBLIC_EVENT_QUEUE_MAX_SIZE, PUBLIC_MAX_UPLOAD_SIZE

    calls = {}

    class FakeDemo:
        public = PublicDemo(public_rt)

        def queue(self, **kw):
            calls["queue"] = kw
            return self

        def launch(self, **kw):
            calls["launch"] = kw

    monkeypatch.setattr(FakeDemo.public, "prepare", lambda: calls.setdefault("prepared", True))
    monkeypatch.setattr(entry, "load_settings", lambda: public_rt.settings)
    monkeypatch.setattr(entry, "build_app", lambda settings: FakeDemo())
    entry.main()
    assert calls["prepared"] and calls["queue"]["max_size"] == PUBLIC_EVENT_QUEUE_MAX_SIZE
    assert calls["launch"]["max_file_size"] == PUBLIC_MAX_UPLOAD_SIZE
