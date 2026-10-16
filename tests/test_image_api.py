"""画图接口测试：严格对照 https://docs.rightapi.ai/docs/rc_draw/ 的协议、参数与异步流程。"""

import base64
import json
from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.engine.image_router import route_image_request
from app.providers.image import (
    ImageAPIError,
    RightAPIImageClient,
    encode_reference,
    extract_result_urls,
    gpt_image_size,
    gpt_quality_for,
)
from app.providers.image_models import CATALOG, HQ_MODELS, UnknownImageModel, protocol_of
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
    assert Settings().image_models == HQ_MODELS


@pytest.mark.parametrize(("model", "protocol"), [
    ("gpt-image-2.5-flare", "openai"), ("gpt-image-2.5-sunburst", "openai"), ("gpt-image-2", "openai"),
    ("gemini-3-pro-image-preview", "gemini"), ("gemini-nano-banana-2.1", "gemini"),
    # image 普通渠道
    ("gpt-image-2-vip", "openai"), ("gpt-image-2.5", "openai"),
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
    assert len(CATALOG) == 10


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
