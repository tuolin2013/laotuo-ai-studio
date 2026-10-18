"""参考爆款视频本地抽帧：读取时长 / 宽高比，按时间轴均匀提取 4~8 张关键帧并记录时间戳。

只依赖 ffmpeg 可执行文件：优先使用 imageio-ffmpeg 自带的二进制（pip 安装即可，免配置 PATH），
找不到时退回系统 PATH 中的 ffmpeg。全部通过 asyncio 子进程调用，不阻塞 Gradio 事件循环。
"""

from __future__ import annotations

import asyncio
import base64
import io
import itertools
import re
import shutil
from pathlib import Path

from PIL import Image, ImageStat

from app.schemas.video import ReferenceVideoFrame, ReferenceVideoSource

VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".m4v")
MIN_FRAMES, MAX_FRAMES = 4, 8
MAX_VIDEO_BYTES = 500 * 1024 * 1024
FFMPEG_TIMEOUT = 120
# 与平台视频比例对齐：抽帧后按最接近的标准比例登记
_RATIOS = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1.0, "3:4": 3 / 4, "4:3": 4 / 3}

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\S+.*?Video:.*?\b(\d{2,5})x(\d{2,5})\b")
_ROTATE_RE = re.compile(r"(?:rotation of\s*|rotate\s*:\s*)(-?\d+(?:\.\d+)?)")


class VideoCloneError(RuntimeError):
    pass


def ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return shutil.which("ffmpeg")


def format_timestamp(seconds: float) -> str:
    """12.34 -> 00:12.3；超过 1 小时补小时位。"""
    tenths = round(max(seconds, 0) * 10)
    minutes, rest = divmod(tenths, 600)
    hours, minutes = divmod(minutes, 60)
    stamp = f"{minutes:02d}:{rest // 10:02d}.{rest % 10}"
    return f"{hours}:{stamp}" if hours else stamp


def nearest_ratio(width: int, height: int) -> str:
    r = width / height
    return min(_RATIOS, key=lambda k: abs(_RATIOS[k] - r))


def sample_timestamps(duration: float, count: int) -> list[float]:
    """均匀抽样：取每段时间的中点，避开片头黑场与片尾。"""
    count = max(MIN_FRAMES, min(MAX_FRAMES, int(count)))
    step = duration / count
    return [round((i + 0.5) * step, 2) for i in range(count)]


def parse_probe(stderr: str) -> tuple[float, int, int]:
    """从 `ffmpeg -i` 的输出中解析时长与（考虑旋转后的）显示宽高。"""
    d = _DURATION_RE.search(stderr)
    v = _VIDEO_RE.search(stderr)
    if not d or not v:
        raise VideoCloneError("无法读取参考视频的时长或画面尺寸，请确认文件是完整的 mp4 / mov / webm 视频")
    duration = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))
    width, height = int(v.group(1)), int(v.group(2))
    rot = _ROTATE_RE.search(stderr)
    if rot and int(abs(float(rot.group(1)))) % 180 == 90:
        width, height = height, width  # 手机竖拍视频常以旋转元数据存储
    if duration <= 0:
        raise VideoCloneError("参考视频时长为 0")
    return duration, width, height


async def _run(args: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT)
    except TimeoutError as e:
        proc.kill()
        raise VideoCloneError("读取参考视频超时") from e
    return proc.returncode or 0, err.decode("utf-8", errors="ignore")


def _thumb(path: Path) -> Image.Image:
    with Image.open(path) as im:
        return im.convert("L").resize((32, 32))


def frame_stats(path: Path, previous: Path | None) -> tuple[float, float]:
    """平均亮度，以及与上一关键帧的画面差异（0~1，越大越像一次切镜）。"""
    cur = _thumb(path)
    brightness = ImageStat.Stat(cur).mean[0]
    if previous is None:
        return round(brightness, 1), 0.0
    prev = _thumb(previous)
    diff = sum(abs(a - b) for a, b in zip(cur.getdata(), prev.getdata(), strict=True)) / (32 * 32 * 255)
    return round(brightness, 1), round(diff, 3)


def validate_video_file(path: Path) -> None:
    if not path.is_file():
        raise VideoCloneError("找不到上传的参考视频文件")
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise VideoCloneError(f"只支持 {' / '.join(VIDEO_EXTENSIONS)} 格式的参考视频")
    if path.stat().st_size > MAX_VIDEO_BYTES:
        raise VideoCloneError("参考视频超过 500MB，请先裁剪成短视频片段")


async def probe_video(path: Path) -> tuple[float, int, int]:
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        raise VideoCloneError("缺少 ffmpeg：请运行 pip install imageio-ffmpeg")
    _, err = await _run([ffmpeg, "-hide_banner", "-i", str(path)])
    return parse_probe(err)


async def extract_keyframes(video_path: Path | str, output_dir: Path, frame_count: int = 6,
                            file_name: str | None = None) -> ReferenceVideoSource:
    """抽取关键帧到 output_dir（如 workspace/projects/{id}/reference_video_frames/{video_id}/）。"""
    video_path = Path(video_path)
    validate_video_file(video_path)
    duration, width, height = await probe_video(video_path)
    ffmpeg = ffmpeg_exe()
    output_dir.mkdir(parents=True, exist_ok=True)
    frames: list[ReferenceVideoFrame] = []
    previous: Path | None = None
    for i, t in enumerate(sample_timestamps(duration, frame_count), start=1):
        out = output_dir / f"keyframe_{i:02d}_{format_timestamp(t).replace(':', '-')}.jpg"
        # -ss 放在 -i 前快速定位；scale 限制长边 1280，节省磁盘与多模态 token
        code, err = await _run([
            ffmpeg, "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video_path), "-frames:v", "1",
            "-vf", "scale='if(gt(iw,ih),min(1280,iw),-2)':'if(gt(iw,ih),-2,min(1280,ih))'", "-q:v", "3", str(out),
        ])
        if code != 0 or not out.is_file():
            raise VideoCloneError(f"第 {i} 张关键帧（{format_timestamp(t)}）抽取失败：{err[-200:]}")
        brightness, change = frame_stats(out, previous)
        frames.append(ReferenceVideoFrame(index=i, timestamp_seconds=t, timestamp=format_timestamp(t),
                                          file_path=out, brightness=brightness, change_score=change))
        previous = out
    return ReferenceVideoSource(
        file_path=video_path, file_name=file_name or video_path.name, duration_seconds=round(duration, 2),
        width=width, height=height, aspect_ratio=nearest_ratio(width, height), frames=frames,
    )


def frame_to_base64(path: Path, max_side: int = 768) -> str:
    """关键帧压缩为 JPEG Base64，作为多模态图像发给 LLM。"""
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def segment_bounds(source: ReferenceVideoSource) -> list[tuple[float, float]]:
    """每张关键帧代表的原片时间段（相邻关键帧的中点为分界）。"""
    ts = [f.timestamp_seconds for f in source.frames]
    cuts = [0.0, *[(a + b) / 2 for a, b in itertools.pairwise(ts)], source.duration_seconds]
    return [(round(a, 2), round(b, 2)) for a, b in itertools.pairwise(cuts)]
