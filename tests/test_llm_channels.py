"""LLM 渠道与中转：RightAPI 的 Key 绑定渠道（模型随渠道变），以及其他中转站的地址、协议与模型列表。"""

import json

import httpx
import pytest

from app.engine.diagnostics import check_llm
from app.engine.runtime import EngineRuntime
from app.providers.llm import API_FORMATS, LLMError, RightAPILLM, api_root, list_models, protocol_order
from app.providers.llm_providers import PROVIDERS, RIGHTAPI, get_provider, resolve_base_url
from app.settings import EDITABLE_ENV_KEYS, Settings, load_settings, normalize_llm_base_url, save_env
from app.ui.header import _llm_view, llm_env_updates

# RightAPI 的 /v1/models 只返回 Key 绑定渠道下的模型（2026-10-29 实测 Claude AwsQ 渠道）
AWSQ_MODELS = {"success": True, "object": "list",
               "data": [{"id": m, "object": "model"} for m in ("claude-opus-5-5", "claude-sonnet-5-5")]}


def test_api_root_keeps_existing_version():
    assert api_root("https://www.rightapi.ai", "openai") == "https://www.rightapi.ai/v1"
    assert api_root("https://www.rightapi.ai", "anthropic") == "https://www.rightapi.ai/v1"
    assert api_root("https://www.rightapi.ai", "gemini") == "https://www.rightapi.ai/v1beta"
    assert api_root("https://api.example.com/v1", "openai") == "https://api.example.com/v1"
    # 国产模型平台的版本号不是 v1，也不在结尾
    assert api_root("https://open.bigmodel.cn/api/paas/v4", "openai") == "https://open.bigmodel.cn/api/paas/v4"
    assert api_root("https://ark.cn-beijing.volces.com/api/v3", "openai").endswith("/api/v3")
    assert api_root("https://generativelanguage.googleapis.com/v1beta/openai", "openai").endswith("/v1beta/openai")
    assert api_root("https://openrouter.ai/api/v1", "openai") == "https://openrouter.ai/api/v1"
    # Gemini 原生接口在 /v1beta 下：填了 /v1 也改过来
    assert api_root("https://relay.example.com/v1", "gemini") == "https://relay.example.com/v1beta"


def test_normalize_llm_base_url_strips_pasted_endpoints():
    assert normalize_llm_base_url("") == ""
    assert normalize_llm_base_url("api.example.com/v1/") == "https://api.example.com/v1"
    assert normalize_llm_base_url("https://api.example.com/v1/chat/completions") == "https://api.example.com/v1"
    assert normalize_llm_base_url("https://x.ai/v1/messages") == "https://x.ai/v1"
    assert normalize_llm_base_url("https://x.ai/v1beta/models/gemini-3:generateContent") == "https://x.ai/v1beta"


def test_protocol_order_follows_model_family():
    assert protocol_order("claude-sonnet-5-5")[0] == "anthropic"
    assert protocol_order("gemini-3.8-flash")[0] == "gemini"
    assert protocol_order("grok-4.7")[0] == "responses"
    assert protocol_order("deepseek-v4-pro")[0] == "openai"
    assert protocol_order("gpt-6.1-sol") == ["openai", "responses"]
    # OpenRouter 一类带厂商前缀的模型名走 OpenAI 兼容
    assert protocol_order("anthropic/claude-sonnet-5-5")[0] == "openai"


async def test_list_models_rightapi_channel():
    seen = []

    def handler(req: httpx.Request):
        seen.append((str(req.url), req.headers["authorization"]))
        return httpx.Response(200, json=AWSQ_MODELS)

    models = await list_models("https://www.rightapi.ai", "sk-1", transport=httpx.MockTransport(handler))
    assert models == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert seen == [("https://www.rightapi.ai/v1/models", "Bearer sk-1")]


async def test_list_models_falls_back_to_gemini_listing():
    def handler(req: httpx.Request):
        if req.url.path == "/v1/models":
            return httpx.Response(404, json={"error": "not found"})
        assert req.url.path == "/v1beta/models" and req.headers["x-goog-api-key"] == "g-key"
        return httpx.Response(200, json={"models": [
            {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-9", "supportedGenerationMethods": ["embedContent"]}]})

    models = await list_models("https://relay.example.com", "g-key", transport=httpx.MockTransport(handler))
    assert models == ["gemini-3.8-flash"]  # 向量模型不能对话，去掉
    with pytest.raises(LLMError, match="401"):
        await list_models("https://x.ai", "bad", "openai",
                          transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "无效的API Key"})))
    with pytest.raises(LLMError, match="密钥"):
        await list_models("https://x.ai", "")


async def test_gemini_native_protocol():
    """RightAPI 的 Gemini 渠道只支持 Gemini 原生协议（文档给的是 :streamGenerateContent）。"""
    def handler(req: httpx.Request):
        assert req.url.path == "/v1beta/models/gemini-3.8-flash:streamGenerateContent"
        assert req.url.params["alt"] == "sse" and req.headers["x-goog-api-key"] == "g-key"
        body = json.loads(req.content)
        assert body["systemInstruction"]["parts"][0]["text"] == "sys"
        parts = body["contents"][0]["parts"]
        assert parts[0]["inline_data"]["data"] == "IMG" and parts[-1]["text"] == "u"
        chunks = [{"candidates": [{"content": {"parts": [{"text": "想一想", "thought": True}]}}]},
                  {"candidates": [{"content": {"parts": [{"text": '{"a": '}]}}]},
                  {"candidates": [{"content": {"parts": [{"text": "1}"}]}, "finishReason": "STOP"}]}]
        sse = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\r\n\r\n" for c in chunks)
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    llm = RightAPILLM("https://www.rightapi.ai", "g-key", "gemini-3.8-flash", "auto",
                      transport=httpx.MockTransport(handler))
    assert await llm.complete_json("sys", "u", images=["IMG"]) == {"a": 1}
    assert llm.resolved_format == "gemini"


async def test_responses_protocol_for_grok():
    def handler(req: httpx.Request):
        assert req.url.path == "/v1/responses"
        body = json.loads(req.content)
        assert body["instructions"] == "sys" and body["model"] == "grok-4.7"
        return httpx.Response(200, json={"object": "response", "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": '{"ok": true}'}]}]})

    llm = RightAPILLM("https://www.rightapi.ai", "k", "grok-4.7", "auto", transport=httpx.MockTransport(handler))
    assert await llm.complete_json("sys", "u") == {"ok": True}


async def test_chat_completions_relay_strips_think_and_switches_token_field():
    calls = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        calls.append((str(req.url), sorted(k for k in body if "tokens" in k)))
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens'. "
                                                                  "Use 'max_completion_tokens' instead."}})
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "<think>先分析一下</think>\n{\"plan\": 1}"}}]})

    llm = RightAPILLM("https://api.example.com/v1", "k", "deepseek-v4-pro", "auto",
                      transport=httpx.MockTransport(handler))
    assert await llm.complete_json("s", "u") == {"plan": 1}
    assert calls == [("https://api.example.com/v1/chat/completions", ["max_tokens"]),
                     ("https://api.example.com/v1/chat/completions", ["max_completion_tokens"])]


async def test_model_outside_bound_channel_gives_actionable_error():
    """Key 绑定 Gemini 渠道却还用着 Claude 模型：两种协议都报 model not found，提示去获取可用模型。"""
    paths = []

    def handler(req: httpx.Request):
        paths.append(req.url.path)
        return httpx.Response(404, json={"error": "model not found"})

    llm = RightAPILLM("https://www.rightapi.ai", "k", "claude-opus-5-5", "auto", transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="获取可用模型") as e:
        await llm.complete("s", "u")
    assert paths == ["/v1/messages", "/v1/chat/completions"] and "claude-opus-5-5" in str(e.value)


async def test_server_error_does_not_retry_other_protocol():
    paths = []

    def handler(req: httpx.Request):
        paths.append(req.url.path)
        return httpx.Response(502, text="<html><title>502 Bad Gateway</title></html>")

    llm = RightAPILLM("https://x.ai", "k", "claude-opus-5-5", "auto", transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="502") as e:
        await llm.complete("s", "u")
    assert paths == ["/v1/messages"] and "<html" not in str(e.value)


def test_settings_llm_relay(tmp_path, monkeypatch):
    # save_env 直接写 os.environ：先用 setenv 登记，测试结束后全部还原，不影响其他测试
    for k in EDITABLE_ENV_KEYS:
        monkeypatch.setenv(k, "")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    assert Settings().llm_api_base == "https://www.rightapi.ai"
    env = tmp_path / ".env"
    s = save_env({"RIGHTAPI_LLM_KEY": "sk-relay", "RIGHTAPI_LLM_BASE_URL": "api.example.com/v1/chat/completions",
                  "RIGHTAPI_LLM_MODEL": "deepseek-v4-pro", "RIGHTAPI_LLM_API_FORMAT": "gemini"}, env)
    assert (s.llm_api_base, s.llm_model) == ("https://api.example.com/v1", "deepseek-v4-pro")
    assert s.llm_api_format == "gemini"
    # 画图仍走 RightAPI，不受 LLM 中转影响
    assert s.rightapi_base_url == "https://www.rightapi.ai"
    rt = EngineRuntime(settings=s, demo_delay=0)
    assert rt.llm().base_url == "https://api.example.com/v1" and rt.llm().model == "deepseek-v4-pro"
    # 未知协议退回 auto；模型留空退回默认
    s = save_env({"RIGHTAPI_LLM_API_FORMAT": "bogus", "RIGHTAPI_LLM_MODEL": " ", "RIGHTAPI_LLM_BASE_URL": ""}, env)
    assert (s.llm_api_format, s.llm_model, s.llm_api_base) == ("auto", "claude-opus-5-5", "https://www.rightapi.ai")
    assert load_settings(env, override=True).llm_key == "sk-relay"


async def test_diagnose_lists_models_when_model_not_in_channel():
    def handler(req: httpx.Request):
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemini-3.8-flash"}, {"id": "gemini-4-argon"}]})
        return httpx.Response(404, json={"error": "model not found"})

    s = Settings(llm_key="LLM-KEY-1234567", llm_model="claude-opus-5-5")
    report = await check_llm(s, httpx.MockTransport(handler))
    assert report.startswith("❌") and "gemini-3.8-flash" in report and "获取可用模型" in report
    assert "LLM-KEY-1234567" not in report

    ok = await check_llm(Settings(llm_key="LLM-KEY-1234567", llm_model="deepseek-v4-pro",
                                  llm_base_url="https://api.example.com/v1"),
                         httpx.MockTransport(lambda r: httpx.Response(200, json={
                             "choices": [{"message": {"content": "OK"}}]})))
    assert ok.startswith("🟢") and "api.example.com" in ok and "/chat/completions" in ok


def test_provider_presets():
    """参考 Cline 的 API Provider：每个提供商有预设地址与默认协议，未知值退回 RightAPI（旧版 .env 行为不变）。"""
    assert get_provider(None).id == RIGHTAPI and get_provider("bogus").id == RIGHTAPI
    ids = [p.id for p in PROVIDERS]
    assert len(ids) == len(set(ids))
    for p in PROVIDERS:
        assert p.protocol in API_FORMATS and not p.base_url.endswith("/") and p.hint
    assert get_provider("rightapi").key_env == "RIGHTAPI_LLM_KEY"
    assert get_provider("deepseek").key_env == "LLM_KEY_DEEPSEEK"
    # 地址优先级：自定义 Base URL > 提供商预设 > RIGHTAPI_BASE_URL（只有 RightAPI 用）；OpenAI 兼容没填为空
    assert resolve_base_url("deepseek", "", "https://www.rightapi.ai") == "https://api.deepseek.com/v1"
    assert resolve_base_url("rightapi", "", "https://www.rightapi.ai") == "https://www.rightapi.ai"
    assert resolve_base_url("custom", "", "https://www.rightapi.ai") == ""
    assert resolve_base_url("deepseek", "https://relay.example.com/v1", "x") == "https://relay.example.com/v1"
    assert api_root(get_provider("anthropic").base_url, "anthropic") == "https://api.anthropic.com/v1"
    assert api_root(get_provider("gemini").base_url, "gemini") == "https://generativelanguage.googleapis.com/v1beta"
    assert api_root(get_provider("zhipu").base_url, "openai") == "https://open.bigmodel.cn/api/paas/v4"


async def test_anthropic_official_uses_x_api_key_only():
    """Anthropic 官方只认 x-api-key（Bearer 是 OAuth 令牌）；中转站多数只认 Bearer，两个都带。"""
    seen = {}

    def handler(req: httpx.Request):
        seen[req.url.host] = (req.headers.get("x-api-key"), req.headers.get("authorization"))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}]})

    for base in ("https://api.anthropic.com", "https://www.rightapi.ai"):
        await RightAPILLM(base, "k", "claude-opus-5-5", "anthropic",
                          transport=httpx.MockTransport(handler)).complete("s", "u")
    assert seen["api.anthropic.com"] == ("k", None)
    assert seen["www.rightapi.ai"] == ("k", "Bearer k")


def test_switching_provider_keeps_each_key(tmp_path, monkeypatch):
    for k in EDITABLE_ENV_KEYS:
        monkeypatch.setenv(k, "")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    env = tmp_path / ".env"
    # 旧版 .env：没有 RIGHTAPI_LLM_PROVIDER → RightAPI，Key 取 RIGHTAPI_LLM_KEY
    s = save_env({"RIGHTAPI_LLM_KEY": "sk-right"}, env)
    assert (s.llm_provider, s.llm_key, s.llm_api_base) == ("rightapi", "sk-right", "https://www.rightapi.ai")

    # 切到 DeepSeek：地址用预设，Key 单独保存，RightAPI 的 Key 还在
    s = save_env(llm_env_updates("deepseek", "sk-ds", s.llm_keys, False, "", "deepseek-v4-pro", "openai",
                                 s.llm_keys), env)
    assert (s.llm_provider, s.llm_key, s.llm_keys["rightapi"]) == ("deepseek", "sk-ds", "sk-right")
    assert s.llm_api_base == "https://api.deepseek.com/v1" and s.channel_live("llm")
    text = env.read_text(encoding="utf-8")
    assert "LLM_KEY_DEEPSEEK=sk-ds" in text and "LLM_KEY_MOONSHOT" not in text  # 没用过的提供商不写空行

    # 勾选自定义 Base URL：覆盖预设（误粘的接口路径去掉）；取消勾选：回到预设
    s = save_env(llm_env_updates("deepseek", "sk-ds", s.llm_keys, True, "relay.example.com/v1/chat/completions",
                                 "deepseek-v4-pro", "auto", s.llm_keys), env)
    assert s.llm_api_base == "https://relay.example.com/v1"
    s = save_env(llm_env_updates("deepseek", "sk-ds", s.llm_keys, False, "relay.example.com/v1",
                                 "deepseek-v4-pro", "auto", s.llm_keys), env)
    assert s.llm_api_base == "https://api.deepseek.com/v1"

    # OpenAI 兼容没填地址、或者没选模型：不走真实接口
    s = save_env(llm_env_updates("custom", "sk-c", s.llm_keys, False, "", "m", "auto", s.llm_keys), env)
    assert s.llm_api_base == "" and not s.channel_live("llm")
    s = save_env(llm_env_updates("deepseek", "sk-ds", s.llm_keys, False, "", "", "auto", s.llm_keys), env)
    assert s.llm_model == "" and not s.channel_live("llm")

    # 切回 RightAPI：原来的 Key 原样还在
    s = save_env(llm_env_updates("rightapi", s.llm_keys["rightapi"], s.llm_keys, False, "", "claude-sonnet-5-5",
                                 "auto", s.llm_keys), env)
    assert (s.llm_key, s.llm_keys["deepseek"], s.llm_model) == ("sk-right", "sk-ds", "claude-sonnet-5-5")


def test_public_demo_drops_every_provider_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LAOTUO_PUBLIC_DEMO", "1")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("RIGHTAPI_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_KEY_DEEPSEEK", "sk-secret-deepseek")
    s = load_settings(tmp_path / "missing.env")
    assert s.llm_key == "" and s.llm_keys == {} and not s.channel_live("llm")


def test_panel_view_when_switching_provider():
    """界面切换提供商：Key 换成该提供商保存过的；地址、模型、协议换成预设；切回已保存的提供商时恢复原值。"""
    s = Settings(llm_provider="rightapi", llm_key="sk-r", llm_keys={"rightapi": "sk-r", "deepseek": "sk-d"},
                 llm_model="claude-sonnet-5-5", llm_api_format="anthropic")
    key, use_custom, base, model, fmt, hint = _llm_view("deepseek", s.llm_keys, s)
    assert key["value"] == "sk-d" and model["value"] is None and fmt["value"] == "openai"
    assert use_custom["visible"] is True and base["visible"] is False and "api.deepseek.com" in hint

    _, use_custom, base, _, _, _ = _llm_view("custom", {}, s)
    assert use_custom["visible"] is False and base["visible"] is True and "必填" in base["label"]

    key, _, _, model, fmt, _ = _llm_view("rightapi", s.llm_keys, s)
    assert (key["value"], model["value"], fmt["value"]) == ("sk-r", "claude-sonnet-5-5", "anthropic")
