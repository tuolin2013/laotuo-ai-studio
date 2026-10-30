"""把动效广告渲染成 mp4：多线程逐帧绘制 → 原始 RGB 帧写进 ffmpeg → 混入本机合成的配乐音效 → 响度统一到 -14 LUFS。

这里全部是同步代码，跑在工作线程里（UI 用 asyncio.to_thread 调用），进度通过回调送回事件循环。
每次渲染都写进新的文件夹（带时间戳），绝不覆盖旧成片。
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from app.engine.motion import audio
from app.engine.motion.media import Media
from app.engine.motion.renderer import THUMB, MotionRenderer
from app.engine.motion.review import Review, contact_sheet, review, sample_frames
from app.schemas.motion import MotionAd

WORKERS = 2
ENCODE_TIMEOUT = 1800
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"
# Debian 12 的 ffmpeg 5.1 在 loudnorm → aresample 之后协商不出声道布局（aac 报 Cannot select channel layout），
# 末尾显式写成立体声；ffmpeg 7 不需要，但写上也没有副作用
STEREO = "aformat=channel_layouts=stereo"
AUDIO_FILTER = f"{LOUDNORM},aresample={audio.SR},{STEREO}"

Progress = Callable[[str], None]


class MotionRenderError(RuntimeError):
    pass


@dataclass
class RenderResult:
    video_path: Path
    contact_sheet_path: Path
    review: Review
    seconds: float
    width: int
    height: int


def load_media(ad: MotionAd, work_dir: Path, ffmpeg: str | None) -> dict[str, Media]:
    """只加载分镜实际用到的素材；视频按它所在分镜的时长抽帧。"""
    need: dict[str, float] = {}
    for sp in ad.timeline():
        for key in sp.scene.images:
            need[key] = max(need.get(key, 0.0), sp.seconds + 0.5)
    width = max(ad.width, ad.height)
    return {key: Media.load(ad.asset(key), work_dir, ad.fps, secs, width, ffmpeg) for key, secs in need.items()}


def _loudness(ffmpeg: str, path: Path) -> tuple[float, float] | None:
    proc = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af",
                           "loudnorm=print_format=json", "-f", "null", "-"], capture_output=True, timeout=300,
                          check=False)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr.decode("utf-8", errors="ignore"))
    if not m:
        return None
    data = json.loads(m.group(0))
    try:
        return float(data["input_i"]), float(data["input_tp"])
    except (KeyError, ValueError):
        return None


def render_motion(ad: MotionAd, out_dir: Path, ffmpeg: str, *, scale: float = 1.0, crf: int = 17,
                  progress: Progress | None = None, stem: str = "motion") -> RenderResult:
    """渲染整条动效广告。scale < 1 为快速预览（分辨率减半、不加颗粒）。"""
    say = progress or (lambda _msg: None)
    ad.require_renderable()
    out_dir.mkdir(parents=True, exist_ok=True)
    say(f"📦 加载素材：{len({k for s in ad.scenes for k in s.images})} 个")
    media = load_media(ad, out_dir, ffmpeg)
    r = MotionRenderer(ad, media, scale=scale, grain=ad.grain and scale >= 0.99)
    total = r.total
    seconds = r.seconds
    say(f"🎞️ 开始逐帧渲染：{len(r.spans)} 个分镜 · {seconds:.1f} 秒 · {r.W}×{r.H} {r.fps}fps · {total} 帧"
        f" · {ad.bpm} BPM")

    events = r.sound_events()
    wav = out_dir / "soundtrack.wav"
    left, right = audio.compose(seconds, ad.bpm, r.theme.music_root, r.theme.music_mode, drop=r.drop_time(),
                                outro=r.outro_time(), music=ad.music, events=events, sfx=ad.sfx)
    audio.write_wav(wav, left, right)
    say(f"🎵 配乐与音效已合成：{sum(1 for e in events if ad.sfx)} 个音效"
        + ("，钩子之后起势" if r.drop_time() is not None else "") + ("" if ad.music else "（不含配乐）"))

    video = out_dir / f"{stem}.mp4"
    args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{r.W}x{r.H}", "-r", str(r.fps), "-i", "pipe:0",
            "-i", str(wav), "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "medium" if scale >= 0.99 else "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-af", AUDIO_FILTER, "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "-t", f"{seconds:.3f}", str(video)]
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    thumbs = []
    samples = dict(sample_frames(r))
    sheet_frames: dict[int, Image.Image] = {}
    try:
        with ThreadPoolExecutor(WORKERS) as pool:
            pending: deque = deque()
            nxt, last_pct = 0, -1
            for f in range(total):
                while nxt < total and len(pending) < WORKERS * 3:
                    pending.append((nxt, pool.submit(r.frame, nxt)))
                    nxt += 1
                idx, fut = pending.popleft()
                img = fut.result()
                proc.stdin.write(img.tobytes())
                thumbs.append(_thumb(img))
                if idx in samples:
                    sheet_frames[idx] = img.copy()
                pct = (f + 1) * 100 // total
                if pct // 10 > last_pct // 10:
                    last_pct = pct
                    say(f"⏳ 渲染进度 {pct}%（{f + 1}/{total} 帧）")
        proc.stdin.close()
        err = proc.stderr.read().decode("utf-8", errors="ignore")
        code = proc.wait(timeout=ENCODE_TIMEOUT)
    except (BrokenPipeError, OSError) as e:
        proc.kill()
        err = proc.stderr.read().decode("utf-8", errors="ignore") if proc.stderr else ""
        raise MotionRenderError(f"ffmpeg 编码中断：{err[-300:] or e}") from e
    finally:
        if proc.poll() is None:
            proc.kill()
    if code != 0 or not video.exists():
        video.unlink(missing_ok=True)
        raise MotionRenderError(f"ffmpeg 编码失败：{err[-400:] or code}")

    sheet = contact_sheet([(sheet_frames[f], label) for f, label in sample_frames(r) if f in sheet_frames],
                          out_dir / f"{stem}_contact_sheet.png")
    loud = _loudness(ffmpeg, video)
    rv = review(r, thumbs, loud)
    (out_dir / f"{stem}_review.md").write_text(rv.markdown(), encoding="utf-8")
    cleanup_frames(out_dir)
    size = video.stat().st_size / 1024 / 1024
    say(f"✅ 动效广告已生成：{video.name}（{seconds:.1f} 秒，{size:.1f} MB），自检 "
        f"{len(rv.checks) - rv.warnings}/{len(rv.checks)} 项通过")
    return RenderResult(video, sheet, rv, seconds, r.W, r.H)


def _thumb(img: Image.Image) -> np.ndarray:
    """48×48 灰度缩略图：自检逐帧比对画面突跳用。"""
    return np.asarray(img.convert("L").resize(THUMB, Image.Resampling.BOX), dtype=np.float32)


def cleanup_frames(work_dir: Path) -> None:
    """删除视频素材抽出的临时帧。"""
    for d in work_dir.glob("frames_*"):
        for f in d.glob("*.jpg"):
            f.unlink()
        d.rmdir()


def render_still(ad: MotionAd, out_path: Path, scene_index: int, ffmpeg: str | None = None,
                 scale: float = 0.5) -> Path:
    """渲染某一屏内容全部落定后的画面（与联系表取帧时刻相同），表格微调时快速看版式。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        media = load_media(ad, out_path.parent, ffmpeg)
        r = MotionRenderer(ad, media, scale=scale, grain=False)
        samples = sample_frames(r)
        f, _ = samples[min(max(scene_index, 0), len(r.spans) - 1) + 1]
        r.frame(f).save(out_path)
    finally:
        cleanup_frames(out_path.parent)
    return out_path
