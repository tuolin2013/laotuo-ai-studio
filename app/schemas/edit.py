"""一键成片剪辑方案（Edit Decision List）：片段顺序、出入点、转场、原声音量、卖点花字与台词字幕。

时间轴规则（成片 mp4、剪映草稿、达芬奇时间线三者一致）：
- 统一按 EDIT_FPS 换算成整数帧，片段首尾相接：第 k 个片段的起点 = 前面所有启用片段的时长之和；
- 转场跨在剪辑点上，借用前一片段出点之后、后一片段入点之前的余量画面（handles），所以转场不改变总时长；
  余量不足时自动缩短，完全没有余量时退化为硬切。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import AspectRatio, EditRecord, new_id, utc_now

TransitionKind = Literal["cut", "fade", "fadeblack", "fadewhite"]
TRANSITION_KINDS: tuple[TransitionKind, ...] = ("cut", "fade", "fadeblack", "fadewhite")
TRANSITION_LABELS: dict[str, str] = {"cut": "硬切", "fade": "叠化", "fadeblack": "闪黑", "fadewhite": "闪白"}

# 与 MiniMax H3 输出帧率一致：成片与草稿都不做帧率转换，避免 24→30 帧补帧造成的顿挫
EDIT_FPS = 24
# 成片分辨率：按视频比例输出 1080P 档
OUTPUT_SIZES: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "3:4": (1080, 1440),
    "4:3": (1440, 1080), "2:3": (1080, 1620), "3:2": (1620, 1080),
}
MIN_CLIP_SECONDS = 0.5
# 卖点花字字数上限（与主图上图文案一致：≤14 个字）
MAX_HEADLINE_CHARS = 14
MAX_TITLE_CHARS = 20

_SPACES = re.compile(r"\s+")


def visible_len(text: str) -> int:
    """花字字数：不计空白。"""
    return len(_SPACES.sub("", text or ""))


_QUOTED = re.compile(r"[“\"「『]([^”\"」』]+)[”\"」』]")


def spoken_text(dialogue: str | None) -> str:
    """从「人物 + 语气 + 台词」的同期声描述中取出实际说出口的台词（引号内的文字）。

    例：西装男主播对着镜头，干脆有力地说：“皮毛营养，看这一盒！” -> 皮毛营养，看这一盒！
    """
    text = (dialogue or "").strip()
    if not text:
        return ""
    quoted = [q.strip() for q in _QUOTED.findall(text) if q.strip()]
    if quoted:
        return "".join(q if q[-1:] in "，。！？…,.!?" else q + "，" for q in quoted).rstrip("，,")
    for sep in ("说：", "说:", "：", ":"):
        if sep in text:
            return text.rsplit(sep, 1)[1].strip()
    return text


def transition_code(value: Any) -> TransitionKind | None:
    """转场：接受代码（cut）或中文名（硬切）。"""
    v = str(value or "").strip()
    if v in TRANSITION_KINDS:
        return v  # type: ignore[return-value]
    for code, label in TRANSITION_LABELS.items():
        if v == label:
            return code  # type: ignore[return-value]
    return None


class EditClip(BaseModel):
    """时间轴上的一个片段：引用某个分镜的某一版视频，截取 [source_in, source_out]（素材时间，秒）。"""

    clip_id: str = Field(default_factory=lambda: new_id("clip"))
    shot_id: str
    shot_index: int = Field(ge=1)  # 分镜序号，界面显示「镜头 N」
    video_id: str
    source_path: Path
    # 素材实测信息（ffmpeg 探测）
    source_duration: float = Field(gt=0)
    width: int = 0
    height: int = 0
    has_audio: bool = True

    enabled: bool = True
    source_in: float = Field(default=0, ge=0)
    source_out: float = Field(gt=0)
    # 切到下一个片段的转场；最后一个片段的转场不生效
    transition_out: TransitionKind = "cut"
    transition_seconds: float = Field(default=0.3, ge=0.1, le=1.0)
    volume: float = Field(default=1.0, ge=0, le=2)  # 原声（H3 原生对白 + 环境音）音量
    headline: str = ""  # 卖点花字（屏幕上方大字）
    subtitle: str = ""  # 台词字幕（逐字取自人物同期声对白）
    # 台词在素材中的起止时间（秒）；为空时按语速估算
    subtitle_start: float | None = None
    subtitle_end: float | None = None

    @model_validator(mode="after")
    def _check(self):
        self.headline = self.headline.strip()
        self.subtitle = self.subtitle.strip()
        if self.source_out > self.source_duration + 0.05:
            raise ValueError(f"镜头 {self.shot_index} 出点 {self.source_out:g} 秒超过素材时长 "
                             f"{self.source_duration:g} 秒")
        if self.source_out - self.source_in < MIN_CLIP_SECONDS - 1e-6:
            raise ValueError(f"镜头 {self.shot_index} 片段太短：出点必须比入点晚至少 {MIN_CLIP_SECONDS:g} 秒")
        if visible_len(self.headline) > MAX_HEADLINE_CHARS:
            raise ValueError(f"镜头 {self.shot_index} 卖点花字「{self.headline}」超过 {MAX_HEADLINE_CHARS} 个字")
        return self

    @property
    def length_seconds(self) -> float:
        return round(self.source_out - self.source_in, 3)


class RenderedCut(BaseModel):
    """一次一键成片的产出（追加保存，不覆盖旧版本）。"""

    render_id: str = Field(default_factory=lambda: new_id("cut"))
    file_path: Path
    duration_seconds: float
    created_at: datetime = Field(default_factory=utc_now)


# ---------------------------------------------------------------- 时间轴（整数帧）


@dataclass(frozen=True)
class PlacedClip:
    clip: EditClip
    start: int  # 成片时间轴起点（帧）
    in_frame: int  # 素材入点（帧）
    length: int  # 时长（帧）
    head: int  # 进入本片段的转场借用的入点前余量（帧）
    tail: int  # 离开本片段的转场借用的出点后余量（帧）

    @property
    def end(self) -> int:
        return self.start + self.length


@dataclass(frozen=True)
class Join:
    """相邻两个片段之间的剪辑点：转场区间为 [cut - before, cut + after]。"""

    kind: TransitionKind
    cut: int
    before: int  # 剪辑点之前的部分（借用后一片段入点前的余量）
    after: int  # 剪辑点之后的部分（借用前一片段出点后的余量）

    @property
    def duration(self) -> int:
        return self.before + self.after


@dataclass(frozen=True)
class Timeline:
    fps: int
    clips: list[PlacedClip]
    joins: list[Join]

    @property
    def total(self) -> int:
        return self.clips[-1].end if self.clips else 0

    def seconds(self, frames: int) -> float:
        return frames / self.fps


@dataclass(frozen=True)
class Cue:
    """字幕 / 花字条目（成片时间轴，帧）。"""

    start: int
    end: int
    text: str
    kind: Literal["subtitle", "headline"]


def _frames(seconds: float, fps: int) -> int:
    return round(seconds * fps)


def _media_frames(clip: EditClip, fps: int) -> int:
    return math.floor(clip.source_duration * fps + 1e-6)


def build_timeline(clips: list[EditClip], fps: int = EDIT_FPS) -> Timeline:
    # 出点按素材实际帧数截断，避免四舍五入后越过素材末尾
    lengths = [min(_frames(c.source_out, fps), _media_frames(c, fps)) - _frames(c.source_in, fps) for c in clips]
    joins: list[Join] = []
    heads, tails = [0] * len(clips), [0] * len(clips)
    cut = 0
    for j in range(len(clips) - 1):
        a_clip, b_clip = clips[j], clips[j + 1]
        cut += lengths[j]
        kind = a_clip.transition_out
        before = after = 0
        if kind != "cut":
            d = _frames(a_clip.transition_seconds, fps)
            tail_room = max(0, _media_frames(a_clip, fps) - _frames(a_clip.source_out, fps))
            head_room = _frames(b_clip.source_in, fps)
            # 每侧最多占用片段一半，保证相邻两个转场不会重叠
            max_after = min(tail_room, lengths[j + 1] // 2)
            max_before = min(head_room, lengths[j] // 2)
            after = min(d - d // 2, max_after)
            before = min(d - after, max_before)
            after = min(d - before, max_after)
            if before + after < 2:
                kind, before, after = "cut", 0, 0
        joins.append(Join(kind=kind, cut=cut, before=max(before, 0), after=max(after, 0)))
        tails[j], heads[j + 1] = joins[-1].after, joins[-1].before
    placed, start = [], 0
    for c, n, h, t in zip(clips, lengths, heads, tails, strict=True):
        placed.append(PlacedClip(clip=c, start=start, in_frame=_frames(c.source_in, fps), length=n, head=h, tail=t))
        start += n
    return Timeline(fps=fps, clips=placed, joins=joins)


# ---------------------------------------------------------------- 字幕切分

_SENTENCE_BREAK = re.compile(r"(?<=[，。！？；、,.!?;…~～])")
_TRIM_PUNCT = "，。、；,.;：: "
SUBTITLE_LINE_CHARS = 14
# 中速口播约 4.5 字 / 秒（估算台词时长用）
SPEECH_CHARS_PER_SECOND = 4.5


def split_subtitle(text: str, max_chars: int = SUBTITLE_LINE_CHARS) -> list[str]:
    """按标点切成每条 ≤max_chars 字的字幕；句末逗号句号去掉（短视频字幕的常见排法）。"""
    parts = [p.strip() for p in _SENTENCE_BREAK.split(text or "") if p.strip()]
    chunks: list[str] = []
    for p in parts:
        while len(p) > max_chars:
            chunks.append(p[:max_chars])
            p = p[max_chars:]
        if chunks and len(chunks[-1]) + len(p) <= max_chars and chunks[-1][-1:] in "，、,":
            chunks[-1] += p
        elif p:
            chunks.append(p)
    return [c.strip(_TRIM_PUNCT) for c in chunks if c.strip(_TRIM_PUNCT)]


def subtitle_window(clip: EditClip) -> tuple[float, float]:
    """台词在素材中的起止时间（秒），限制在片段出入点之内；未提供时按语速估算。"""
    lo, hi = clip.source_in, clip.source_out
    start = clip.subtitle_start if clip.subtitle_start is not None else max(lo, 0.25)
    if clip.subtitle_end is not None:
        end = clip.subtitle_end
    else:
        end = start + visible_len(clip.subtitle) / SPEECH_CHARS_PER_SECOND + 0.4
    start, end = max(lo, min(start, hi)), max(lo, min(end, hi - 0.05))
    if end - start < 0.6:
        start, end = lo, hi
    return start, end


def clip_cues(placed: PlacedClip, fps: int, subtitles: bool = True, headlines: bool = True) -> list[Cue]:
    c = placed.clip
    out: list[Cue] = []
    if headlines and c.headline:
        out.append(Cue(placed.start, placed.end, c.headline, "headline"))
    chunks = split_subtitle(c.subtitle) if subtitles else []
    if chunks:
        s, e = subtitle_window(c)
        to_tl = lambda sec: placed.start + _frames(sec, fps) - placed.in_frame  # noqa: E731
        t0, t1 = max(placed.start, to_tl(s)), min(placed.end, to_tl(e))
        weights = [max(len(ch), 1) for ch in chunks]
        total, acc = sum(weights), 0
        for ch, w in zip(chunks, weights, strict=True):
            a = t0 + round((t1 - t0) * acc / total)
            acc += w
            b = t0 + round((t1 - t0) * acc / total)
            if b > a:
                out.append(Cue(a, b, ch, "subtitle"))
    return out


# ---------------------------------------------------------------- 剪辑方案


class EditPlan(BaseModel):
    edit_id: str = Field(default_factory=lambda: new_id("edit"))
    script_id: str
    editor_skill_id: str
    aspect_ratio: AspectRatio
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: int = EDIT_FPS
    title: str = ""
    edit_summary: str = ""
    user_edit_idea: str | None = None
    clips: list[EditClip] = Field(default_factory=list)

    # ---- 成片选项 ----
    burn_subtitles: bool = True
    burn_headlines: bool = True
    bgm_path: Path | None = None
    bgm_duration: float | None = None  # 背景音乐时长（秒），导出达芬奇时间线时用于循环铺满
    bgm_volume: float = Field(default=0.18, ge=0, le=1)
    duck_bgm: bool = True  # 人物说话时自动压低背景音乐

    renders: list[RenderedCut] = Field(default_factory=list)
    edit_log: list[EditRecord] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    def _log(self, action: str, target_id: str | None = None, **details: Any) -> None:
        self.edit_log.append(EditRecord(action=action, target_id=target_id, details=details))

    @property
    def enabled_clips(self) -> list[EditClip]:
        return [c for c in self.clips if c.enabled]

    def get_clip(self, clip_id: str) -> EditClip:
        for c in self.clips:
            if c.clip_id == clip_id:
                return c
        raise KeyError(f"找不到片段：{clip_id}")

    def update_clip(self, clip_id: str, /, **changes: Any) -> EditClip:
        """修改片段字段：重新构造并校验，非法修改不会写入。"""
        blocked = changes.keys() & {"clip_id", "shot_id", "shot_index", "video_id", "source_path",
                                    "source_duration", "width", "height", "has_audio"}
        if blocked:
            raise ValueError(f"以下字段不能手工修改：{', '.join(sorted(blocked))}")
        old = self.get_clip(clip_id)
        new = EditClip.model_validate({**old.model_dump(), **changes})
        self.clips[self.clips.index(old)] = new
        self._log("update_clip", clip_id, fields=sorted(changes))
        return new

    def reorder(self, clip_ids: list[str]) -> None:
        if sorted(clip_ids) != sorted(c.clip_id for c in self.clips):
            raise ValueError("重新排序必须包含全部片段")
        by_id = {c.clip_id: c for c in self.clips}
        self.clips = [by_id[i] for i in clip_ids]
        self._log("reorder")

    def set_options(self, *, burn_subtitles: bool | None = None, burn_headlines: bool | None = None,
                    bgm_volume: float | None = None, duck_bgm: bool | None = None) -> None:
        data = self.model_dump()
        for k, v in {"burn_subtitles": burn_subtitles, "burn_headlines": burn_headlines,
                     "bgm_volume": bgm_volume, "duck_bgm": duck_bgm}.items():
            if v is not None:
                data[k] = v
        checked = EditPlan.model_validate(data)
        self.burn_subtitles, self.burn_headlines = checked.burn_subtitles, checked.burn_headlines
        self.bgm_volume, self.duck_bgm = checked.bgm_volume, checked.duck_bgm

    def set_bgm(self, path: Path | str | None, duration: float | None = None) -> None:
        self.bgm_path = Path(path) if path else None
        self.bgm_duration = duration if path else None
        self._log("set_bgm", path=str(self.bgm_path or ""), duration=self.bgm_duration)

    def add_render(self, path: Path, duration_seconds: float) -> RenderedCut:
        cut = RenderedCut(file_path=path, duration_seconds=round(duration_seconds, 2))
        self.renders.append(cut)
        self._log("render", cut.render_id)
        return cut

    @property
    def latest_render(self) -> RenderedCut | None:
        found = [r for r in self.renders if r.file_path.exists()]
        return found[-1] if found else None

    def require_renderable(self) -> list[EditClip]:
        clips = self.enabled_clips
        if not clips:
            raise ValueError("剪辑方案里没有启用的片段：请至少勾选一个镜头")
        missing = [c.shot_index for c in clips if not c.source_path.exists()]
        if missing:
            raise ValueError(f"镜头 {'、'.join(map(str, missing))} 的视频文件不存在，请重新生成剪辑方案")
        return clips

    def timeline(self) -> Timeline:
        return build_timeline(self.enabled_clips, self.fps)

    def cues(self, timeline: Timeline | None = None, subtitles: bool = True, headlines: bool = True) -> list[Cue]:
        tl = timeline or self.timeline()
        return [cue for p in tl.clips for cue in clip_cues(p, tl.fps, subtitles, headlines)]

    @property
    def total_seconds(self) -> float:
        return round(self.timeline().total / self.fps, 2)

    def sources(self) -> set[tuple[str, str]]:
        """方案引用的（分镜, 视频版本），用于判断分镜视频是否已更新。"""
        return {(c.shot_id, c.video_id) for c in self.clips}
