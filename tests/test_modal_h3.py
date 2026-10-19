"""MiniMax H3（Modal SDK 调用）测试：用替身代替 GPU，验证调用约定，不产生 Modal 费用。"""

from pathlib import Path

import pytest
from PIL import Image

from app.providers.video import ModalMiniMaxH3Provider, VideoProviderError, VideoRequest
from app.providers.video.modal_minimax_h3 import build_ref2va_prompt, h3_frames


class FakeRemote:
    def __init__(self, calls, result=b"MP4DATA", error=None):
        self.calls, self.result, self.error = calls, result, error

    async def aio(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.result


class FakeMethod:
    def __init__(self, remote):
        self.remote = remote


class FakeRunner:
    def __init__(self, workflow, calls, **kw):
        self.workflow = workflow
        calls.append(("workflow", workflow))
        self.render_single_shot = FakeMethod(FakeRemote(calls, **kw))


def _img(p: Path, color) -> Path:
    Image.new("RGB", (64, 112), color).save(p)
    return p


@pytest.mark.parametrize(("seconds", "frames"), [
    (3, 124), (5, 124), (5.1, 124), (5.2, 141), (6, 158), (10, 243), (14, 345), (30, 345),
])
def test_frames_follow_h3_rule(seconds, frames):
    f = h3_frames(seconds)
    assert f == frames and (f - 5) % 17 == 0 and 124 <= f <= 345


def test_ref2va_prompt_declares_pictures():
    text = build_ref2va_prompt("[Push in] 沸水注入", ["product_package", "tea_soup"])
    assert text.startswith("<Picture 1> 是本镜头锁定的首帧画面")
    assert "<Picture 2> 是商品参考图（product_package）" in text and "<Picture 3>" in text
    assert text.endswith("[Push in] 沸水注入")


async def test_generate_calls_ref2va_with_first_frame_then_refs(tmp_path):
    calls = []
    prov = ModalMiniMaxH3Provider(steps=12, runner_factory=lambda wf: FakeRunner(wf, calls))
    assert prov.is_configured()
    frame = _img(tmp_path / "frame.png", (200, 0, 0))
    pkg = _img(tmp_path / "pkg.png", (0, 200, 0))
    req = VideoRequest(shot_id="shot_abc", prompt="[Macro static] 沸水注入玻璃杯", first_frame_path=frame,
                       aspect_ratio="9:16", duration_seconds=3, output_dir=tmp_path / "out",
                       extra={"reference_paths": [pkg], "reference_roles": ["product_package"], "seed": 7})
    events = [ev async for ev in prov.generate(req)]

    assert calls[0] == ("workflow", "ref2va")
    prompt, images, frames, seed, steps, ratio = calls[1]
    assert images == [frame.read_bytes(), pkg.read_bytes()]  # 首帧必须在第 1 位
    assert "<Picture 1> 是本镜头锁定的首帧" in prompt and "product_package" in prompt
    assert (frames, seed, steps, ratio) == (124, 7, 12, "9:16")
    assert any("已调整" in e.message for e in events)  # 3 秒分镜被拉长到 5.2 秒有提示
    done = events[-1]
    assert done.stage == "done" and done.video_path.read_bytes() == b"MP4DATA"


@pytest.mark.parametrize(("mode", "n_images"), [("i2va", 1), ("ref2va", 2)])
async def test_official_prompt_sent_verbatim_with_mode_images(tmp_path, mode, n_images):
    """官方卡带提示词自带 <Picture N> 定义：原样发送；i2va 只传首帧，ref2va 传首帧 + 商品参考图。"""
    calls = []
    prov = ModalMiniMaxH3Provider(runner_factory=lambda wf: FakeRunner(wf, calls))
    frame = _img(tmp_path / "frame.png", (200, 0, 0))
    pkg = _img(tmp_path / "pkg.png", (0, 200, 0))
    req = VideoRequest(shot_id="s", prompt="official prompt", first_frame_path=frame, aspect_ratio="9:16",
                       duration_seconds=6, output_dir=tmp_path / "out",
                       extra={"reference_paths": [pkg], "reference_roles": ["product_package"],
                              "h3_prompt_mode": mode})
    _ = [ev async for ev in prov.generate(req)]
    prompt, images = calls[1][0], calls[1][1]
    assert calls[0] == ("workflow", "ref2va") and prompt == "official prompt"
    assert len(images) == n_images and images[0] == frame.read_bytes()


async def test_generate_caps_reference_count_and_wraps_errors(tmp_path):
    calls = []
    frame = _img(tmp_path / "f.png", (1, 2, 3))
    refs = [_img(tmp_path / f"r{i}.png", (i, i, i)) for i in range(12)]
    prov = ModalMiniMaxH3Provider(runner_factory=lambda wf: FakeRunner(wf, calls))
    req = VideoRequest(shot_id="s", prompt="p", first_frame_path=frame, aspect_ratio="1:1", duration_seconds=6,
                       output_dir=tmp_path, extra={"reference_paths": refs})
    _ = [ev async for ev in prov.generate(req)]
    assert len(calls[1][1]) == 9  # H3 ref2va 上限 9 张

    bad = ModalMiniMaxH3Provider(runner_factory=lambda wf: FakeRunner(wf, [], error=RuntimeError("CUDA OOM")))
    with pytest.raises(VideoProviderError, match="CUDA OOM"):
        _ = [ev async for ev in bad.generate(req)]
    empty = ModalMiniMaxH3Provider(runner_factory=lambda wf: FakeRunner(wf, [], result=b""))
    with pytest.raises(VideoProviderError, match="空视频"):
        _ = [ev async for ev in empty.generate(req)]


async def test_not_logged_in_reports_clearly(monkeypatch, tmp_path):
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    prov = ModalMiniMaxH3Provider()
    assert not prov.is_configured()
    res = await prov.warmup()
    assert not res.ready and "modal token new" in res.message
