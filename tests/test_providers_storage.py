import base64

import pytest

from app.engine.image_router import family_for_model, image_key, route_image_request
from app.providers.llm import LLMError, extract_json
from app.providers.video import (
    FalAIVideoProvider,
    ManualActionRequired,
    ManualComfyUIBridgeProvider,
    ModalMiniMaxH3Provider,
    VideoProviderError,
    VideoRequest,
    available_providers,
)
from app.schemas import Project, mode_for_reference_count
from app.settings import Settings, normalize_base_url
from app.storage.project_store import ProjectStore

from .test_plan_workflow import make_draft

S = Settings(image_key="draw-key")


@pytest.fixture
def ref_png(tmp_path):
    p = tmp_path / "pkg.png"
    p.write_bytes(bytes([0x89]) + b"PNG fake")
    return p


def test_default_base_url_and_normalization():
    assert Settings().rightapi_base_url == "https://rightapi.ai"
    assert normalize_base_url(None) == "https://rightapi.ai"
    assert normalize_base_url("rightapi.ai/") == "https://rightapi.ai"
    assert normalize_base_url("https://rightapi.ai/v1/") == "https://rightapi.ai"
    assert normalize_base_url(" https://rightapi.ai/v1beta ") == "https://rightapi.ai"


def test_openai_routes_by_actual_reference_count(ref_png):
    r0 = route_image_request(S, "gpt-image-2", "p", "1:1", [])
    assert r0.url == "https://rightapi.ai/v1/images/generations" and r0.json["size"] == "1024x1024"
    assert r0.json["quality"] == "high"  # 默认 2K
    r2 = route_image_request(S, "gpt-image-2.5", "p", "3:4", [ref_png, ref_png], n=3, resolution="1K")
    assert r2.url.endswith("/v1/images/edits") and len(r2.files) == 2 and r2.data["n"] == "3"
    assert r2.data["quality"] == "medium" and r2.fallback is None


def test_nano_banana_uses_generate_content_with_inline_data(ref_png):
    r0 = route_image_request(S, "nano-banana-pro", "p", "9:16", [], resolution="4K")
    assert r0.url == "https://rightapi.ai/v1beta/models/nano-banana-pro:generateContent"
    assert "streamGenerateContent" not in r0.url
    assert r0.json["contents"][0]["parts"] == [{"text": "p"}]
    assert r0.json["generationConfig"]["imageConfig"] == {"aspectRatio": "9:16", "imageSize": "4K"}
    assert "async" not in r0.json
    # 自带 OpenAI 兼容回退请求：0 张 -> generations
    assert r0.fallback.url.endswith("/v1/images/generations") and r0.fallback.json["model"] == "nano-banana-pro"

    s_async = Settings(image_key="draw-key", gemini_async=True)
    r1 = route_image_request(s_async, "nano-banana-2-lite", "p", "1:1", [ref_png])
    parts = r1.json["contents"][0]["parts"]
    assert base64.b64decode(parts[0]["inline_data"]["data"]) == ref_png.read_bytes()
    assert parts[-1] == {"text": "p"} and r1.json["async"] is True
    assert r1.fallback.url.endswith("/v1/images/edits")


def test_single_image_key_shared_by_both_families(monkeypatch):
    assert family_for_model("gpt-image-2.5") == "openai"
    assert family_for_model("nano-banana-2") == "gemini"
    assert family_for_model("gemini-3-pro-image-preview") == "gemini"
    with pytest.raises(ValueError):
        family_for_model("dall-e-3")
    assert image_key(S) == "draw-key"
    with pytest.raises(RuntimeError, match="RIGHTAPI_IMAGE_KEY"):
        image_key(Settings())
    assert "draw-key" not in repr(S)
    # 两种模型共用同一个通道开关
    assert S.channel_live("image") and not Settings().channel_live("image")
    assert S.image_models == ["gpt-image-2.5", "gpt-image-2", "nano-banana-pro", "nano-banana-2", "nano-banana-2-lite"]


def test_legacy_env_names_fallback(monkeypatch, tmp_path):
    from app.settings import load_settings
    for k in ("RIGHTAPI_IMAGE_KEY", "RIGHTAPI_OPENAI_IMAGE_KEY", "RIGHTAPI_GEMINI_IMAGE_KEY", "RIGHTAPI_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RIGHTAPI_GEMINI_IMAGE_KEY", "old-gemini")
    assert load_settings(tmp_path / "none.env").image_key == "old-gemini"
    monkeypatch.setenv("RIGHTAPI_IMAGE_KEY", "new-draw")
    assert load_settings(tmp_path / "none.env").image_key == "new-draw"


def test_too_many_references_rejected(ref_png):
    with pytest.raises(ValueError):
        route_image_request(S, "gpt-image-2", "p", "1:1", [ref_png] * 4)
    with pytest.raises(ValueError):
        mode_for_reference_count(4)
    assert mode_for_reference_count(2) == "multi_image_fusion"


def test_extract_json():
    assert extract_json('说明\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(LLMError):
        extract_json("no json")


def test_video_providers_registered():
    assert set(available_providers()) >= {"modal_minimax_h3", "manual_comfyui", "fal_ai"}
    assert ModalMiniMaxH3Provider().timeout_seconds == 600
    assert not ModalMiniMaxH3Provider().is_configured()


async def test_warmup_and_manual_bridge(tmp_path):
    warm = await ModalMiniMaxH3Provider(url="https://x.modal.run").warmup()
    assert warm.ready

    req = VideoRequest(shot_id="shot_1", prompt="p", first_frame_path=tmp_path / "f.png",
                       aspect_ratio="9:16", duration_seconds=3, output_dir=tmp_path)
    gen = ManualComfyUIBridgeProvider(comfyui_url="https://comfy.modal.run").generate(req)
    first = await anext(gen)
    assert "comfy.modal.run" in first.message and "9:16" in first.message
    with pytest.raises(ManualActionRequired):
        await anext(gen)
    with pytest.raises(VideoProviderError):
        await anext(FalAIVideoProvider().generate(req))


def test_project_store_roundtrip(tmp_path, ref_png):
    store = ProjectStore(tmp_path)
    proj = Project(name="莓茶中秋", category_id="zhangjiajie_berry_tea", platform_id="taobao",
                   main_image_plan=make_draft())
    proj.main_image_plan.confirm_plan()
    store.save(proj)
    copied = store.import_file(proj.project_id, ref_png, "references")
    assert copied.exists()

    loaded = store.load(proj.project_id)
    assert loaded.main_image_plan.plan_status == "confirmed"
    assert loaded.main_image_plan.user_creative_idea == "中秋主题，暖金色调"
    assert [c.role for c in loaded.main_image_plan.reference_checklist] == ["product_package", "tea_soup"]
    assert [p.project_id for p in store.list_projects()] == [proj.project_id]

    with pytest.raises(ValueError):
        store.project_dir("../evil")
    with pytest.raises(ValueError):
        store.import_file(proj.project_id, ref_png, "candidates", sub_id="../x")
