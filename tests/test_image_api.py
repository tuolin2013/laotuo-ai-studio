"""画图接口测试：严格对照 https://docs.rightapi.ai/docs/rc_draw/ 的协议、参数与异步流程。"""

import base64
import json
from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.engine.image_router import image_key, route_image_request
from app.providers.image import (
    ImageAPIError,
    RightAPIImageClient,
    UpstreamBusyError,
    encode_reference,
    extract_result_urls,
    gpt_image_size,
    gpt_quality_for,
    is_busy_error,
)
from app.providers.image_models import (
    CATALOG,
    HQ_MODELS,
    STANDARD_MODELS,
    UnknownImageModel,
    choices,
    live_channel_of,
    protocol_of,
)
from app.settings import Settings, normalize_base_url

S = Settings(image_key="sk-draw")
BASE = "https://www.rightapi.ai"


def _png(tmp_path, name="ref.png", size=(64, 64)):
    p = tmp_path / name
    Image.new("RGB", size, (200, 30, 30)).save(p)
    return p


def _png_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- 模型目录：模型决定协议


def test_hq_channel_is_production_default():
    assert HQ_MODELS == ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst", "gpt-image-2",
                         "gemini-3-pro-image-preview", "gemini-nano-banana-2.1"]
    assert STANDARD_MODELS == ["gpt-image-2@image", "gpt-image-2-vip", "gpt-image-2.5",
                               "nano-banana-pro", "nano-banana-2", "nano-banana-2-lite"]
    # 两个渠道都可选，高质量渠道排在前面（下拉框默认选第一个）
    assert Settings().image_models == [*HQ_MODELS, *STANDARD_MODELS]
    assert Settings().image_models[0] == "gpt-image-2.5-flare"


@pytest.mark.parametrize(("model", "protocol"), [
    ("gpt-image-2.5-flare", "openai"), ("gpt-image-2.5-sunburst", "openai"), ("gpt-image-2", "openai"),
    ("gemini-3-pro-image-preview", "gemini"), ("gemini-nano-banana-2.1", "gemini"),
    # image 普通渠道
    ("gpt-image-2@image", "openai"), ("gpt-image-2-vip", "openai"), ("gpt-image-2.5", "openai"),
    ("nano-banana-pro", "gemini"), ("nano-banana-2", "gemini"), ("nano-banana-2-lite", "gemini"),
])
def test_every_listed_model_has_explicit_protocol(model, protocol):
    assert protocol_of(model) == protocol


def test_unknown_model_is_rejected_not_guessed():
    for bad in ("gemini-2.0-flash", "gpt-image-3", "dall-e-3", "nano-banana-9"):
        with pytest.raises(UnknownImageModel):
            protocol_of(bad)
    with pytest.raises(UnknownImageModel):
        route_image_request(S, "gpt-image-3", "p", "1:1", [])
    assert len(CATALOG) == 11


def test_base_url_follows_docs():
    assert Settings().rightapi_base_url == BASE
    assert normalize_base_url(None) == BASE
    assert normalize_base_url("rightapi.ai") == BASE
    assert normalize_base_url("https://rightapi.ai/v1/") == BASE
    assert normalize_base_url("https://www.rightapi.ai/v1beta") == BASE


# ---------------------------------------------------------------- OpenAI 生成


@pytest.mark.parametrize("model", ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst", "gpt-image-2"])
def test_gpt_text_to_image_uses_generations(model):
    r = route_image_request(S, model, "海报", "1:1", [], resolution="2K", quality="xhigh")
    assert r.url == f"{BASE}/v1/images/generations"
    assert r.json["model"] == model and r.json["async"] is True and r.json["n"] == 1
    assert r.json["size"] == "2048x2048" and r.json["output_format"] == "png"
    # xhigh 只能用于 2.5 模型
    assert r.json["quality"] == ("high" if model == "gpt-image-2" else "xhigh")
    assert "images" not in r.json and "response_format" not in r.json


def test_gpt_with_references_uses_edits_with_data_urls(tmp_path):
    a, b = _png(tmp_path, "a.png"), _png(tmp_path, "b.png")
    r = route_image_request(S, "gpt-image-2.5-sunburst", "把第一张图的商品放进第二张图", "3:4", [a, b])
    assert r.url == f"{BASE}/v1/images/edits"
    assert r.json["async"] is True and len(r.json["images"]) == 2
    assert all(x.startswith("data:image/png;base64,") for x in r.json["images"])
    assert base64.b64decode(r.json["images"][0].split(",", 1)[1]) == a.read_bytes()


@pytest.mark.parametrize(("ratio", "tier"), [(r, t) for r in ["1:1", "3:4", "4:3", "2:3", "3:2", "9:16", "16:9"]
                                             for t in ["1K", "2K", "4K"]])
def test_gpt_sizes_obey_documented_rules(ratio, tier):
    w, h = (int(x) for x in gpt_image_size(ratio, tier).split("x"))
    assert w % 16 == 0 and h % 16 == 0
    assert max(w, h) <= 3840
    assert 655_360 <= w * h <= 8_294_400
    assert max(w, h) / min(w, h) <= 3
    rw, rh = (int(x) for x in ratio.split(":"))
    assert abs(w / h - rw / rh) < 0.02  # 比例保持


def test_gpt_4k_sizes():
    assert gpt_image_size("16:9", "4K") == "3840x2160"
    assert gpt_image_size("9:16", "4K") == "2160x3840"
    assert gpt_image_size("1:1", "1K") == "1024x1024"
    assert gpt_quality_for("gpt-image-2", "max") == "high"
    assert gpt_quality_for("gpt-image-2.5-flare", "max") == "max"
    assert gpt_quality_for("gpt-image-2.5-flare", "bogus") == "high"


# ---------------------------------------------------------------- Gemini 生成


@pytest.mark.parametrize("model", ["gemini-3-pro-image-preview", "gemini-nano-banana-2.1"])
def test_gemini_text_to_image(model):
    r = route_image_request(S, model, "未来城市", "16:9", [], resolution="4K")
    assert r.url == f"{BASE}/v1beta/models/{model}:generateContent"
    assert "streamGenerateContent" not in r.url
    assert r.json["async"] is True
    assert r.json["contents"] == [{"role": "user", "parts": [{"text": "未来城市"}]}]
    assert r.json["generationConfig"] == {"imageConfig": {"aspectRatio": "16:9", "imageSize": "4K"}}
    # Gemini 协议没有 model / size / quality 字段
    assert not {"model", "size", "quality", "images"} & set(r.json)


def test_gemini_references_go_into_same_endpoint_as_inline_data(tmp_path):
    a, b = _png(tmp_path, "a.png"), _png(tmp_path, "b.png")
    r = route_image_request(S, "gemini-3-pro-image-preview", "把第二张图的帽子戴到第一张图人物头上", "3:4", [a, b],
                            resolution="2k")
    assert r.url.endswith("/v1beta/models/gemini-3-pro-image-preview:generateContent")
    parts = r.json["contents"][0]["parts"]
    assert parts[0] == {"text": "把第二张图的帽子戴到第一张图人物头上"}
    assert [p["inline_data"]["mime_type"] for p in parts[1:]] == ["image/png", "image/png"]
    # 纯 base64，不带 data: 前缀
    assert not parts[1]["inline_data"]["data"].startswith("data:")
    assert base64.b64decode(parts[2]["inline_data"]["data"]) == b.read_bytes()
    assert r.json["generationConfig"]["imageConfig"]["imageSize"] == "2K"  # K 必须大写


def test_large_reference_is_compressed(tmp_path):
    big = _png(tmp_path, "big.png", size=(4000, 3000))
    mime, b64 = encode_reference(big)
    with Image.open(BytesIO(base64.b64decode(b64))) as im:
        assert max(im.size) <= 2048
    assert mime in {"image/png", "image/jpeg"}


# ---------------------------------------------------------------- 异步流程与结果解析


def test_extract_result_urls_shapes():
    assert extract_result_urls({"task_id": "t", "status": "in_progress"}) is None
    assert extract_result_urls({"created": 1, "data": [{"url": "https://c/x.png"}]}) == ["https://c/x.png"]
    gem = {"candidates": [{"content": {"role": "model", "parts": [{"text": "https://c/y.png"}]},
                           "finishReason": "STOP"}]}
    assert extract_result_urls(gem) == ["https://c/y.png"]


def _async_handler(protocol: str, log: list, fail: bool = False, submit_fail: bool = False):
    png = _png_bytes()
    polls = {"n": 0}

    def handler(req: httpx.Request):
        log.append((req.method, str(req.url), req.headers.get("authorization")))
        if req.url.host == "cdn.example.com":
            assert "authorization" not in req.headers  # 下载 CDN 不携带 Key
            return httpx.Response(200, content=png)
        if req.method == "POST":
            assert json.loads(req.content)["async"] is True
            if submit_fail:
                return httpx.Response(200, json={"task_id": "task_x", "status": "failed",
                                                 "error": "上游 HTTP 404: 404 page not found"})
            return httpx.Response(200, json={"task_id": "task_abc", "status": "processing", "progress": 0})
        assert req.url.path == "/v1/tasks/task_abc"
        polls["n"] += 1
        if polls["n"] < 3:
            return httpx.Response(200, json={"task_id": "task_abc", "status": "in_progress",
                                             "progress": 30 * polls["n"]})
        if fail:
            return httpx.Response(200, json={"task_id": "task_abc", "status": "failed",
                                             "error": {"message": "上游生成失败", "code": ""}})
        if protocol == "openai":
            return httpx.Response(200, json={"created": 1, "data": [{"url": "https://cdn.example.com/r.png"}]})
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [
            {"text": "https://cdn.example.com/r.png"}]}, "finishReason": "STOP"}]})

    return handler, png


@pytest.mark.parametrize("model", HQ_MODELS)
async def test_async_submit_poll_download_for_every_hq_model(model, tmp_path):
    log, events = [], []
    proto = protocol_of(model)
    handler, png = _async_handler(proto, log)
    client = RightAPIImageClient(base_url=BASE, poll_interval=0, transport=httpx.MockTransport(handler))
    refs = [_png(tmp_path)] if model.endswith("sunburst") else []

    async def on_event(msg):
        events.append(msg)

    req = route_image_request(S, model, "p", "1:1", refs, resolution="4K")
    assert await client.send(req, "sk-draw", on_event=on_event) == [png]
    submit = log[0][1]
    if proto == "openai":
        assert submit == f"{BASE}/v1/images/{'edits' if refs else 'generations'}"
    else:
        assert submit == f"{BASE}/v1beta/models/{model}:generateContent"
    assert log[1][1] == f"{BASE}/v1/tasks/task_abc"
    assert all(a == "Bearer sk-draw" for _, u, a in log if "rightapi" in u)
    assert any("进度 60%" in e for e in events)


async def test_task_failed_and_submit_failed():
    log = []
    handler, _ = _async_handler("openai", log, fail=True)
    client = RightAPIImageClient(base_url=BASE, poll_interval=0, transport=httpx.MockTransport(handler))
    req = route_image_request(S, "gpt-image-2", "p", "1:1", [])
    with pytest.raises(ImageAPIError, match="上游生成失败"):
        await client.send(req, "sk-draw")

    handler, _ = _async_handler("openai", [], submit_fail=True)
    client = RightAPIImageClient(base_url=BASE, poll_interval=0, transport=httpx.MockTransport(handler))
    with pytest.raises(ImageAPIError, match=r"提交即失败.*404"):
        await client.send(req, "sk-draw")


async def test_http_errors_and_timeout():
    client = RightAPIImageClient(base_url=BASE, poll_interval=0, transport=httpx.MockTransport(
        lambda r: httpx.Response(403, json={"error": {"message": "分组未开通该模型"}})))
    req = route_image_request(S, "gemini-3-pro-image-preview", "p", "1:1", [])
    with pytest.raises(ImageAPIError, match=r"403.*分组未开通"):
        await client.send(req, "sk-draw")

    def never_done(r):
        if r.method == "POST":
            return httpx.Response(200, json={"task_id": "task_abc", "status": "processing"})
        return httpx.Response(200, json={"status": "queued"})

    slow = RightAPIImageClient(base_url=BASE, timeout=0.05, poll_interval=0.01,
                               transport=httpx.MockTransport(never_done))
    with pytest.raises(ImageAPIError, match="未完成"):
        await slow.send(req, "sk-draw")
    with pytest.raises(ImageAPIError, match="RIGHTAPI_IMAGE_KEY"):
        await slow.send(req, "")


async def test_connect_errors_are_retried_without_duplicate_submit():
    """本机代理偶发断连（请求没发出去）会自动重试；服务端已收到的请求不会重复提交。"""
    attempts = {"post": 0}

    def flaky(req: httpx.Request):
        if req.method == "POST":
            attempts["post"] += 1
            if attempts["post"] < 3:
                raise httpx.ConnectError("proxy reset", request=req)
            return httpx.Response(200, json={"created": 1, "data": [{"url": "https://cdn.example.com/a.png"}]})
        return httpx.Response(200, content=_png_bytes())

    client = RightAPIImageClient(base_url=BASE, poll_interval=0, transport=httpx.MockTransport(flaky))
    req = route_image_request(S, "gpt-image-2", "p", "1:1", [])
    assert await client.send(req, "sk-draw") == [_png_bytes()]
    assert attempts["post"] == 3

    attempts["post"] = 0

    def server_error(req: httpx.Request):
        attempts["post"] += 1
        return httpx.Response(500, json={"error": "暂无可用上游"})

    client = RightAPIImageClient(base_url=BASE, poll_interval=0, transport=httpx.MockTransport(server_error))
    with pytest.raises(ImageAPIError, match="500"):
        await client.send(req, "sk-draw")
    assert attempts["post"] == 1  # 服务端已响应的错误不重试，避免重复扣费


# ---------------------------------------------------------------- 上游繁忙：自动重新提交

BUSY_MSG = "Upstream resources are temporarily busy. Please try again later."


def test_is_busy_error():
    assert is_busy_error(BUSY_MSG)
    assert is_busy_error("上游繁忙，请稍后重试")
    assert not is_busy_error("上游生成失败")
    assert not is_busy_error("暂无可用上游")
    assert not is_busy_error("content policy violation")


def _busy_then_ok(busy_times: int, where: str = "poll"):
    """前 busy_times 次任务因上游繁忙失败，之后成功出图。where=poll 在轮询时失败，submit 在提交时失败。"""
    state = {"post": 0}

    def handler(req: httpx.Request):
        if req.method == "POST":
            state["post"] += 1
            n = state["post"]
            if where == "submit" and n <= busy_times:
                return httpx.Response(200, json={"task_id": f"t{n}", "status": "failed", "error": BUSY_MSG})
            return httpx.Response(200, json={"task_id": f"t{n}", "status": "processing", "progress": 0})
        if req.url.path.startswith("/v1/tasks/"):
            n = int(req.url.path.rsplit("t", 1)[1])
            if where == "poll" and n <= busy_times:
                return httpx.Response(200, json={"task_id": f"t{n}", "status": "failed",
                                                 "error": {"message": BUSY_MSG}})
            return httpx.Response(200, json={"created": 1, "data": [{"url": "https://cdn.example.com/r.png"}]})
        return httpx.Response(200, content=_png_bytes())

    return handler, state


@pytest.mark.parametrize("where", ["poll", "submit"])
async def test_busy_failure_is_resubmitted_until_success(where):
    handler, state = _busy_then_ok(2, where)
    events = []

    async def on_event(msg):
        events.append(msg)

    client = RightAPIImageClient(base_url=BASE, poll_interval=0, busy_backoff=0,
                                 transport=httpx.MockTransport(handler))
    req = route_image_request(S, "gpt-image-2.5-sunburst", "p", "1:1", [])
    assert await client.send(req, "sk-draw", on_event=on_event) == [_png_bytes()]
    assert state["post"] == 3
    retries = [e for e in events if "上游繁忙" in e]
    assert len(retries) == 2 and "第 1/3 次重试" in retries[0] and "第 2/3 次重试" in retries[1]


async def test_busy_gives_up_after_retry_limit():
    handler, state = _busy_then_ok(99)
    client = RightAPIImageClient(base_url=BASE, poll_interval=0, busy_retries=2, busy_backoff=0,
                                 transport=httpx.MockTransport(handler))
    req = route_image_request(S, "gpt-image-2", "p", "1:1", [])
    with pytest.raises(UpstreamBusyError, match=r"temporarily busy.*已自动重试 2 次"):
        await client.send(req, "sk-draw")
    assert state["post"] == 3  # 1 次原始提交 + 2 次重试

    state["post"] = 0
    no_retry = RightAPIImageClient(base_url=BASE, poll_interval=0, busy_retries=0,
                                   transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamBusyError):
        await no_retry.send(req, "sk-draw")
    assert state["post"] == 1


@pytest.mark.parametrize("status", [429, 503])
async def test_busy_http_status_on_submit_is_retried(status):
    state = {"post": 0}

    def handler(req: httpx.Request):
        if req.method == "POST":
            state["post"] += 1
            if state["post"] == 1:
                return httpx.Response(status, json={"error": {"message": "slow down"}})
            return httpx.Response(200, json={"created": 1, "data": [{"url": "https://cdn.example.com/r.png"}]})
        return httpx.Response(200, content=_png_bytes())

    client = RightAPIImageClient(base_url=BASE, poll_interval=0, busy_backoff=0,
                                 transport=httpx.MockTransport(handler))
    req = route_image_request(S, "gpt-image-2", "p", "1:1", [])
    assert await client.send(req, "sk-draw") == [_png_bytes()]
    assert state["post"] == 2


async def test_non_busy_task_failure_is_not_retried():
    log = []
    handler, _ = _async_handler("openai", log, fail=True)
    client = RightAPIImageClient(base_url=BASE, poll_interval=0, busy_backoff=0,
                                 transport=httpx.MockTransport(handler))
    req = route_image_request(S, "gpt-image-2", "p", "1:1", [])
    with pytest.raises(ImageAPIError, match="上游生成失败") as exc:
        await client.send(req, "sk-draw")
    assert not isinstance(exc.value, UpstreamBusyError)
    assert sum(1 for m, *_ in log if m == "POST") == 1


# ---------------------------------------------------------------- 两个渠道：各用各的 Key / Base URL

S2 = Settings(image_key="sk-hq", image_std_key="sk-std")


def test_channel_of_each_model():
    for m in HQ_MODELS:
        assert live_channel_of(m) == "image"
    for m in STANDARD_MODELS:
        assert live_channel_of(m) == "image_std"
    labels = dict((v, k) for k, v in choices([*HQ_MODELS, *STANDARD_MODELS]))
    assert labels["gpt-image-2"].startswith("【高质量】gpt-image-2｜")
    assert labels["gpt-image-2@image"].startswith("【普通】gpt-image-2｜")  # 显示真实模型名，不显示 @image


def test_key_follows_model_channel():
    assert image_key(S2, "gpt-image-2.5-flare") == "sk-hq"
    assert image_key(S2, "gemini-3-pro-image-preview") == "sk-hq"
    assert image_key(S2, "gpt-image-2@image") == "sk-std"
    assert image_key(S2, "nano-banana-2-lite") == "sk-std"
    # 只配了高质量 Key：普通渠道模型报错提示填写，不会拿高质量 Key 去调用
    with pytest.raises(RuntimeError, match="RIGHTAPI_IMAGE_STD_KEY"):
        image_key(Settings(image_key="sk-hq"), "nano-banana-pro")
    assert Settings(image_key="sk-hq").channel_live("image")
    assert not Settings(image_key="sk-hq").channel_live("image_std")
    assert Settings(image_std_key="sk-std").channel_live("image_std")


def test_std_channel_sends_real_model_name_and_base_url():
    s = Settings(image_std_key="sk-std", image_std_base_url="https://std.example.com")
    r = route_image_request(s, "gpt-image-2@image", "p", "1:1", [])
    assert r.url == "https://std.example.com/v1/images/generations"
    assert r.json["model"] == "gpt-image-2"  # 请求里不带 @image 后缀
    g = route_image_request(s, "nano-banana-2", "p", "16:9", [], resolution="2K")
    assert g.url == "https://std.example.com/v1beta/models/nano-banana-2:generateContent"
    assert g.json["generationConfig"]["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "2K"}
    # 高质量渠道仍用 RIGHTAPI_BASE_URL；普通渠道未单独配置时同样回退到它
    assert route_image_request(s, "gpt-image-2", "p", "1:1", []).url == f"{BASE}/v1/images/generations"
    assert route_image_request(S2, "nano-banana-pro", "p", "1:1", []).url.startswith(f"{BASE}/v1beta/")


def test_xhigh_only_for_flagged_models():
    assert gpt_quality_for("gpt-image-2.5", "max") == "high"  # 普通渠道 2.5 未标记支持 xhigh/max
    assert gpt_quality_for("gpt-image-2-vip", "xhigh") == "high"
    assert gpt_quality_for("gpt-image-2@image", "max") == "high"
    assert gpt_quality_for("gpt-image-2.5-sunburst", "xhigh") == "xhigh"
    assert route_image_request(S2, "gpt-image-2.5", "p", "1:1", [], quality="max").json["quality"] == "high"


async def test_std_model_renders_with_std_key(tmp_path):
    """端到端：普通渠道模型用普通 Key + 普通 Base URL 提交、轮询，高质量 Key 不出现在请求里。"""
    from app.engine.render_engine import RenderEngine
    from app.engine.runtime import EngineRuntime
    from app.providers.demo.render import DemoArt

    seen = []

    def handler(req: httpx.Request):
        seen.append((req.url.host, req.url.path, req.headers.get("authorization")))
        if req.url.host == "cdn.example.com":
            return httpx.Response(200, content=_png_bytes())
        assert req.url.host == "std.example.com"
        assert req.headers["authorization"] == "Bearer sk-std"
        if req.method == "POST":
            assert json.loads(req.content)["model"] == "gpt-image-2"
            return httpx.Response(200, json={"task_id": "task_s", "status": "processing"})
        return httpx.Response(200, json={"created": 1, "data": [{"url": "https://cdn.example.com/s.png"}]})

    s = Settings(workspace_dir=tmp_path, image_key="sk-hq", image_std_key="sk-std",
                 image_std_base_url="https://std.example.com", image_poll_interval=0)
    rt = EngineRuntime(settings=s, demo_delay=0)
    real_client = rt.image_client

    def client_with_mock(base_url=None):
        c = real_client(base_url)
        c._transport = httpx.MockTransport(handler)
        return c

    rt.image_client = client_with_mock
    out = tmp_path / "x.png"
    events = []

    async def emit(msg):
        events.append(msg)

    path = await RenderEngine(rt)._generate_one(
        model="gpt-image-2@image", prompt="p", aspect_ratio="1:1", references=[], out_path=out,
        art=DemoArt(title="t", model="gpt-image-2@image"),
        resolution="1K", quality="high", emit=emit)
    assert path.read_bytes() == _png_bytes()
    assert ("std.example.com", "/v1/images/generations", "Bearer sk-std") in seen
    assert not any(a == "Bearer sk-hq" for *_, a in seen)
    assert RenderEngine(rt)._mode_label("nano-banana-2") == "image 普通渠道 · 真实 API"
    assert RenderEngine(rt)._mode_label("gpt-image-2") == "image 高质量渠道 · 真实 API"
