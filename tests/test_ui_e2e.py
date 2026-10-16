"""界面端到端测试：启动真实 Gradio 服务，用 gradio_client 像浏览器一样驱动三大选项卡全流程（演示模式）。"""

import os
import socket
from pathlib import Path

import pytest
from gradio_client import Client, handle_file
from PIL import Image

from app.engine.runtime import EngineRuntime
from app.settings import Settings
from app.ui.app import build_app

INFO = ["特级芽尖", "霜厚", "", "张家界永定区", "50g×2罐", "到手2罐", "", ""]
TEA = "zhangjiajie_berry_tea"


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

    out = client.predict("gpt-image-2.5-flare", 2, out[0], api_name="/main_image_render")
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
    out = client.predict("gemini-3-pro-image-preview", 1, out[0], api_name="/detail_page_render")
    assert len(out[-1]) == screens  # 长图序列每屏一张

    # ---------------- 视频：剧本 -> 确认 -> 复用参考图 -> 首帧 -> 锁定 -> H3 -> 生成
    out = client.predict(TEA, "taobao", "immersive_demo", "9:16", 4, "办公室女生冲一杯", "default_scriptwriter", "",
                         *INFO, api_name="/video_generate")
    rows = out[2]["data"]
    assert len(rows) == 4 and any(r[8] for r in rows) and all(r[7] for r in rows)
    out = client.predict(out[2], out[0], api_name="/video_confirm")
    out = client.predict(out[0], api_name="/video_reuse")
    out = client.predict("gpt-image-2", 1, out[0], api_name="/video_first_frames")
    assert "首帧生成完成" in _last(out[0])
    out = client.predict(out[0], api_name="/video_lock_all")
    out = client.predict("minimax_h3_prompt", out[0], api_name="/video_h3")
    shot1_prompt, global_prompt = out[19], out[20]
    assert shot1_prompt.startswith("[") and "9:16" in shot1_prompt
    assert "第 1 分镜锁定首帧" in global_prompt and "镜头 4" in global_prompt

    script = rt.store.load(project.project_id).video_script
    assert all(s.confirmed_first_frame_id and s.h3_prompt for s in script.shots)
    for s in script.shots:
        with Image.open(s.confirmed_first_frame.file_path) as im:
            assert abs(im.width / im.height - 9 / 16) < 0.01

    from app.providers.video import DemoVideoProvider
    if DemoVideoProvider.ffmpeg_path():
        out = client.predict("modal_minimax_h3", out[0], api_name="/video_generate_all")
        assert "视频已绑定" in out[0]
        assert all(s.videos for s in rt.store.load(project.project_id).video_script.shots)
