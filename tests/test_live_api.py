"""真实 API 模式：用 httpx.MockTransport 模拟 RightAPI，验证请求路径、Key 隔离与响应解析。"""

import base64
import json
from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.engine.planner_engine import PlannerEngine
from app.engine.project_service import create_project
from app.engine.render_engine import RenderEngine
from app.engine.runtime import EngineRuntime
from app.providers.image import ImageAPIError, RightAPIImageClient
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

    llm = RightAPILLM("https://www.rightapi.ai", "llm-key", "claude-opus-5-5", "auto",
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


async def test_gemini_async_task_polling():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req.url.path)
        if req.method == "POST":
            assert req.url.path.endswith(":generateContent")
            assert json.loads(req.content)["async"] is True
            return httpx.Response(200, json={"task_id": "t1"})
        if len(seen) < 3:
            return httpx.Response(200, json={"status": "running"})
        return httpx.Response(200, json={"status": "succeeded", "result": {
            "candidates": [{"content": {"parts": [{"inlineData": {"data": _png_b64()}}]}}]}})

    from app.engine.image_router import route_image_request
    req = route_image_request(Settings(gemini_async=True), "gemini-nano-banana-2.1", "p", "1:1", [])
    client = RightAPIImageClient(task_url_template="https://www.rightapi.ai/v1/tasks/{task_id}", poll_interval=0,
                                 transport=httpx.MockTransport(handler))
    imgs = await client.send(req, "g-key")
    assert len(imgs) == 1 and seen[1] == "/v1/tasks/t1"

    no_template = RightAPIImageClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ImageAPIError, match="TASK_URL_TEMPLATE"):
        await no_template.send(req, "g-key")


async def test_live_plan_and_render_use_isolated_keys(tmp_path):
    """真实模式全链路：LLM 策划 + OpenAI 生图，各自使用自己的 Key 与路径。"""
    log = []

    def handler(req: httpx.Request):
        path, auth = req.url.path, req.headers.get("authorization")
        log.append((path, auth))
        if path == "/v1/messages":
            assert auth == "Bearer LLM"
            content = json.loads(req.content)["messages"][0]["content"]
            if "slot_json" in content or "槽位策划" in content:
                out = {"prompt": "real prompt", "negative_notes": "none"}
            else:
                out = PLAN_JSON
            return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(out)}]})
        if path in ("/v1/images/generations", "/v1/images/edits"):
            assert auth == "Bearer OAI"
            return httpx.Response(200, json={"data": [{"b64_json": _png_b64()}]})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    settings = Settings(workspace_dir=tmp_path, llm_key="LLM", llm_api_format="anthropic",
                        openai_image_key="OAI", gemini_image_key="GEM")
    rt = EngineRuntime(settings=settings, demo_delay=0)
    rt.llm_override = RightAPILLM(settings.rightapi_base_url, "LLM", "m", "anthropic", transport=transport)
    rt.image_client_override = RightAPIImageClient(transport=transport)

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

    events = [e async for e in RenderEngine(rt).render_plan(project, plan, "gpt-image-2", "gpt_image_2")]
    assert sum(1 for e in events if e.image_path) == 2, [e.message for e in events]
    paths = [p for p, _ in log]
    assert "/v1/images/edits" in paths and "/v1/images/generations" in paths
    assert plan.slots[0].compiled_prompt.startswith("real prompt")


def test_save_env_roundtrip_and_whitelist(tmp_path, monkeypatch):
    for k in EDITABLE_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    s = save_env({"RIGHTAPI_LLM_KEY": "abc\nINJECTED=1", "MODAL_H3_ENDPOINT_URL": "https://h3.modal.run",
                  "DEMO_MODE": "true"}, env)
    text = env.read_text(encoding="utf-8")
    # 换行被移除，无法注入新的一行环境变量
    assert "\nINJECTED=1" not in text and s.llm_key == "abcINJECTED=1"
    assert s.modal_h3_url == "https://h3.modal.run" and s.demo_mode is True
    assert not s.channel_live("llm")
    with pytest.raises(ValueError):
        save_env({"PATH": "x"}, env)
    assert load_settings(env, override=True).modal_h3_url == "https://h3.modal.run"
