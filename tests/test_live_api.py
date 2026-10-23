"""真实 API 模式：用 httpx.MockTransport 模拟 RightAPI，验证请求路径、2 把 Key 的分工与响应解析。"""

import base64
import json
from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.engine.diagnostics import diagnose
from app.engine.planner_engine import PlannerEngine
from app.engine.project_service import create_project
from app.engine.render_engine import RenderEngine
from app.engine.runtime import EngineRuntime
from app.providers.image import RightAPIImageClient
from app.providers.llm import LLMError, RightAPILLM
from app.settings import EDITABLE_ENV_KEYS, Settings, load_settings, save_env


def _png_b64() -> str:
    buf = BytesIO()
    Image.new("RGB", (64, 64), (10, 200, 10)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


PLAN_JSON = {
    "plan_summary": "真实 LLM 策划",
    "slots": [
        {"title": "霜厚回甘", "visual_description": "罐子居中", "generation_mode": "text_to_image",
         "required_references": [{"role": "product_package", "description": "罐子"}]},
        {"title": "云雾产区", "visual_description": "峰林", "generation_mode": "text_to_image",
         "required_references": []},
    ],
}


async def test_llm_auto_falls_back_to_chat_completions():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if req.url.path == "/v1/messages":
            return httpx.Response(404)
        assert req.headers["authorization"] == "Bearer llm-key"
        body = json.loads(req.content)
        assert body["messages"][0]["role"] == "system"
        return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n{\"ok\": 1}\n```"}}]})

    llm = RightAPILLM("https://rightapi.ai", "llm-key", "claude-opus-5-5", "auto",
                      transport=httpx.MockTransport(handler))
    assert await llm.complete_json("s", "u") == {"ok": 1}
    assert calls == ["/v1/messages", "/v1/chat/completions"] and llm.resolved_format == "openai"
    await llm.complete("s", "u")
    assert calls[-1] == "/v1/chat/completions"  # 记住可用格式，不再探测


async def test_llm_anthropic_format():
    def handler(req: httpx.Request):
        assert req.url.path == "/v1/messages"
        body = json.loads(req.content)
        assert body["system"] == "s" and req.headers["anthropic-version"]
        return httpx.Response(200, json={"content": [{"type": "text", "text": '{"a": 2}'}]})

    llm = RightAPILLM("https://x.ai", "k", "m", "anthropic", transport=httpx.MockTransport(handler))
    assert await llm.complete_json("s", "u") == {"a": 2}
    with pytest.raises(LLMError, match="401"):
        await RightAPILLM("https://x.ai", "k", "m", "anthropic",
                          transport=httpx.MockTransport(lambda r: httpx.Response(401))).complete("s", "u")


async def test_live_plan_and_render_use_two_keys(tmp_path):
    """真实模式全链路（官方异步协议）：LLM Key 只用于策划/提示词；Image Key 同时用于 GPT Image 与 Gemini。"""
    log = []
    png = base64.b64decode(_png_b64())
    tasks: dict[str, str] = {}

    def handler(req: httpx.Request):
        path, auth = req.url.path, req.headers.get("authorization")
        log.append((path, auth))
        if req.url.host == "cdn.example.com":
            return httpx.Response(200, content=png)
        if path == "/v1/messages":
            assert auth == "Bearer LLM"
            content = json.loads(req.content)["messages"][0]["content"]
            out = {"prompt": "real prompt", "negative_notes": "none"} if "槽位策划" in content else PLAN_JSON
            return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(out)}]})
        if path in ("/v1/images/generations", "/v1/images/edits") or path.endswith(":generateContent"):
            assert auth == "Bearer DRAW"
            body = json.loads(req.content)
            assert body["async"] is True
            tid = f"task_{len(tasks)}"
            tasks[tid] = "gemini" if path.endswith(":generateContent") else "openai"
            return httpx.Response(200, json={"task_id": tid, "status": "processing", "progress": 0})
        if path.startswith("/v1/tasks/"):
            assert auth == "Bearer DRAW"
            tid = path.rsplit("/", 1)[-1]
            url = f"https://cdn.example.com/{tid}.png"
            if tasks[tid] == "openai":
                return httpx.Response(200, json={"created": 1, "data": [{"url": url}]})
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": url}]}}]})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    settings = Settings(workspace_dir=tmp_path, llm_key="LLM", llm_api_format="anthropic", image_key="DRAW")
    rt = EngineRuntime(settings=settings, demo_delay=0)
    rt.llm_override = RightAPILLM(settings.rightapi_base_url, "LLM", "m", "anthropic", transport=transport)
    rt.image_client_override = RightAPIImageClient(base_url=settings.rightapi_base_url, poll_interval=0,
                                                   transport=transport)

    project = create_project(rt, "live", "zhangjiajie_berry_tea", "taobao")
    plan, mode = await PlannerEngine(rt).generate(
        deliverable_type="main_image", category_id="zhangjiajie_berry_tea", platform_id="taobao",
        aspect_ratio="1:1", count=2, user_creative_idea="中秋")
    assert mode == "真实 LLM"
    # LLM 给的模式与参考图数量不符时自动纠正
    assert plan.slots[0].generation_mode == "single_image_edit"
    project.main_image_plan = plan
    plan.confirm_plan()
    ref = tmp_path / "pkg.png"
    Image.new("RGB", (32, 32)).save(ref)
    plan.upload_reference("product_package", ref)
    for s in plan.slots:
        plan.set_candidate_count(s.slot_id, 2)

    events = [e async for e in RenderEngine(rt).render_plan(project, plan, "gpt-image-2.5-sunburst", "gpt_image_2",
                                                            resolution="4K", quality="max")]
    assert sum(1 for e in events if e.image_path) == 4, [e.message for e in events]
    paths = [p for p, _ in log]
    # 有参考图的槽位走 edits，无参考图的槽位走 generations
    assert paths.count("/v1/images/edits") == 2 and paths.count("/v1/images/generations") == 2
    # 1:1 的 4K 受总像素上限 8,294,400 约束，取最大合法值 2880x2880
    assert any("OpenAI 生成 /v1/images/edits · size 2880x2880" in e.message for e in events)
    assert any("quality max" in e.message for e in events)
    assert plan.slots[0].compiled_prompt.startswith("real prompt")
    assert all(c.file_path.read_bytes() == png for s in plan.slots for c in s.candidates)

    # 换成 Gemini 模型：同一把 Key，文生图与参考图编辑都走 generateContent
    log.clear()
    events = [e async for e in RenderEngine(rt).render_plan(project, plan, "gemini-3-pro-image-preview",
                                                            "gemini_nano_banana", resolution="4K")]
    assert sum(1 for e in events if e.image_path) == 4
    gen_paths = [p for p, _ in log if p.startswith("/v1beta")]
    assert gen_paths == ["/v1beta/models/gemini-3-pro-image-preview:generateContent"] * 4
    assert not any(p.startswith("/v1/images") for p, _ in log)
    # 画图请求从不使用 LLM Key
    assert all(a != "Bearer LLM" for p, a in log if p != "/v1/messages")


def test_save_env_roundtrip_and_whitelist(tmp_path, monkeypatch):
    for k in EDITABLE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    s = save_env({"RIGHTAPI_LLM_KEY": "abc\nINJECTED=1", "MODAL_H3_APP": "my-h3", "MODAL_H3_CLASS": "Runner",
                  "RIGHTAPI_IMAGE_KEY": "draw", "RIGHTAPI_IMAGE_RESOLUTION": "4K", "DEMO_MODE": "true"}, env)
    text = env.read_text(encoding="utf-8")
    # 换行被移除，无法注入新的一行环境变量
    assert "\nINJECTED=1" not in text and s.llm_key == "abcINJECTED=1"
    assert (s.modal_h3_app, s.modal_h3_class) == ("my-h3", "Runner") and s.demo_mode is True
    assert s.image_key == "draw" and s.image_resolution == "4K"
    with pytest.raises(ValueError):
        save_env({"RIGHTAPI_OPENAI_IMAGE_KEY": "x"}, env)  # 旧的三 Key 变量名不再从界面写入
    assert not s.channel_live("llm")
    with pytest.raises(ValueError):
        save_env({"PATH": "x"}, env)
    assert load_settings(env, override=True).modal_h3_app == "my-h3"
    with pytest.raises(ValueError):
        save_env({"MODAL_H3_ENDPOINT_URL": "https://x"}, env)  # 已废弃：H3 不再通过 URL 调用


async def test_diagnostics_without_spending_image_quota():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.path)
        if req.url.path == "/v1/messages":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}]})
        if req.url.path.startswith("/v1/tasks/task_"):
            assert req.headers["authorization"] == "Bearer DRAW-KEY-123456"
            return httpx.Response(404, json={"error": {"message": "任务不存在"}})
        return httpx.Response(500)

    s = Settings(llm_key="LLM-KEY-1234567", image_key="DRAW-KEY-123456", llm_api_format="anthropic")
    report = await diagnose(s, httpx.MockTransport(handler))
    assert "🟢 LLM" in report and "🟢 画图 · image 高质量" in report and "gemini-3-pro-image-preview(gemini)" in report
    # 普通渠道没填 Key：单独提示，不影响高质量渠道
    assert "🟡 画图 · image 普通" in report and "RIGHTAPI_IMAGE_STD_KEY" in report
    # Key 打码显示
    assert "DRAW-KEY-123456" not in report and "LLM-KEY-1234567" not in report
    # 诊断不提交画图任务，不消耗额度
    assert not any(c.startswith(("/v1/images", "/v1beta")) for c in calls)

    bad = await diagnose(s, httpx.MockTransport(lambda r: httpx.Response(401)))
    assert "❌ 画图" in bad and "Key 无效" in bad

    empty = await diagnose(Settings(), httpx.MockTransport(handler))
    assert empty.count("🟡") == 4

    # 两个渠道都配置：各自用自己的 Key 探测
    both = Settings(image_key="DRAW-KEY-123456", image_std_key="STD-KEY-1234567")
    probes = []

    def probe_handler(req: httpx.Request):
        probes.append(req.headers["authorization"])
        return httpx.Response(404)

    report2 = await diagnose(both, httpx.MockTransport(probe_handler))
    assert "🟢 画图 · image 普通" in report2 and "nano-banana-2-lite(gemini)" in report2
    assert "Bearer STD-KEY-1234567" in probes and "Bearer DRAW-KEY-123456" in probes
    assert "STD-KEY-1234567" not in report2
