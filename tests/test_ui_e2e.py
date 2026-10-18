"""界面端到端测试：启动真实 Gradio 服务，用 gradio_client 像浏览器一样驱动三大选项卡全流程（演示模式）。"""

import os
import socket
import subprocess
from pathlib import Path

import pytest
from gradio_client import Client, handle_file
from PIL import Image

from app.engine.runtime import EngineRuntime
from app.settings import Settings
from app.ui.app import build_app

INFO = ["特级芽尖", "霜厚", "", "张家界永定区", "50g×2罐", "到手2罐", "", ""]
TEA = "zhangjiajie_berry_tea"
# 视频选项卡输出位置：[logs, status, table, shot_pick, checklist, 8×上传框, frames, locked_frames, edit_shot,
#                      h3_edit, comfy_link, shot1_prompt, global_prompt, …, clone_gallery, clone_md]
V_TABLE, V_SHOT1, V_GLOBAL = 2, 18, 19


def video_generate_args(mode: str, video=None, frames: int = 6, idea: str = "办公室女生冲一杯") -> tuple:
    """/video_generate 入参：类目、平台、策划方式、视频类型、比例、分镜数、想法、编剧卡带、
    参考视频、克隆策略、抽帧数、日志、商品信息。"""
    return (TEA, "taobao", mode, "product_showcase", "9:16", 4, idea, "default_scriptwriter", video, "replicate",
            frames, "", *INFO)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    ws = tmp_path_factory.mktemp("ws")
    rt = EngineRuntime(settings=Settings(workspace_dir=ws), demo_delay=0, force_demo=True)
    demo = build_app(rt.settings, runtime=rt)
    port = _free_port()
    demo.queue().launch(server_name="127.0.0.1", server_port=port, prevent_thread_lock=True, quiet=True,
                        allowed_paths=[str(ws)])
    yield Client(f"http://127.0.0.1:{port}/", verbose=False), rt, ws
    demo.close()


def _last(log: str) -> str:
    return log.strip().splitlines()[-1]


def _png(path: Path, color) -> str:
    Image.new("RGB", (240, 320), color).save(path)
    return str(path)


def test_app_builds_with_all_tabs(server):
    client, _, _ = server
    names = set(client.view_api(return_format="dict", print_info=False)["named_endpoints"])
    expected = {"/new_project", "/main_image_generate", "/main_image_confirm", "/main_image_render",
                "/detail_page_generate", "/detail_page_reuse", "/detail_page_render", "/video_generate",
                "/video_confirm", "/video_first_frames", "/video_lock_all", "/video_h3", "/video_generate_all"}
    assert expected <= names


def test_full_workflow_through_ui(server, tmp_path):
    client, rt, _ = server
    client.predict("UI验收", TEA, "taobao", *INFO, api_name="/new_project")

    # ---------------- 主图：生成 -> 确认 -> 上传参考图 -> 渲染
    out = client.predict(TEA, "taobao", 5, "1:1", "中秋主题，暖金色调", "default_planner", "", *INFO,
                         api_name="/main_image_generate")
    assert "策划完成" in _last(out[0]) and len(out[2]["data"]) == 5 and "草稿" in out[1]
    out = client.predict(out[2], out[0], api_name="/main_image_confirm")
    assert "已确认锁定" in _last(out[0])

    project = next(iter(rt.store.list_projects()))
    plan = rt.store.load(project.project_id).main_image_plan
    assert plan.plan_status == "confirmed" and plan.user_creative_idea == "中秋主题，暖金色调"

    # 上传第 1 个清单角色（等同于在界面上传图片）
    first_role = plan.reference_checklist[0].role
    client.predict(handle_file(_png(tmp_path / "pkg.png", (150, 30, 30))), out[0],
                   api_name="/main_image_upload_ref_0")
    plan = rt.store.load(project.project_id).main_image_plan
    assert [a.role for a in plan.reference_assets] == [first_role]

    out = client.predict("gpt-image-2.5-flare", 2, "2K", "high", out[0], api_name="/main_image_render")
    assert "全部完成" in _last(out[0])
    gallery = out[-2]
    assert len(gallery) == 10

    # ---------------- 详情页：自动屏数 -> 确认 -> 一键复用主图参考图 -> 渲染 -> 长图序列
    out = client.predict(TEA, "taobao", "auto", 8, "3:4", "", "default_planner", "", *INFO,
                         api_name="/detail_page_generate")
    screens = len(out[2]["data"])
    assert screens >= 3
    out = client.predict(out[2], out[0], api_name="/detail_page_confirm")
    out = client.predict(out[0], api_name="/detail_page_reuse")
    assert first_role in _last(out[0])
    out = client.predict("gemini-nano-banana-2.1", 1, "1K", "high", out[0], api_name="/detail_page_render")
    assert len(out[-1]) == screens  # 长图序列每屏一张

    # ---------------- 视频（📝 原创）：剧本 -> 确认 -> 复用参考图 -> 首帧 -> 锁定 -> H3 -> 生成
    out = client.predict(*video_generate_args("original"), api_name="/video_generate")
    rows = out[V_TABLE]["data"]
    assert len(rows) == 4 and any(r[8] for r in rows) and all(r[7] for r in rows)
    assert "原创剧本完成" in _last(out[0]) and "来源：📝 原创策划" in out[1]
    out = client.predict(out[2], out[0], api_name="/video_confirm")
    out = client.predict(out[0], api_name="/video_reuse")
    out = client.predict("gpt-image-2", 1, "2K", out[0], api_name="/video_first_frames")
    assert "首帧生成完成" in _last(out[0])
    out = client.predict(out[0], api_name="/video_lock_all")
    out = client.predict("minimax_h3_prompt", "i2va", out[0], api_name="/video_h3")
    assert out[V_SHOT1].startswith("[") and "9:16" in out[V_SHOT1]  # 旧版卡带仍可用
    # 默认官方卡带：A + B 同时生成，视频生成使用 I2VA 版本
    out = client.predict("official_h3_prompt_writing", "both", out[0], api_name="/video_h3")
    shot1_prompt, global_prompt = out[V_SHOT1], out[V_GLOBAL]
    assert shot1_prompt.startswith("For the target video, at 0.00 seconds") and "non_diegetic_music:" in shot1_prompt
    assert "第 1 分镜锁定首帧" in global_prompt and "镜头 4" in global_prompt

    script = rt.store.load(project.project_id).video_script
    assert all(s.confirmed_first_frame_id and s.h3_prompt for s in script.shots)
    assert all(s.h3_prompt_mode == "i2va" and set(s.h3_prompt_variants) == {"i2va", "ref2va"} for s in script.shots)
    assert all("<Picture 1>" in s.h3_prompt_variants["ref2va"] for s in script.shots)
    for s in script.shots:
        with Image.open(s.confirmed_first_frame.file_path) as im:
            assert abs(im.width / im.height - 9 / 16) < 0.01

    from app.providers.video import DemoVideoProvider
    if DemoVideoProvider.ffmpeg_path():
        out = client.predict("modal_minimax_h3", out[0], api_name="/video_generate_all")
        assert "视频已绑定" in out[0]
        assert all(s.videos for s in rt.store.load(project.project_id).video_script.shots)


def _viral_video(path: Path) -> str:
    """合成一段 8 秒 9:16 测试视频：红 / 蓝 / 绿 / 黄 各 2 秒。"""
    from app.engine.video_cloner import ffmpeg_exe

    exe = ffmpeg_exe()
    if not exe:
        pytest.skip("没有可用的 ffmpeg")
    colors = ["red", "blue", "green", "yellow"]
    args = [exe, "-y", "-loglevel", "error"]
    for c in colors:
        args += ["-f", "lavfi", "-i", f"color=c={c}:s=360x640:r=24:d=2"]
    args += ["-filter_complex", "".join(f"[{i}:v]" for i in range(4)) + "concat=n=4:v=1:a=0",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True, timeout=120)
    return str(path)


def test_clone_workflow_through_ui(server, tmp_path):
    """📹 克隆参考爆款视频：上传 -> 抽帧 + 拆解 -> 同一个分镜表 -> 确认 -> 首帧 -> H3 -> 生成视频。"""
    client, rt, ws = server
    client.predict("克隆验收", TEA, "taobao", *INFO, api_name="/new_project")
    pid = next(p.project_id for p in rt.store.list_projects() if p.name == "克隆验收")
    video = handle_file(_viral_video(tmp_path / "viral.mp4"))

    out = client.predict(*video_generate_args("clone", video, frames=4, idea="人设换成宝妈"),
                         api_name="/video_generate")
    log, rows = out[0], out[V_TABLE]["data"]
    assert "抽帧完成" in log and "00:01.0、00:03.0、00:05.0、00:07.0" in log
    assert "克隆剧本完成" in _last(log) and len(rows) == 4 and rows[0][7]
    assert "来源：📹 克隆「viral.mp4」" in out[1]
    # clone_result 是布局容器（gr.Column），gradio_client 不返回它的值；最后两项是关键帧画廊与拆解报告
    clone_gallery, clone_md = out[-2:]
    assert len(clone_gallery) == 4
    assert "原爆款视频拆解报告" in clone_md and "镜头 1 ← 00:01.0" in clone_md

    project = rt.store.load(pid)
    script = project.video_script
    assert script.script_mode == "clone" and script.scriptwriter_skill_id == "hypit_viral_clone"
    frames_dir = ws / "projects" / pid / "reference_video_frames"
    assert len(list(frames_dir.rglob("keyframe_*.jpg"))) == 4 and list(frames_dir.rglob("source.mp4"))

    # 同一个分镜表：在克隆结果上直接修改台词再确认
    table = out[V_TABLE]
    table["data"][1][7] = "native_dialogue"
    table["data"][1][8] = "宝妈对着镜头笑着说：“孩子睡了，我也泡一杯。”"
    table["data"][1][9] = ""
    out = client.predict(table, out[0], api_name="/video_confirm")
    assert "已确认" in _last(out[0])
    edited = rt.store.load(pid).video_script.shots[1]
    assert edited.character_dialogue.endswith("我也泡一杯。”") and edited.source_timestamp == "00:03.0"

    out = client.predict(out[0], api_name="/video_reuse")
    out = client.predict("gpt-image-2", 1, "1K", out[0], api_name="/video_first_frames")
    assert "首帧生成完成" in _last(out[0])
    out = client.predict(out[0], api_name="/video_lock_all")
    out = client.predict("official_h3_prompt_writing", "both", out[0], api_name="/video_h3")
    assert out[V_SHOT1].startswith("For the target video, at 0.00 seconds")
    script = rt.store.load(pid).video_script
    assert all(set(s.h3_prompt_variants) == {"i2va", "ref2va"} for s in script.shots)

    from app.providers.video import DemoVideoProvider
    if DemoVideoProvider.ffmpeg_path():
        out = client.predict("modal_minimax_h3", out[0], api_name="/video_generate_all")
        assert all(s.videos for s in rt.store.load(pid).video_script.shots)


def test_clone_without_video_is_rejected(server):
    client, _, _ = server
    client.predict("克隆缺视频", TEA, "taobao", *INFO, api_name="/new_project")
    with pytest.raises(Exception, match="上传参考爆款视频"):
        client.predict(*video_generate_args("clone", None), api_name="/video_generate")
