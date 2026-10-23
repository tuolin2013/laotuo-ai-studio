"""剪辑大师引擎：分析各分镜视频（时长 / 画面 / 有声段落）→ 执行 video_editor Skill → 剪辑方案 EditPlan。

- 素材取每个分镜的最新一版视频，按剧本顺序排列；
- 用 ffmpeg silencedetect 找出每个片段里真正有声音的时间段：H3 的台词常常不在片段开头
  （实测钩子镜头 5.2 秒素材里台词在 1.85–3.85 秒），按剧本计划时长硬切会把台词剪断；
- 真实 LLM 与演示模式输出同一种 JSON，由 assemble_plan() 统一校验、纠正后写入 EditPlan。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.engine.compiler import SYSTEM_PROMPT, compile_video_editor
from app.engine.compliance import sanitize
from app.engine.runtime import EngineRuntime
from app.engine.video_cloner import ffmpeg_exe
from app.providers.demo.edit import demo_edit
from app.schemas.config import CategoryConfig, PlatformConfig
from app.schemas.edit import (
    MAX_HEADLINE_CHARS,
    MAX_TITLE_CHARS,
    MIN_CLIP_SECONDS,
    OUTPUT_SIZES,
    SPEECH_CHARS_PER_SECOND,
    EditClip,
    EditPlan,
    spoken_text,
    transition_code,
    visible_len,
)
from app.schemas.project import Project
from app.schemas.video import StoryboardShot, VideoAsset, VideoScript

DEFAULT_EDITOR_SKILL = "default_video_editor"
PROBE_TIMEOUT = 120
# 有声段落检测：低于 -30 dB 持续 0.3 秒视为静音（H3 原声平均响度约 -14 dB，环境底噪通常低于 -35 dB）
SILENCE_NOISE_DB = -30
SILENCE_MIN_SECONDS = 0.3

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\S+.*?Video:.*?\b(\d{2,5})x(\d{2,5})\b(?:.*?(\d+(?:\.\d+)?) fps)?")
_AUDIO_RE = re.compile(r"Stream #\S+.*?Audio:")
_SILENCE_RE = re.compile(r"silence_(start|end):\s*(-?\d+(?:\.\d+)?)")


class EditError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    duration: float
    width: int = 0
    height: int = 0
    fps: float = 0
    has_audio: bool = False
    has_video: bool = False
    # 有声音的时间段（秒），已合并相邻的短间隙
    sound_spans: list[tuple[float, float]] = field(default_factory=list)


def parse_media(stderr: str) -> MediaInfo:
    d = _DURATION_RE.search(stderr)
    if not d:
        raise EditError("无法读取素材时长，请确认文件是完整的音视频文件")
    duration = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))
    v = _VIDEO_RE.search(stderr)
    return MediaInfo(
        duration=round(duration, 3), width=int(v.group(1)) if v else 0, height=int(v.group(2)) if v else 0,
        fps=float(v.group(3)) if v and v.group(3) else 0, has_audio=bool(_AUDIO_RE.search(stderr)),
        has_video=bool(v),
    )


def sound_spans_from_silence(stderr: str, duration: float, merge_gap: float = 0.3,
                             min_span: float = 0.12) -> list[tuple[float, float]]:
    """silencedetect 输出的静音区间取反 = 有声区间。"""
    silences: list[tuple[float, float]] = []
    start: float | None = None
    for kind, value in _SILENCE_RE.findall(stderr):
        t = max(0.0, float(value))
        if kind == "start":
            start = t
        elif start is not None:
            silences.append((start, t))
            start = None
    if start is not None:
        silences.append((start, duration))
    spans, cursor = [], 0.0
    for a, b in silences:
        if a - cursor > 1e-3:
            spans.append((cursor, a))
        cursor = max(cursor, b)
    if duration - cursor > 1e-3:
        spans.append((cursor, duration))
    merged: list[tuple[float, float]] = []
    for a, b in spans:
        if merged and a - merged[-1][1] < merge_gap:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return [(round(a, 2), round(b, 2)) for a, b in merged if b - a >= min_span]


async def _run_ffmpeg(args: list[str], timeout: float = PROBE_TIMEOUT) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        proc.kill()
        raise EditError("分析素材超时") from e
    return proc.returncode or 0, err.decode("utf-8", errors="ignore")


async def analyze_media(path: Path) -> MediaInfo:
    """一次 ffmpeg 调用同时拿到时长 / 画面尺寸 / 帧率，以及音轨的有声段落。"""
    ffmpeg = ffmpeg_exe()
    if not ffmpeg:
        raise EditError("缺少 ffmpeg：请运行 pip install imageio-ffmpeg")
    if not Path(path).is_file():
        raise EditError(f"找不到素材文件：{Path(path).name}")
    _, err = await _run_ffmpeg([
        ffmpeg, "-hide_banner", "-nostats", "-i", str(path), "-vn",
        "-af", f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_SECONDS}", "-f", "null", "-",
    ])
    info = parse_media(err)
    if info.has_audio:
        info.sound_spans = sound_spans_from_silence(err, info.duration)
    return info


_SPOKEN_CHAR = re.compile(r"[一-鿿A-Za-z0-9]")


def estimated_speech_seconds(line: str) -> float:
    """按中速口播估算台词时长：只数汉字、字母、数字（标点不发音）。"""
    return round(len(_SPOKEN_CHAR.findall(line or "")) / SPEECH_CHARS_PER_SECOND, 1)


def speech_window(line: str, info: MediaInfo) -> tuple[float, float] | None:
    """有台词的镜头：台词大致的起止时间（第一个有声段落开头 ~ 最后一个有声段落结尾）。

    环境音贯穿全片时（有声段落比台词长得多）无法据此定位台词，返回 None，改按语速估算。
    """
    if not line or not info.sound_spans:
        return None
    start, end = info.sound_spans[0][0], info.sound_spans[-1][1]
    if end - start > estimated_speech_seconds(line) * 1.8 + 1.2:
        return None
    return start, end


@dataclass
class ClipSource:
    index: int  # 分镜序号（从 1 开始）
    shot: StoryboardShot
    video: VideoAsset
    info: MediaInfo

    @property
    def line(self) -> str:
        return spoken_text(self.shot.character_dialogue)

    def payload(self) -> dict[str, Any]:
        """发给剪辑 Skill 的素材描述（演示剪辑也用同一份数据）。"""
        s = self.shot
        return {
            "shot_index": self.index,
            "shot_purpose": s.shot_purpose,
            "planned_seconds": s.duration_seconds,
            "actual_seconds": self.info.duration,
            "shot_size": s.shot_size,
            "camera_movement": s.camera_movement,
            "scene_description": s.scene_description[:160],
            "subject_action": s.subject_action[:120],
            "audio_mode": s.audio_mode,
            "spoken_line": self.line,
            "estimated_speech_seconds": estimated_speech_seconds(self.line),
            "ambient_sound": s.ambient_sound_description[:80],
            "sound_spans": [[a, b] for a, b in self.info.sound_spans],
        }


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_text(text: Any, limit: int, cat: CategoryConfig, plat: PlatformConfig) -> str:
    out = sanitize(str(text or "").strip(), cat, plat)
    while visible_len(out) > limit:
        out = out[:-1]
    return out.strip()


def assemble_plan(data: dict[str, Any], sources: list[ClipSource], script: VideoScript, cat: CategoryConfig,
                  plat: PlatformConfig, skill_id: str, idea: str | None,
                  previous: EditPlan | None = None) -> EditPlan:
    """把 Skill 输出的剪辑 JSON 纠正为合法的 EditPlan。

    纠正规则：出入点限制在素材范围内且不剪断台词；未知转场改为硬切；花字去违禁词并截到 14 字；
    缺失的镜头追加为停用；重复的镜头只保留第一次；钩子镜头强制排第一并启用。
    """
    by_index = {s.index: s for s in sources}
    rows = [r for r in data.get("clips") or [] if isinstance(r, dict)]
    clips: list[EditClip] = []
    seen: set[int] = set()

    def make(src: ClipSource, row: dict[str, Any], enabled: bool) -> EditClip:
        dur = src.info.duration
        line = src.line
        window = speech_window(line, src.info)
        # 检测到台词位置时以检测结果为准；检测不到时按语速估算台词需要的最短时长
        need = estimated_speech_seconds(line) + 0.3 if line and not window else MIN_CLIP_SECONDS
        need = min(max(need, MIN_CLIP_SECONDS), dur)
        src_in = min(max(_num(row.get("source_in"), 0.0), 0.0), max(dur - need, 0.0))
        src_out = min(max(_num(row.get("source_out"), dur), src_in + need), dur)
        if window:
            # 不剪断台词：入点不晚于台词开头，出点不早于台词结尾（都在素材范围内）
            src_in = min(src_in, max(window[0] - 0.1, 0.0))
            src_out = max(src_out, min(window[1] + 0.15, dur))
        kind = transition_code(row.get("transition_out")) or "cut"
        return EditClip(
            shot_id=src.shot.shot_id, shot_index=src.index, video_id=src.video.video_id,
            source_path=src.video.file_path, source_duration=dur, width=src.info.width, height=src.info.height,
            has_audio=src.info.has_audio, enabled=enabled, source_in=round(src_in, 2), source_out=round(src_out, 2),
            transition_out=kind,
            transition_seconds=round(min(max(_num(row.get("transition_seconds"), 0.3), 0.1), 1.0), 2),
            volume=round(min(max(_num(row.get("volume"), 1.0), 0.0), 2.0), 2),
            headline=_clean_text(row.get("headline"), MAX_HEADLINE_CHARS, cat, plat),
            subtitle=line,
            subtitle_start=window[0] if window else None, subtitle_end=window[1] if window else None,
        )

    for row in rows:
        try:
            idx = int(row.get("shot_index"))
        except (TypeError, ValueError):
            continue
        if idx not in by_index or idx in seen:
            continue
        seen.add(idx)
        clips.append(make(by_index[idx], row, row.get("enabled", True) is not False))
    for src in sources:
        if src.index not in seen:  # Skill 漏掉的镜头：保留为停用，用户可在表格里勾选启用
            clips.append(make(src, {}, enabled=not rows))
    hook = next((c for c in clips if c.shot_index == 1), None)
    if hook is not None:
        clips.remove(hook)
        clips.insert(0, hook.model_copy(update={"enabled": True}))
    if clips:
        clips[-1] = clips[-1].model_copy(update={"transition_out": "cut"})

    w, h = OUTPUT_SIZES.get(script.aspect_ratio, (1080, 1920))
    plan = EditPlan(
        script_id=script.script_id, editor_skill_id=skill_id, aspect_ratio=script.aspect_ratio, width=w, height=h,
        title=_clean_text(data.get("title"), MAX_TITLE_CHARS, cat, plat) or "带货短视频成片",
        edit_summary=str(data.get("edit_summary") or "").strip()[:300], user_edit_idea=idea or None, clips=clips,
    )
    if previous is not None:  # 重新生成方案时沿用用户已设置的成片选项与背景音乐
        plan.burn_subtitles, plan.burn_headlines = previous.burn_subtitles, previous.burn_headlines
        plan.bgm_volume, plan.duck_bgm = previous.bgm_volume, previous.duck_bgm
        if previous.bgm_path and previous.bgm_path.exists():
            plan.bgm_path, plan.bgm_duration = previous.bgm_path, previous.bgm_duration
        plan.renders = list(previous.renders)
    return plan


def latest_videos(script: VideoScript) -> list[tuple[int, StoryboardShot, VideoAsset]]:
    """每个分镜的最新一版视频（按剧本顺序）；没有视频的分镜跳过。"""
    out = []
    for i, shot in enumerate(script.shots, start=1):
        vids = shot.playable_videos
        if vids:
            out.append((i, shot, vids[-1]))
    return out


def stale_shots(plan: EditPlan, script: VideoScript) -> list[int]:
    """剪辑方案生成后，哪些分镜有了新视频 / 新增了视频（序号），提示用户重新生成方案。"""
    used = plan.sources()
    return [i for i, shot, v in latest_videos(script) if (shot.shot_id, v.video_id) not in used]


class EditEngine:
    def __init__(self, rt: EngineRuntime):
        self.rt = rt

    async def collect(self, script: VideoScript) -> list[ClipSource]:
        found = latest_videos(script)
        if not found:
            raise EditError("还没有可剪辑的分镜视频：请先在 Step 3 生成视频")
        infos = await asyncio.gather(*(analyze_media(v.file_path) for _, _, v in found))
        return [ClipSource(i, shot, v, info) for (i, shot, v), info in zip(found, infos, strict=True)]

    async def generate(self, project: Project, script: VideoScript, skill_id: str | None = None,
                       user_edit_idea: str | None = None) -> tuple[EditPlan, str, list[ClipSource]]:
        skill_id = skill_id or DEFAULT_EDITOR_SKILL
        cat, plat = self.rt.category(script.category_id), self.rt.platform(script.platform_id)
        sources = await self.collect(script)
        payload = [s.payload() for s in sources]
        info = {k: v for k, v in project.product_info.items() if v}
        idea = (user_edit_idea or "").strip() or None
        if self.rt.is_live("llm"):
            skill = self.rt.skills.get("video_editor", skill_id)
            prompt = compile_video_editor(skill, script, payload, cat, plat, info, idea)
            data = await self.rt.llm().complete_json(SYSTEM_PROMPT, prompt)
            mode = "真实 LLM"
        else:
            await asyncio.sleep(self.rt.demo_delay)
            data = demo_edit(payload, cat, plat, script.target_duration_seconds, idea, info)
            mode = "演示模式"
        plan = assemble_plan(data, sources, script, cat, plat, skill_id, idea, previous=project.video_edit)
        return plan, mode, sources
