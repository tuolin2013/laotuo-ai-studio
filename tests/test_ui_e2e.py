"""界面端到端测试：启动真实 Gradio 服务，用 gradio_client 像浏览器一样驱动三大选项卡全流程（演示模式）。"""

import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
from gradio_client import Client, handle_file
from PIL import Image

from app.engine.compiler import h3_structure_issues
from app.engine.prompt_compiler import auto_h3_mode
from app.engine.runtime import EngineRuntime
from app.engine.skill_loader import SKILLS_DIR, SkillRegistry
from app.settings import Settings
from app.ui.app import build_app

INFO = ["特级芽尖", "霜厚", "", "张家界永定区", "50g×2罐", "到手2罐", "", ""]
TEA = "zhangjiajie_berry_tea"
# 视频选项卡输出位置：[logs, status, table, shot_pick, checklist, 8×上传框, frames, locked_frames, video_pick,
#                      video_progress, 12×分镜播放器, versions, h3_shot, h3_info, h3_edit, …, clone_gallery, clone_md]
V_STATUS, V_TABLE, V_PICK, V_PROGRESS = 1, 2, 15, 16
V_PLAYERS = slice(17, 29)
V_VERSIONS, V_H3_EDIT = 29, 32
# 类目专属策划卡带：开源仓库里没有（行业卡带在 Pro 卡带包中），测试时临时放一张
PET_ONLY_PLANNER = "---\nskill_id: pet_only_planner\nname: 宠物类目专属策划\ncategories: [pet_supplement]\n---\n宠物\n"


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
    # 卡带目录用副本：不加载本机的 Pro 卡带包，测试结果与是否安装无关
    skills = tmp_path_factory.mktemp("cards") / "skills"
    shutil.copytree(SKILLS_DIR, skills)
    (skills / "ecommerce_planner" / "pet_only.md").write_text(PET_ONLY_PLANNER, encoding="utf-8")
    rt = EngineRuntime(settings=Settings(workspace_dir=ws), skills=SkillRegistry(skills), demo_delay=0,
                       force_demo=True)
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
                "/video_confirm", "/video_first_frames", "/video_lock_all", "/video_h3", "/video_render",
                "/video_edit_plan", "/video_edit_save", "/video_edit_render", "/video_export_jianying",
                "/video_export_resolve", "/voice_write", "/voice_split", "/voice_save", "/voice_preview",
                "/voice_lock", "/voice_render", "/voice_mix", "/voice_transcribe", "/voice_library_save"}
    assert expected <= names


def _voice(out, key: str):
    """配音选项卡输出：[运行日志, *刷新输出]，按 REFRESH_KEYS 的名字取位置。"""
    from app.ui.voice_tab import REFRESH_KEYS

    return out[1 + REFRESH_KEYS.index(key)]


def test_voice_workflow_through_ui(server):
    """【配音】AI 撰写文案 → 表格微调 → 试听 → 逐段生成（自动锁定音色）→ 改一段只重做这一段 → 合成完整配音。"""
    from app.engine.video_cloner import ffmpeg_exe

    client, rt, _ = server
    client.predict("配音验收", TEA, "taobao", *INFO, api_name="/new_project")
    pid = next(p.project_id for p in rt.store.list_projects() if p.name == "配音验收")

    out = client.predict("mandarin", "live_selling", 30, "开头用反问", "default_voice_writer", "",
                         api_name="/voice_write")
    st = rt.store.load(pid).voice_studio
    assert "配音文案完成" in out[0] and len(st.segments) >= 3
    assert st.source.description and out[-1] == st.source.description  # AI 建议的音色填进 Step 2
    table = _voice(out, "table")
    assert len(table["data"]) == len(st.segments)

    table["data"][0][1] = "（放慢语速）" + table["data"][0][1]
    out = client.predict(table, None, out[0], api_name="/voice_save")
    first = rt.store.load(pid).voice_studio.segments[0]
    assert "已保存表格修改（1 段" in _last(out[0]) and first.control == "放慢语速" and "（" not in first.text

    src = ("design", st.source.description, "", True, False, "", None)
    params = (2.0, 10, True, 42, True)
    log = client.predict("", *src, *params, out[0], api_name="/voice_preview")[0]
    assert "试听已生成" in _last(log)

    table = _voice(out, "table")
    out = client.predict(table, "pending", [], *src, *params, log, api_name="/voice_render")
    st = rt.store.load(pid).voice_studio
    n = len(st.segments)
    assert f"成功 {n}/{n}" in _last(out[0]) and "自动锁定" in out[0]
    assert st.source.design_locked and not st.missing_segments()

    # 改一段文案：「生成未完成的段落」只重做这一段
    table["data"][1][1] = "改过的第二段：表面这层天然白霜，懂茶的人一看就知道。"
    out = client.predict(table, "pending", [], *src, *params, out[0], api_name="/voice_render")
    st = rt.store.load(pid).voice_studio
    assert "成功 1/1" in _last(out[0]) and [len(s.takes) for s in st.segments] == [1, 2, *[1] * (n - 2)]

    if ffmpeg_exe():
        out = client.predict(out[0], api_name="/voice_mix")
        mix = rt.store.load(pid).voice_studio.latest_mix
        assert "完整配音已生成" in _last(out[0]) and mix.segment_count == n
        assert {p.suffix for p in mix.files} == {".wav", ".mp3", ".srt", ".zip"}
        assert "改过的第二段" in mix.srt_path.read_text(encoding="utf-8-sig")


def test_full_workflow_through_ui(server, tmp_path):
    client, rt, _ = server
    client.predict("UI验收", TEA, "taobao", *INFO, api_name="/new_project")

    # ---------------- 主图：生成 -> 确认 -> 上传参考图 -> 渲染
    out = client.predict(TEA, "taobao", 5, "1:1", "auto", "中秋主题，暖金色调", "default_planner", "", *INFO,
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
    out = client.predict(TEA, "taobao", "auto", 8, "3:4", "clinical_lab_minimal", "", "default_planner", "", *INFO,
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
    out = client.predict("minimax_h3_prompt", out[0], api_name="/video_h3")
    assert out[V_H3_EDIT].startswith("[") and "9:16" in out[V_H3_EDIT]  # 旧版卡带仍可用
    # 默认官方卡带：不用选模式，按分镜是否绑定商品参考图自动选择 I2VA / Ref2VA
    out = client.predict("official_h3_prompt_writing", out[0], api_name="/video_h3")
    script = rt.store.load(project.project_id).video_script
    modes = [auto_h3_mode(script, s) for s in script.shots]
    assert all(s.h3_official_mode == m and h3_structure_issues(s.h3_prompt, m) == []
               for s, m in zip(script.shots, modes, strict=True))
    assert out[V_H3_EDIT] == script.shots[0].h3_prompt
    for s in script.shots:
        with Image.open(s.confirmed_first_frame.file_path) as im:
            assert abs(im.width / im.height - 9 / 16) < 0.01

    from app.providers.video import DemoVideoProvider
    if DemoVideoProvider.ffmpeg_path():
        _check_video_render(client, rt, project.project_id, out[0])


def _video_path(value) -> str | None:
    """gradio_client 返回的播放器值：gr.update 字典（value 字段）或 {"video": 路径}。"""
    while isinstance(value, dict):
        value = value.get("value", value.get("video"))
    return value or None


def _check_video_render(client, rt, pid: str, log: str) -> None:
    """Step 3：① 逐个分镜依次生成 → 每个分镜单独回显；② 选分镜重新生成 → 追加新版本，不覆盖其他分镜。"""
    out = client.predict("all", [], True, "official_h3_prompt_writing", log, api_name="/video_render")
    script = rt.store.load(pid).video_script
    n = len(script.shots)
    assert f"成功 {n}/{n}" in _last(out[0])
    shown = [_video_path(v) for v in out[V_PLAYERS]][:n]
    assert all(shown) and len(set(shown)) == n  # 每个分镜一个播放器，各自显示自己的视频
    assert all(len(s.videos) == 1 for s in script.shots)

    first = script.shots[0].shot_id
    out = client.predict("pick", [first], True, "official_h3_prompt_writing", out[0], api_name="/video_render")
    script = rt.store.load(pid).video_script
    assert [len(s.videos) for s in script.shots] == [2, *[1] * (n - 1)]
    again = [_video_path(v) for v in out[V_PLAYERS]][:n]
    assert again[0] != shown[0] and again[1:] == shown[1:]  # 只有被选中的分镜换成新版本
    assert len(out[V_VERSIONS]) == n + 1 and "镜头 1 ✅ 2 版" in out[V_PROGRESS]
    assert all(v.file_path.exists() for s in script.shots for v in s.videos)  # 旧版本文件仍在

    out = client.predict("all", [], True, "official_h3_prompt_writing", out[0], api_name="/video_render")
    assert "都已有视频" in _last(out[0])
    _check_edit(client, rt, pid, out[0])


def _check_edit(client, rt, pid: str, log: str) -> None:
    """Step 4：剪辑大师生成方案 → 表格微调 → 一键成片 → 导出剪映草稿 / 达芬奇工程。"""
    out = client.predict("default_video_editor", "控制在 20 秒内", log, api_name="/video_edit_plan")
    plan = rt.store.load(pid).video_edit
    script = rt.store.load(pid).video_script
    assert plan is not None and len(plan.clips) == len(script.shots)
    assert plan.clips[0].shot_index == 1 and "剪辑方案完成" in out[0]
    # 最新视频版本：镜头 1 生成过两版，剪辑方案用第 2 版
    assert plan.clips[0].video_id == script.shots[0].videos[-1].video_id
    table = out[2]
    assert len(table["data"]) == len(script.shots) and table["data"][0][1] == "镜头 1"

    table["data"][-1][2] = False  # 停用最后一个镜头
    out = client.predict(table, True, True, 0.2, True, out[0], api_name="/video_edit_save")
    assert "已保存剪辑修改" in _last(out[0])
    assert not rt.store.load(pid).video_edit.clips[-1].enabled

    out = client.predict(out[2], True, True, 0.2, True, out[0], api_name="/video_edit_render")
    assert "成片已生成" in _last(out[0])
    cut = rt.store.load(pid).video_edit.latest_render
    assert cut is not None and cut.file_path.exists() and _video_path(out[3])

    table, log = out[2], out[0]
    # 剪映草稿文件夹留空：只打包下载（测试机不写入真实的剪映目录）
    out = client.predict(table, True, True, 0.2, True, "", log, api_name="/video_export_jianying")
    assert "剪映草稿已导出" in _last(out[0]) and str(out[1]).endswith(".zip")
    out = client.predict(table, True, True, 0.2, True, out[0], api_name="/video_export_resolve")
    assert "达芬奇工程已导出" in _last(out[0]) and str(out[1]).endswith(".zip")


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
    from app.providers.video import DemoVideoProvider
    if DemoVideoProvider.ffmpeg_path():
        # 不手动编写 H3 提示词，直接生成：系统自动编写后再生成视频
        out = client.predict("all", [], True, "official_h3_prompt_writing", out[0], api_name="/video_render")
        assert "自动编写 H3" in out[0]
        script = rt.store.load(pid).video_script
        assert all(s.videos and s.h3_prompt for s in script.shots)
        assert all(h3_structure_issues(s.h3_prompt, s.h3_official_mode) == [] for s in script.shots)


def test_category_skill_accepted_without_category_change_event(server):
    """服务重启后，旧页面直接提交其他类目的专属卡带（服务端会话里没有切换类目的记录）也能生成策划。"""
    client, _, _ = server
    pet_info = ["猫犬通用", "辰节宁", "", "", "", "", "", ""]
    client.predict("重启后旧页面", "pet_supplement", "taobao", *pet_info, api_name="/new_project")
    skill = "pet_only_planner"
    out = client.predict("pet_supplement", "taobao", 5, "1:1", "auto", "", skill, "", *pet_info,
                         api_name="/main_image_generate")
    assert "策划完成" in _last(out[0]) and len(out[2]["data"]) == 5


def test_clone_without_video_is_rejected(server):
    client, _, _ = server
    client.predict("克隆缺视频", TEA, "taobao", *INFO, api_name="/new_project")
    with pytest.raises(Exception, match="上传参考爆款视频"):
        client.predict(*video_generate_args("clone", None), api_name="/video_generate")
