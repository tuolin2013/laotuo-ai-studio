
import pytest

from app.engine.image_router import image_key, route_image_request
from app.providers.llm import LLMError, extract_json
from app.providers.video import (
    FalAIVideoProvider,
    ModalMiniMaxH3Provider,
    VideoProviderError,
    VideoRequest,
    available_providers,
)
from app.schemas import Project, mode_for_reference_count
from app.settings import Settings
from app.storage.project_store import ProjectStore

from .test_plan_workflow import make_draft

S = Settings(image_key="draw-key")


@pytest.fixture
def ref_png(tmp_path):
    p = tmp_path / "pkg.png"
    p.write_bytes(bytes([0x89]) + b"PNG fake")
    return p


def test_single_image_key_and_legacy_env_names(monkeypatch, tmp_path):
    assert image_key(S) == "draw-key"
    with pytest.raises(RuntimeError, match="RIGHTAPI_IMAGE_KEY"):
        image_key(Settings())
    assert "draw-key" not in repr(S)
    assert S.channel_live("image") and not Settings().channel_live("image")

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

    # 测试缺少逗号的修复
    missing_comma = """{
  "plan_summary": "测试",
  "slots": [
    {
      "title": "第一张"
    }
    {
      "title": "第二张"
    }
  ]
}"""
    result = extract_json(missing_comma)
    assert len(result["slots"]) == 2
    assert result["slots"][1]["title"] == "第二张"

    # 测试多余尾随逗号的修复
    trailing_comma = '{"items": [1, 2, 3,], "name": "test",}'
    result = extract_json(trailing_comma)
    assert result == {"items": [1, 2, 3], "name": "test"}

    with pytest.raises(LLMError):
        extract_json("no json")


def test_video_providers_registered():
    assert set(available_providers()) >= {"modal_minimax_h3", "demo", "fal_ai"}
    assert "manual_comfyui" not in available_providers()  # ComfyUI 人工桥接已移除
    prov = ModalMiniMaxH3Provider()
    assert prov.app_name == "minimax-h3-diffusers-service" and prov.class_name == "H3BatchRunner"
    assert prov.timeout_seconds == 2400


async def test_warmup_and_reserved_fal(tmp_path):
    warm = await ModalMiniMaxH3Provider(runner_factory=lambda wf: None).warmup()
    assert warm.ready

    req = VideoRequest(shot_id="shot_1", prompt="p", first_frame_path=tmp_path / "f.png",
                       aspect_ratio="9:16", duration_seconds=3, output_dir=tmp_path)
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
