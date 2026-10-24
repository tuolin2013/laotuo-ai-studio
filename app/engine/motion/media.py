"""动效素材加载：图片一次解码并缩到够用的尺寸；视频先用 ffmpeg 按成片帧率抽成 JPEG 序列，渲染时按时间取帧。

渲染在工作线程里多线程逐帧进行，所以这里只做线程安全的只读访问（解码结果用 lru_cache 缓存）。
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps

from app.schemas.motion import MotionAsset

# 单张素材解码后的像素上限：详情页长图很长，按宽度缩放后仍可能很大
MAX_PIXELS = 14_000_000
EXTRACT_TIMEOUT = 300


class MediaError(RuntimeError):
    pass


@lru_cache(maxsize=96)
def _load_frame(path: str) -> Image.Image:
    with Image.open(path) as im:
        return im.convert("RGB")


def load_image(path: Path, max_width: int) -> Image.Image:
    """解码图片（按 EXIF 方向转正），宽度超过 max_width 或总像素超限时等比缩小。保留透明通道。"""
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            has_alpha = im.mode in {"RGBA", "LA", "PA"} or (im.mode == "P" and "transparency" in im.info)
            img = im.convert("RGBA" if has_alpha else "RGB")
    except (OSError, ValueError) as e:
        raise MediaError(f"无法读取图片 {path.name}：{e}") from e
    s = min(1.0, max_width / img.width, (MAX_PIXELS / (img.width * img.height)) ** 0.5)
    if s < 0.999:
        img = img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.Resampling.LANCZOS)
    return img


class Media:
    """一个素材的渲染源。frame(t) 返回 t 秒时的画面（图片恒定，视频按帧率循环播放）。"""

    def __init__(self, asset: MotionAsset, image: Image.Image | None = None, frames: list[Path] | None = None,
                 fps: int = 30):
        self.asset = asset
        self.image = image
        self.frames = frames or []
        self.fps = fps
        if image is None and not self.frames:
            raise MediaError(f"素材 {asset.key} 没有可用的画面")

    @property
    def is_video(self) -> bool:
        return bool(self.frames)

    @property
    def size(self) -> tuple[int, int]:
        if self.image is not None:
            return self.image.size
        return _load_frame(str(self.frames[0])).size

    @property
    def ratio(self) -> float:
        w, h = self.size
        return h / w if w else 1.0

    def frame(self, t: float) -> Image.Image:
        if self.image is not None:
            return self.image
        i = int(max(0.0, t) * self.fps) % len(self.frames)
        return _load_frame(str(self.frames[i]))

    @classmethod
    def load(cls, asset: MotionAsset, work_dir: Path, fps: int, need_seconds: float, max_width: int,
             ffmpeg: str | None) -> Media:
        if not asset.exists:
            raise MediaError(f"素材 {asset.key} 的文件已不存在：{asset.path.name}")
        if asset.kind == "image":
            return cls(asset, image=load_image(asset.path, max_width), fps=fps)
        if not ffmpeg:
            raise MediaError("缺少 ffmpeg，无法读取视频素材：请运行 pip install imageio-ffmpeg")
        out = work_dir / f"frames_{asset.key}"
        out.mkdir(parents=True, exist_ok=True)
        for old in out.glob("*.jpg"):
            old.unlink()
        width = min(max_width, asset.width or max_width)
        width -= width % 2
        seconds = max(0.5, min(need_seconds, asset.duration_seconds or need_seconds))
        args = [ffmpeg, "-y", "-v", "error", "-t", f"{seconds:.3f}", "-i", str(asset.path), "-an",
                "-vf", f"fps={fps},scale={width}:-2:flags=lanczos", "-q:v", "3", str(out / "f_%05d.jpg")]
        try:
            proc = subprocess.run(args, capture_output=True, timeout=EXTRACT_TIMEOUT, check=False)
        except subprocess.TimeoutExpired as e:
            raise MediaError(f"视频素材 {asset.key} 抽帧超时") from e
        frames = sorted(out.glob("f_*.jpg"))
        if proc.returncode != 0 or not frames:
            err = proc.stderr.decode("utf-8", errors="ignore").strip()[-300:]
            raise MediaError(f"视频素材 {asset.key} 抽帧失败：{err or proc.returncode}")
        return cls(asset, frames=frames, fps=fps)
