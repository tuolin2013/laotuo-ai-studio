"""一键成片渲染：把 EditPlan 交给本地 ffmpeg 合成 mp4（转场、台词字幕 / 卖点花字、背景音乐与自动避让、响度统一）。

- 每个片段按整数帧截取（含转场余量），统一缩放裁切到成片尺寸，24 fps；
- 硬切用 concat、转场用 xfade / acrossfade，总时长与剪辑方案、剪映 / 达芬奇草稿完全一致；
- 字幕与花字写成 ASS 文件交给 libass 渲染（支持描边、自动缩字号），ffmpeg 在输出目录里运行，
  滤镜参数只用相对文件名，避开 Windows 盘符冒号在滤镜语法中的转义问题；
- 全部通过 asyncio 子进程执行并实时解析 -progress 输出，不阻塞 Gradio 事件循环。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from app.engine.render_engine import RenderEvent
from app.engine.video_cloner import ffmpeg_exe
from app.schemas.edit import Cue, EditPlan, Timeline

RENDER_TIMEOUT = 1200
AUDIO_RATE = 48000
ASS_NAME = "captions.ass"

# 中文字体（libass 按字体族名查找系统字体）：找到第一个存在的字体文件就用它的族名
_FONT_CANDIDATES = (
    ("C:/Windows/Fonts/msyhbd.ttc", "Microsoft YaHei"),
    ("C:/Windows/Fonts/msyh.ttc", "Microsoft YaHei"),
    ("C:/Windows/Fonts/simhei.ttf", "SimHei"),
    ("/System/Library/Fonts/PingFang.ttc", "PingFang SC"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "Noto Sans CJK SC"),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "WenQuanYi Micro Hei"),
)
_XFADE = {"fade": "fade", "fadeblack": "fadeblack", "fadewhite": "fadewhite"}


class RenderError(RuntimeError):
    pass


def ass_font_name() -> str:
    for path, family in _FONT_CANDIDATES:
        if Path(path).exists():
            return family
    return "Microsoft YaHei"


# ---------------------------------------------------------------- ASS 字幕 / 花字


def _ass_time(frames: int, fps: int) -> str:
    cs = round(frames * 100 / fps)
    h, rest = divmod(cs, 360000)
    m, rest = divmod(rest, 6000)
    s, cs = divmod(rest, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_text(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", "\\N")


def _text_width(text: str, size: int) -> float:
    """估算单行宽度：中文等全角字符约 1 个字号宽，半角约 0.55 个字号宽。"""
    return sum(size if ord(ch) > 0x2E80 else size * 0.55 for ch in text)


def fit_font_size(text: str, size: int, max_width: float) -> int:
    width = _text_width(text, size)
    return size if width <= max_width else max(12, math.floor(size * max_width / width))


def _ass_color(hex_rgb: str, alpha: int = 0) -> str:
    """#RRGGBB -> ASS 的 &HAABBGGRR。"""
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def caption_layout(width: int, height: int) -> dict[str, int]:
    """字号与安全边距：竖屏避开平台底部文案 / 右侧按钮区，字幕在下方约 1/5 处、花字在上方约 1/8 处。"""
    base = min(width, height)
    portrait = height > width
    return {
        "sub_size": round(base * 0.06),
        "head_size": round(base * 0.085),
        "sub_margin": round(height * (0.2 if portrait else 0.08)),
        "head_margin": round(height * (0.12 if portrait else 0.06)),
        "side_margin": round(width * 0.06),
    }


def build_ass(plan: EditPlan, cues: list[Cue], font: str | None = None) -> str:
    w, h, fps = plan.width, plan.height, plan.fps
    lay = caption_layout(w, h)
    font = font or ass_font_name()
    max_width = w - 2 * lay["side_margin"]
    white, black, yellow = _ass_color("#FFFFFF"), _ass_color("#000000"), _ass_color("#FFE14D")
    shadow = _ass_color("#000000", 0x80)
    styles = [
        f"Style: Sub,{font},{lay['sub_size']},{white},{white},{black},{shadow},1,0,0,0,100,100,0,0,1,"
        f"{max(3, lay['sub_size'] // 16)},1,2,{lay['side_margin']},{lay['side_margin']},{lay['sub_margin']},1",
        f"Style: Head,{font},{lay['head_size']},{yellow},{yellow},{black},{shadow},1,0,0,0,100,100,2,0,1,"
        f"{max(4, lay['head_size'] // 12)},2,8,{lay['side_margin']},{lay['side_margin']},{lay['head_margin']},1",
    ]
    events = []
    for cue in cues:
        style, size = ("Head", lay["head_size"]) if cue.kind == "headline" else ("Sub", lay["sub_size"])
        fs = fit_font_size(cue.text, size, max_width)
        override = f"{{\\fs{fs}}}" if fs != size else ""
        events.append(f"Dialogue: {1 if style == 'Head' else 0},{_ass_time(cue.start, fps)},"
                      f"{_ass_time(cue.end, fps)},{style},,0,0,0,,{override}{_ass_text(cue.text)}")
    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 2",
        "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        *styles, "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events, "",
    ])


# ---------------------------------------------------------------- ffmpeg 滤镜图


def _samples(frames: int, fps: int) -> int:
    return frames * AUDIO_RATE // fps


def build_filter_graph(plan: EditPlan, tl: Timeline, *, with_captions: bool, bgm_input: int | None) -> str:
    """返回 -filter_complex 内容；输出标签 [vout] / [aout]。片段 k 的素材是第 k 个输入。"""
    w, h, fps = plan.width, plan.height, plan.fps
    parts: list[str] = []
    for k, p in enumerate(tl.clips):
        first, last = p.in_frame - p.head, p.in_frame + p.length + p.tail
        parts.append(
            f"[{k}:v]fps={fps},scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},"
            f"setsar=1,format=yuv420p,tpad=stop_mode=clone:stop_duration=1,"
            # trim / tpad 之后帧率信息会丢失（1/0），xfade 要求恒定帧率：末尾重新声明帧率
            f"trim=start_frame={first}:end_frame={last},setpts=PTS-STARTPTS,fps={fps}[v{k}]")
        a0, a1 = _samples(first, fps), _samples(last, fps)
        if p.clip.has_audio:
            src = f"[{k}:a]aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,apad,"
        else:  # 无声素材补一段静音，保证音画逐段对齐
            src = f"anullsrc=r={AUDIO_RATE}:cl=stereo,aformat=sample_fmts=fltp,"
            a1, a0 = a1 - a0, 0
        parts.append(f"{src}atrim=start_sample={a0}:end_sample={a1},asetpts=PTS-STARTPTS,"
                     f"volume={p.clip.volume:g}[a{k}]")
    cur_v, cur_a = "v0", "a0"
    for j, join in enumerate(tl.joins):
        nv, na = f"v{j + 1}", f"a{j + 1}"
        if join.kind == "cut":
            parts.append(f"[{cur_v}][{nv}]concat=n=2:v=1:a=0,fps={fps}[vj{j}]")
            parts.append(f"[{cur_a}][{na}]concat=n=2:v=0:a=1[aj{j}]")
        else:
            parts.append(f"[{cur_v}][{nv}]xfade=transition={_XFADE[join.kind]}:duration={join.duration / fps:.6f}:"
                         f"offset={(join.cut - join.before) / fps:.6f}[vj{j}]")
            parts.append(f"[{cur_a}][{na}]acrossfade=ns={_samples(join.duration, fps)}:c1=tri:c2=tri[aj{j}]")
        cur_v, cur_a = f"vj{j}", f"aj{j}"
    caption = f"subtitles={ASS_NAME}," if with_captions else ""
    parts.append(f"[{cur_v}]{caption}format=yuv420p[vout]")

    total_s = tl.total / fps
    voice = cur_a
    if bgm_input is not None:
        fade_out = max(total_s - 1.5, 0)
        parts.append(f"[{bgm_input}:a]aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"atrim=end_sample={_samples(tl.total, fps)},asetpts=PTS-STARTPTS,afade=t=in:d=0.8,"
                     f"afade=t=out:st={fade_out:.3f}:d=1.5,volume={plan.bgm_volume:g}[bgm]")
        if plan.duck_bgm:
            # 人物说话（原声变响）时把背景音乐压低，说完自动恢复
            parts.append(f"[{voice}]asplit=2[voice][sc]")
            parts.append("[bgm][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[bgmd]")
            parts.append("[voice][bgmd]amix=inputs=2:duration=first:normalize=0[mix]")
        else:
            parts.append(f"[{voice}][bgm]amix=inputs=2:duration=first:normalize=0[mix]")
        voice = "mix"
    # 短视频平台常用响度 -14 LUFS；loudnorm 内部会升采样，最后统一回 48 kHz
    parts.append(f"[{voice}]loudnorm=I=-14:TP=-1.5:LRA=11,aresample={AUDIO_RATE}[aout]")
    return ";".join(parts)


def build_ffmpeg_args(ffmpeg: str, plan: EditPlan, tl: Timeline, out_path: Path, *, with_captions: bool) -> list[str]:
    args = [ffmpeg, "-y", "-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1"]
    for p in tl.clips:
        args += ["-i", str(p.clip.source_path)]
    bgm_input = None
    if plan.bgm_path is not None:
        bgm_input = len(tl.clips)
        args += ["-stream_loop", "-1", "-i", str(plan.bgm_path)]  # 音乐比成片短时循环播放
    graph = build_filter_graph(plan, tl, with_captions=with_captions, bgm_input=bgm_input)
    args += ["-filter_complex", graph, "-map", "[vout]", "-map", "[aout]",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(plan.fps),
             "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE), "-movflags", "+faststart",
             "-t", f"{tl.total / plan.fps:.3f}", str(out_path)]
    return args


def safe_name(text: str, limit: int = 24) -> str:
    cleaned = "".join(ch for ch in (text or "") if ch not in '\\/:*?"<>|\r\n\t').strip().strip(".")
    return cleaned[:limit] or "成片"


async def _read_progress(stream: asyncio.StreamReader, total_us: int, queue: asyncio.Queue) -> None:
    last = -1
    while line := await stream.readline():
        key, _, value = line.decode("utf-8", errors="ignore").strip().partition("=")
        if key in {"out_time_us", "out_time_ms"} and value.isdigit() and total_us > 0:
            pct = min(99, int(int(value) * 100 / total_us))
            if pct // 10 > last // 10:
                last = pct
                await queue.put(pct)


async def render_final(plan: EditPlan, out_dir: Path, timeout: float = RENDER_TIMEOUT) -> AsyncIterator[RenderEvent]:
    """渲染成片 mp4，逐条产出进度事件；最后一条事件带 video_path。"""
    plan.require_renderable()
    if plan.bgm_path is not None and not plan.bgm_path.exists():
        raise RenderError("背景音乐文件不存在，请重新上传或移除背景音乐")
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        raise RenderError("缺少 ffmpeg：请运行 pip install imageio-ffmpeg")
    tl = plan.timeline()
    if tl.total <= 0:
        raise RenderError("成片时长为 0，请检查各片段的出入点")
    out_dir.mkdir(parents=True, exist_ok=True)
    cues = plan.cues(tl, subtitles=plan.burn_subtitles, headlines=plan.burn_headlines)
    if cues:
        (out_dir / ASS_NAME).write_text(build_ass(plan, cues), encoding="utf-8")
    out = out_dir / f"{safe_name(plan.title)}_{datetime.now():%m%d_%H%M%S}.mp4"
    args = build_ffmpeg_args(ffmpeg, plan, tl, out, with_captions=bool(cues))
    total_s = tl.total / plan.fps
    fades = sum(1 for j in tl.joins if j.kind != "cut")
    yield RenderEvent(f"🎞️ 开始合成：{len(tl.clips)} 个片段 · {total_s:.1f} 秒 · {plan.width}×{plan.height} "
                      f"{plan.fps}fps · {fades} 处转场 · 字幕 / 花字 {len(cues)} 条"
                      + (" · 背景音乐" + ("（说话时自动避让）" if plan.duck_bgm else "") if plan.bgm_path else ""))

    proc = await asyncio.create_subprocess_exec(*args, cwd=str(out_dir), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    queue: asyncio.Queue[int] = asyncio.Queue()
    reader = asyncio.create_task(_read_progress(proc.stdout, int(total_s * 1_000_000), queue))
    errors = asyncio.create_task(proc.stderr.read())
    waiter = asyncio.create_task(proc.wait())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while True:
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, waiter}, timeout=max(deadline - loop.time(), 0),
                                         return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield RenderEvent(f"⏳ 合成进度 {getter.result()}%")
                continue
            getter.cancel()
            if waiter in done:
                break
            raise RenderError(f"合成超时（超过 {timeout:.0f} 秒）")
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        await reader
    err = (await errors).decode("utf-8", errors="ignore").strip()
    if proc.returncode != 0 or not out.exists():
        out.unlink(missing_ok=True)
        raise RenderError(f"ffmpeg 合成失败：{err[-400:] or f'退出码 {proc.returncode}'}")
    size_mb = out.stat().st_size / 1024 / 1024
    yield RenderEvent(f"✅ 成片已生成：{out.name}（{total_s:.1f} 秒，{size_mb:.1f} MB）", video_path=out, done=True)
