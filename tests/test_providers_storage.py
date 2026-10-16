import base64

import pytest

from app.engine.image_router import family_for_model, key_for_family, route_image_request
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
from app.settings import Settings
from app.storage.project_store import ProjectStore

from .test_plan_workflow import make_draft

S = Settings(openai_image_key="k1", gemini_image_key="k2")


@pytest.fixture
def ref_png(tmp_path):
    p = tmp_path / "pkg.png"
    p.write_bytes(b"\x89PNG fake")
    return p


def test_openai_routes_by_actual_reference_count(ref_png):
    r0 = route_image_request(S, "gpt-image-2", "p", "1:1", [])
    assert r0.url == "https://www.rightapi.ai/v1/images/generations" and r0.json["size"] == "1024x1024"
    r2 = route_image_request(S, "gpt-image-2.5-flare", "p", "3:4", [ref_png, ref_png], n=3)
    assert r2.url.endswith("/v1/images/edits") and len(r2.files) == 2 and r2.data["n"] == "3"


def test_gemini_uses_generate_content_with_inline_data(ref_png):
    r0 = route_image_request(S, "gemini-nano-banana-2.1", "p", "9:16", [])
    assert r0.url.endswith("/v1beta/models/gemini-nano-banana-2.1:generateContent")
    assert "streamGenerateContent" not in r0.url
    assert r0.json["contents"][0]["parts"] == [{"text": "p"}]
    assert "async" not in r0.json

    s_async = Settings(gemini_image_key="k2", gemini_async=True)
    r1 = route_image_request(s_async, "gemini-3-pro-image-preview", "p", "1:1", [ref_png])
    parts = r1.json["contents"][0]["parts"]
    assert base64.b64decode(parts[0]["inline_data"]["data"]) == ref_png.read_bytes()
    assert parts[-1] == {"text": "p"} and r1.json["async"] is True


def test_keys_are_isolated_per_family():
    assert key_for_family(S, family_for_model("gpt-image-2")) == "k1"
    assert key_for_family(S, family_for_model("gemini-3-pro-image-preview")) == "k2"
    with pytest.raises(RuntimeError, match="RIGHTAPI_GEMINI_IMAGE_KEY"):
        key_for_family(Settings(openai_image_key="k1"), "gemini")
    assert "k1" not in repr(S)


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
