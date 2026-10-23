"""一键导出剪辑工程：剪映草稿（pyJianYingDraft）与 DaVinci Resolve 时间线（FCPXML 1.10 / OTIO + SRT 字幕）。

两种导出与一键成片使用同一条时间轴（EditPlan.timeline()），片段顺序、出入点、转场、字幕时间完全一致。
- 剪映：每次导出都新建一个草稿（剪映打开后会把草稿转存为自己的格式，旧草稿不再回读修改）；
  素材复制进草稿文件夹的 materials/，草稿与工作区解耦。
- 达芬奇：FCPXML 1.10 是 Resolve 18 起所有版本都接受的版本；OTIO 为 Resolve 18.5+ 的原生交换格式；
  台词字幕 / 卖点花字另附 SRT（Resolve 免费版可直接导入为字幕轨）。素材复制到 media/，文件名只用 ASCII，
  避免中文路径在 file:// 链接中的编码问题。
- 所有函数都是同步 IO（复制文件、读媒体信息），UI 层用 asyncio.to_thread 调用。
"""

from __future__ import annotations

import json
import math
import shutil
import time
import uuid
import zipfile
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from xml.sax.saxutils import escape

from app.engine.edit_render import caption_layout, safe_name
from app.schemas.edit import Cue, EditPlan, Timeline

US = 1_000_000
# 剪映文字：字号为剪映自身单位（默认 8），位置单位为半个画布高（正数向上）
JY_SUBTITLE_SIZE, JY_HEADLINE_SIZE = 7.0, 10.0
JY_TRANSITIONS = {"fade": "叠化", "fadeblack": "闪黑", "fadewhite": "闪白"}


class ExportError(RuntimeError):
    pass


def _stamp() -> str:
    return datetime.now().strftime("%m%d_%H%M%S")


def _copy_media(tl: Timeline, plan: EditPlan, media_dir: Path) -> tuple[dict[str, Path], Path | None]:
    """把用到的素材复制到 media_dir，返回 {clip_id: 新路径} 与背景音乐新路径。"""
    media_dir.mkdir(parents=True, exist_ok=True)
    copied: dict[str, Path] = {}
    for k, p in enumerate(tl.clips, start=1):
        dest = media_dir / f"shot{p.clip.shot_index:02d}_{k:02d}{p.clip.source_path.suffix.lower() or '.mp4'}"
        shutil.copy2(p.clip.source_path, dest)
        copied[p.clip.clip_id] = dest
    bgm = None
    if plan.bgm_path is not None and plan.bgm_path.exists():
        bgm = media_dir / f"bgm{plan.bgm_path.suffix.lower() or '.mp3'}"
        shutil.copy2(plan.bgm_path, bgm)
    return copied, bgm


def _zip_dir(src: Path, zip_path: Path, arc_root: str) -> Path:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                zf.write(f, f"{arc_root}/{f.relative_to(src).as_posix()}")
    return zip_path


def _require(plan: EditPlan) -> Timeline:
    plan.require_renderable()
    tl = plan.timeline()
    if tl.total <= 0:
        raise ExportError("成片时长为 0，请检查各片段的出入点")
    return tl


def _export_cues(plan: EditPlan, tl: Timeline) -> list[Cue]:
    return plan.cues(tl, subtitles=plan.burn_subtitles, headlines=plan.burn_headlines)


# ---------------------------------------------------------------- SRT


def _srt_time(frames: int, fps: int) -> str:
    ms = round(frames * 1000 / fps)
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(cues: list[Cue], fps: int) -> str:
    blocks = [f"{i}\n{_srt_time(c.start, fps)} --> {_srt_time(c.end, fps)}\n{c.text}\n"
              for i, c in enumerate(cues, start=1)]
    return "\n".join(blocks)


# ---------------------------------------------------------------- 剪映草稿


def _us(frames: int, fps: int) -> int:
    return round(frames * US / fps)


def fill_scale(src_w: int, src_h: int, dst_w: int, dst_h: int) -> float:
    """剪映默认把素材「适应」画布（可能留细黑边）；返回铺满画布需要的额外缩放倍数。"""
    if not (src_w and src_h):
        return 1.0
    fit = min(dst_w / src_w, dst_h / src_h)
    fill = max(dst_w / src_w, dst_h / src_h)
    return round(fill / fit, 4)


def _caption_offsets(plan: EditPlan) -> tuple[float, float]:
    """字幕 / 花字在剪映中的垂直位置（与成片 ASS 版式一致）。"""
    lay = caption_layout(plan.width, plan.height)
    h = plan.height
    sub_center = lay["sub_margin"] + lay["sub_size"] / 2
    head_center = lay["head_margin"] + lay["head_size"] / 2
    return round(-(1 - 2 * sub_center / h), 3), round(1 - 2 * head_center / h, 3)


def _fix_meta(draft_dir: Path, root: Path, total_us: int) -> None:
    """pyJianYingDraft 的元信息模板带固定的 draft_id：换成新 ID，并补上名称、路径与时长。"""
    meta_path = draft_dir / "draft_meta_info.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    now_us = int(time.time() * US)
    meta.update({
        "draft_id": str(uuid.uuid4()).upper(), "draft_name": draft_dir.name,
        "draft_fold_path": draft_dir.as_posix(), "draft_root_path": root.as_posix(),
        "tm_duration": total_us, "tm_draft_create": now_us, "tm_draft_modified": now_us,
    })
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def write_jianying_draft(plan: EditPlan, root: Path, draft_name: str | None = None) -> Path:
    """在 root（剪映草稿文件夹）下新建一个草稿，返回草稿文件夹路径。"""
    try:
        import pyJianYingDraft as jy
    except ImportError as e:  # pragma: no cover - 依赖缺失时的友好提示
        raise ExportError("缺少剪映草稿依赖：请运行 pip install pyJianYingDraft==0.3.1") from e
    tl = _require(plan)
    root = Path(root)
    if not root.is_dir():
        raise ExportError(f"剪映草稿文件夹不存在：{root}")
    base = safe_name(draft_name or f"{plan.title}_{_stamp()}", 40)
    name, n = base, 2
    while (root / name).exists():  # 绝不覆盖剪映里已有的草稿
        name, n = f"{base}_{n}", n + 1
    fps = plan.fps
    folder = jy.DraftFolder(str(root))
    script = folder.create_draft(name, plan.width, plan.height, fps=fps, allow_replace=False)
    draft_dir = root / name
    copied, bgm = _copy_media(tl, plan, draft_dir / "materials")

    cues = _export_cues(plan, tl)
    has_subs = any(c.kind == "subtitle" for c in cues)
    has_heads = any(c.kind == "headline" for c in cues)
    tracks = [jy.TrackSpec(jy.TrackType.video, "主轨")]
    if bgm is not None:
        tracks.insert(0, jy.TrackSpec(jy.TrackType.audio, "背景音乐"))
    if has_subs:
        tracks.append(jy.TrackSpec(jy.TrackType.text, "台词字幕"))
    if has_heads:
        tracks.append(jy.TrackSpec(jy.TrackType.text, "卖点花字"))
    script.append_tracks(tracks)

    joins_after = {j: join for j, join in enumerate(tl.joins)}
    for k, p in enumerate(tl.clips):
        start, end = _us(p.start, fps), _us(p.end, fps)
        material = jy.VideoMaterial(str(copied[p.clip.clip_id]))
        # 剪映按视频流时长校验截取范围（可能比容器时长短几毫秒）：越界时把入点前移，保持片段时长不变
        src_in = max(0, min(_us(p.in_frame, fps), material.duration - (end - start)))
        scale = fill_scale(p.clip.width, p.clip.height, plan.width, plan.height)
        seg = jy.VideoSegment(
            material, jy.Timerange(start, end - start),
            source_timerange=jy.Timerange(src_in, end - start), volume=p.clip.volume,
            clip_settings=jy.ClipSettings(scale_x=scale, scale_y=scale),
        )
        join = joins_after.get(k)
        if join is not None and join.kind != "cut":
            seg.add_transition(getattr(jy.TransitionType, JY_TRANSITIONS[join.kind]),
                               duration=_us(join.duration, fps))
        script.add_segment(seg, "主轨")

    sub_y, head_y = _caption_offsets(plan)
    sub_style = jy.TextStyle(size=JY_SUBTITLE_SIZE, bold=True, color=(1.0, 1.0, 1.0), align=1,
                             auto_wrapping=True, max_line_width=0.88)
    head_style = jy.TextStyle(size=JY_HEADLINE_SIZE, bold=True, color=(1.0, 0.88, 0.3), align=1,
                              auto_wrapping=True, max_line_width=0.88)
    for cue in cues:
        start, end = _us(cue.start, fps), _us(cue.end, fps)
        is_head = cue.kind == "headline"
        script.add_segment(jy.TextSegment(
            cue.text, jy.Timerange(start, end - start), style=head_style if is_head else sub_style,
            border=jy.TextBorder(color=(0.0, 0.0, 0.0), width=55.0 if is_head else 45.0),
            clip_settings=jy.ClipSettings(transform_y=head_y if is_head else sub_y),
        ), "卖点花字" if is_head else "台词字幕")

    total_us = _us(tl.total, fps)
    if bgm is not None:
        material = jy.AudioMaterial(str(bgm))
        t = 0
        while t < total_us and material.duration > 0:  # 音乐比成片短时首尾相接循环
            length = min(material.duration, total_us - t)
            seg = jy.AudioSegment(material, jy.Timerange(t, length), source_timerange=jy.Timerange(0, length),
                                  volume=plan.bgm_volume)
            fade_in = "0.8s" if t == 0 else 0
            fade_out = "1.5s" if t + length >= total_us else 0
            seg.add_fade(fade_in, fade_out)
            script.add_segment(seg, "背景音乐")
            t += length
    script.save()
    # 新版剪映读取 draft_info.json，旧版读取 draft_content.json：两份内容相同
    shutil.copyfile(draft_dir / "draft_content.json", draft_dir / "draft_info.json")
    _fix_meta(draft_dir, root, total_us)
    return draft_dir


JIANYING_README = """剪映草稿使用说明
================

1. 如果导出时已自动写入剪映草稿文件夹：重新打开剪映专业版，即可在「本地草稿」里看到「{name}」。
2. 如果下载的是压缩包：把压缩包里的「{name}」文件夹整个解压到剪映的草稿文件夹，
   位置见 剪映 → 设置（右上角）→ 全局设置 → 草稿位置，默认是
   C:\\Users\\你的用户名\\AppData\\Local\\JianyingPro\\User Data\\Projects\\com.lveditor.draft
   然后重新打开剪映。
3. 素材都在草稿文件夹的 materials 里。若剪映提示「素材丢失」，点「重新链接」并选择 materials 文件夹。
4. 时间线：主轨按成片顺序排好各镜头（已掐头去尾、含转场），「台词字幕」「卖点花字」为可编辑文字轨，
   背景音乐在音频轨。字幕位置、字体、花字样式都可以在剪映里继续调整。
"""


def export_jianying(plan: EditPlan, exports_dir: Path, jianying_root: Path | None = None) -> tuple[Path, Path | None]:
    """导出剪映草稿并打包下载。

    jianying_root 存在时直接写进剪映草稿文件夹（打开剪映即可看到）；否则写在 exports_dir/jianying/。
    返回（zip 压缩包, 已写入剪映文件夹的草稿路径或 None）。
    """
    installed = jianying_root if jianying_root is not None and Path(jianying_root).is_dir() else None
    root = installed or (exports_dir / "jianying")
    root.mkdir(parents=True, exist_ok=True)
    draft_dir = write_jianying_draft(plan, root)
    (draft_dir / "使用说明.txt").write_text(JIANYING_README.format(name=draft_dir.name), encoding="utf-8")
    zip_path = _zip_dir(draft_dir, exports_dir / f"剪映草稿_{draft_dir.name}.zip", draft_dir.name)
    return zip_path, draft_dir if installed else None


# ---------------------------------------------------------------- DaVinci Resolve：FCPXML 1.10


def _t(frames: int, fps: int) -> str:
    """FCPXML 有理数时间：帧数 / 帧率，写成 N/Ds（整秒写成 Ns）。"""
    v = Fraction(frames, fps)
    return f"{v.numerator}s" if v.denominator == 1 else f"{v.numerator}/{v.denominator}s"


def _q(text: object) -> str:
    return escape(str(text), {'"': "&quot;"})


def _db(volume: float) -> str:
    return "-96dB" if volume <= 0 else f"{20 * math.log10(volume):.1f}dB"


def _media_frames(seconds: float, fps: int) -> int:
    return math.floor(seconds * fps + 1e-6)


def build_fcpxml(plan: EditPlan, tl: Timeline, media: dict[str, Path], bgm: Path | None,
                 bgm_seconds: float | None, cues: list[Cue]) -> str:
    fps = plan.fps
    res: list[str] = []
    formats: dict[tuple[int, int], str] = {}
    assets: dict[Path, str] = {}

    def fmt(w: int, h: int) -> str:
        if (w, h) not in formats:
            fid = f"r{len(formats) + len(assets) + 1}"
            formats[(w, h)] = fid
            res.append(f'    <format id="{fid}" name="FFVideoFormat{h}p{fps}" frameDuration="{_t(1, fps)}" '
                       f'width="{w}" height="{h}" colorSpace="1-1-1 (Rec. 709)"/>')
        return formats[(w, h)]

    def asset(path: Path, name: str, frames: int, has_video: bool, has_audio: bool, fid: str = "") -> str:
        if path not in assets:
            aid = f"r{len(formats) + len(assets) + 1}"
            assets[path] = aid
            video = f' hasVideo="1" videoSources="1" format="{fid}"' if has_video else ""
            audio = ' hasAudio="1" audioSources="1" audioChannels="2"' if has_audio else ""
            res.append(f'    <asset id="{aid}" name="{_q(name)}" start="0s" '
                       f'duration="{_t(frames, fps)}"{video}{audio}>\n'
                       f'      <media-rep kind="original-media" src="{_q(path.resolve().as_uri())}"/>\n'
                       f"    </asset>")
        return assets[path]

    seq_fmt = fmt(plan.width, plan.height)
    spine: list[str] = []
    heads = [c for c in cues if c.kind == "headline"]
    for k, p in enumerate(tl.clips):
        c = p.clip
        name = f"镜头{c.shot_index:02d}"
        has_video = bool(c.width and c.height)
        fid = fmt(c.width, c.height) if has_video else seq_fmt
        aid = asset(media[c.clip_id], name, _media_frames(c.source_duration, fps), has_video, c.has_audio, fid)
        if k > 0 and (join := tl.joins[k - 1]).kind != "cut":
            spine.append(f'        <transition name="Cross Dissolve" offset="{_t(join.cut - join.before, fps)}" '
                         f'duration="{_t(join.duration, fps)}"/>')
        inner: list[str] = []
        # FCPXML 1.10 DTD 顺序：adjust-conform → adjust-volume → 连接片段（音乐）→ 标记
        if has_video and c.width * plan.height != c.height * plan.width:
            inner.append('          <adjust-conform type="fill"/>')
        if abs(c.volume - 1.0) > 1e-6:
            inner.append(f'          <adjust-volume amount="{_db(c.volume)}"/>')
        if k == 0 and bgm is not None and bgm_seconds:
            bid = asset(bgm, "背景音乐", _media_frames(bgm_seconds, fps), False, True)
            pos, total, bgm_frames = 0, tl.total, _media_frames(bgm_seconds, fps)
            while pos < total and bgm_frames > 0:  # 音乐比成片短时首尾相接循环
                length = min(bgm_frames, total - pos)
                inner.append(f'          <asset-clip ref="{bid}" lane="-1" offset="{_t(p.in_frame + pos, fps)}" '
                             f'name="背景音乐" start="0s" duration="{_t(length, fps)}" audioRole="music">\n'
                             f'            <adjust-volume amount="{_db(plan.bgm_volume)}"/>\n'
                             f"          </asset-clip>")
                pos += length
        for cue in heads:
            if p.start <= cue.start < p.end:
                inner.append(f'          <marker start="{_t(p.in_frame + cue.start - p.start, fps)}" '
                             f'duration="{_t(1, fps)}" value="{_q("花字：" + cue.text)}"/>')
        head = (f'        <asset-clip ref="{aid}" offset="{_t(p.start, fps)}" name="{_q(name)}" '
                f'start="{_t(p.in_frame, fps)}" duration="{_t(p.length, fps)}" format="{fid}" tcFormat="NDF"')
        spine.append(head + (">\n" + "\n".join(inner) + "\n        </asset-clip>" if inner else "/>"))
    title = _q(plan.title or "带货短视频")
    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>', "<!DOCTYPE fcpxml>", '<fcpxml version="1.10">',
        "  <resources>", *res, "  </resources>", "  <library>", f'    <event name="{title}">',
        f'      <project name="{title}">',
        f'        <sequence format="{seq_fmt}" duration="{_t(tl.total, fps)}" tcStart="0s" tcFormat="NDF" '
        f'audioLayout="stereo" audioRate="48k">',
        "          <spine>", *spine, "          </spine>", "        </sequence>", "      </project>", "    </event>",
        "  </library>", "</fcpxml>", "",
    ])


# ---------------------------------------------------------------- DaVinci Resolve：OTIO


def _rt(frames: int, fps: int) -> dict:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": float(fps), "value": float(frames)}


def _range(start: int, dur: int, fps: int) -> dict:
    return {"OTIO_SCHEMA": "TimeRange.1", "start_time": _rt(start, fps), "duration": _rt(dur, fps)}


def _otio_clip(name: str, path: Path, start: int, dur: int, media_frames: int, fps: int) -> dict:
    # Clip.1（单个 media_reference）：新旧版本的 OTIO 都能读取
    return {"OTIO_SCHEMA": "Clip.1", "name": name, "metadata": {}, "enabled": True,
            "source_range": _range(start, dur, fps), "effects": [], "markers": [],
            "media_reference": {"OTIO_SCHEMA": "ExternalReference.1", "name": name, "metadata": {},
                                "target_url": path.resolve().as_uri(),
                                "available_range": _range(0, media_frames, fps)}}


def _otio_track(name: str, kind: str, children: list[dict]) -> dict:
    return {"OTIO_SCHEMA": "Track.1", "name": name, "kind": kind, "metadata": {}, "enabled": True,
            "source_range": None, "effects": [], "markers": [], "children": children}


def build_otio(plan: EditPlan, tl: Timeline, media: dict[str, Path], bgm: Path | None,
               bgm_seconds: float | None, cues: list[Cue]) -> str:
    fps = plan.fps

    def lane(with_audio_only: bool) -> list[dict]:
        out: list[dict] = []
        for k, p in enumerate(tl.clips):
            c = p.clip
            if k > 0 and (join := tl.joins[k - 1]).kind != "cut":
                out.append({"OTIO_SCHEMA": "Transition.1", "name": "Cross Dissolve", "metadata": {},
                            "transition_type": "SMPTE_Dissolve",
                            # in_offset：剪辑点之前的部分；out_offset：剪辑点之后的部分
                            "in_offset": _rt(join.before, fps), "out_offset": _rt(join.after, fps)})
            if with_audio_only and not c.has_audio:
                out.append({"OTIO_SCHEMA": "Gap.1", "name": "", "metadata": {}, "enabled": True,
                            "source_range": _range(0, p.length, fps), "effects": [], "markers": []})
                continue
            out.append(_otio_clip(f"镜头{c.shot_index:02d}", media[c.clip_id], p.in_frame, p.length,
                                  _media_frames(c.source_duration, fps), fps))
        return out

    tracks = [_otio_track("V1", "Video", lane(False)), _otio_track("A1 原声", "Audio", lane(True))]
    if bgm is not None and bgm_seconds:
        bgm_frames, pos, music = _media_frames(bgm_seconds, fps), 0, []
        while pos < tl.total and bgm_frames > 0:
            length = min(bgm_frames, tl.total - pos)
            music.append(_otio_clip("背景音乐", bgm, 0, length, bgm_frames, fps))
            pos += length
        tracks.append(_otio_track("A2 背景音乐", "Audio", music))
    markers = [{"OTIO_SCHEMA": "Marker.2", "name": f"花字：{c.text}", "metadata": {}, "color": "YELLOW",
                "comment": "", "marked_range": _range(c.start, 0, fps)} for c in cues if c.kind == "headline"]
    doc = {"OTIO_SCHEMA": "Timeline.1", "name": plan.title or "带货短视频", "metadata": {},
           "global_start_time": _rt(0, fps),
           "tracks": {"OTIO_SCHEMA": "Stack.1", "name": "tracks", "metadata": {}, "enabled": True,
                      "source_range": None, "effects": [], "markers": markers, "children": tracks}}
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


RESOLVE_README = """DaVinci Resolve 工程导入说明
==========================

文件：
- {name}.fcpxml        时间线（推荐，DaVinci Resolve 18 及以上）
- {name}.otio          时间线（备选，DaVinci Resolve 18.5 及以上）
- 台词字幕.srt / 卖点花字.srt   字幕文件（可选）
- media/               所有素材（镜头视频与背景音乐）

步骤：
1. 在生成这个工程的电脑上，可以直接导入界面里显示的工程文件夹（素材已经指向其中的 media 文件夹）；
   拷到其他电脑时，把压缩包解压到一个固定位置，导入后按第 5 步重新链接一次素材。
2. 打开 DaVinci Resolve，新建项目；在 文件 → 项目设置 → 主设置 中把时间线分辨率设为 {width}×{height}、
   时间线帧率设为 {fps}（导入前设置，避免剪辑点错位）。
3. 文件 → 导入 → 时间线（Import Timeline），选择 {name}.fcpxml；导入对话框中保持勾选
   「自动将时间线中的源片段导入媒体池」。fcpxml 导入失败时改选 {name}.otio。
4. 字幕：文件 → 导入 → 字幕，选择「台词字幕.srt」（以及「卖点花字.srt」），再从媒体池拖到时间线上，
   会生成字幕轨，可在检查器里修改字体与位置。卖点花字也以时间线标记的形式写在了对应镜头上。
5. 若素材显示离线：在媒体池选中全部片段 → 右键「重新链接所选片段」→ 选择 media 文件夹。
6. 转场在达芬奇中统一为「交叉叠化」，可在特效库里替换成其他转场。
"""


def export_resolve(plan: EditPlan, exports_dir: Path) -> tuple[Path, Path]:
    """导出 DaVinci Resolve 工程文件夹并打包。返回（zip 压缩包, 工程文件夹）。"""
    tl = _require(plan)
    bundle = exports_dir / f"resolve_{_stamp()}"
    media, bgm = _copy_media(tl, plan, bundle / "media")
    cues = plan.cues(tl, subtitles=True, headlines=True)
    name = safe_name(plan.title, 30)
    secs = plan.bgm_duration
    (bundle / f"{name}.fcpxml").write_text(build_fcpxml(plan, tl, media, bgm, secs, cues), encoding="utf-8")
    (bundle / f"{name}.otio").write_text(build_otio(plan, tl, media, bgm, secs, cues), encoding="utf-8")
    subs = [c for c in cues if c.kind == "subtitle"]
    heads = [c for c in cues if c.kind == "headline"]
    if subs:  # utf-8-sig：带 BOM，Windows 上的剪辑软件识别中文更稳
        (bundle / "台词字幕.srt").write_text(build_srt(subs, plan.fps), encoding="utf-8-sig")
    if heads:
        (bundle / "卖点花字.srt").write_text(build_srt(heads, plan.fps), encoding="utf-8-sig")
    (bundle / "导入说明.txt").write_text(
        RESOLVE_README.format(name=name, width=plan.width, height=plan.height, fps=plan.fps), encoding="utf-8")
    zip_path = _zip_dir(bundle, exports_dir / f"达芬奇工程_{name}_{bundle.name.removeprefix('resolve_')}.zip", name)
    return zip_path, bundle
