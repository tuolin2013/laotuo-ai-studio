"""渲染自检：联系表（每个分镜落定后 0.3 秒各取一帧）+ 逐项规则检查，生成给人看的自检报告。

检查项与 config/skills/motion_director/references/motion_design_principles.md 第 10 节一致：
节拍对齐、黄金 3 秒钩子、收尾停留、阅读时间、文字停稳、安全区、文字互相遮挡、转场种类、
第 0 帧不黑、画面突跳 / 单帧闪烁、整片响度与真峰值、字号下限、读完不干等、版式不连续重复。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from app.engine.motion.renderer import MotionRenderer
from app.engine.motion.typeset import font
from app.schemas.motion import (
    CTA_MIN_SECONDS,
    HOOK_MAX_SECONDS,
    MOTION_TRANSITION_LABELS,
    READING_CHARS_PER_SECOND,
)

# 画面突跳：相邻帧差异是前后各 4 帧中位数的 3 倍以上，且超过该阈值（48×48 灰度缩略图，0~255）
POP_RATIO = 3.0
POP_MIN = 10.0
MIN_HOLD_SECONDS = 0.5
TARGET_LUFS = -14.0
# 字号下限：画面短边的 2.6%（1080P 约 28 像素，手机竖屏全屏播放时还能看清）
TEXT_FLOOR = 0.026
# 画面里没有东西在动的屏，读完之后最多再停这么久（再长就是「干等」，观众会划走）
IDLE_SECONDS = 3.0


@dataclass
class Check:
    ok: bool
    title: str
    detail: str = ""


@dataclass
class Review:
    checks: list[Check] = field(default_factory=list)

    def add(self, ok: bool, title: str, detail: str = "") -> None:
        self.checks.append(Check(ok, title, detail))

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if not c.ok)

    def markdown(self) -> str:
        passed = len(self.checks) - self.warnings
        head = (f"### 🔍 自检报告：{passed}/{len(self.checks)} 项通过"
                + ("" if not self.warnings else f"，{self.warnings} 项需要留意"))
        lines = [f"- {'✅' if c.ok else '⚠️'} **{c.title}**" + (f"：{c.detail}" if c.detail else "")
                 for c in self.checks]
        return head + "\n\n" + "\n".join(lines)


def sample_frames(r: MotionRenderer) -> list[tuple[int, str]]:
    """联系表取帧：第 0 帧 + 每个分镜内容落定后 0.3 秒（不晚于退场开始）。"""
    out = [(0, "第 0 帧")]
    for i, (sp, sc) in enumerate(zip(r.spans, r.scenes, strict=True)):
        lt = min(sc.settled_at + 0.3, sp.seconds - r.exit_s[i] - 1 / r.fps)
        f = sp.start + max(0, round(lt * r.fps))
        out.append((min(f, sp.end - 1), f"{i + 1}. {sp.scene.label} · {sp.start_s + lt:.1f}s"))
    return out


def contact_sheet(frames: list[tuple[Image.Image, str]], path: Path, cols: int = 4, thumb_w: int = 300) -> Path:
    if not frames:
        raise ValueError("没有可以放进联系表的画面")
    w0, h0 = frames[0][0].size
    tw, th = thumb_w, round(thumb_w * h0 / w0)
    label_h, gap = 34, 12
    rows = -(-len(frames) // cols)
    sheet = Image.new("RGB", (cols * tw + (cols + 1) * gap, rows * (th + label_h) + (rows + 1) * gap), (18, 18, 22))
    draw = ImageDraw.Draw(sheet)
    fnt = font(18, "regular")
    for k, (img, label) in enumerate(frames):
        r, c = divmod(k, cols)
        x, y = gap + c * (tw + gap), gap + r * (th + label_h + gap)
        sheet.paste(img.resize((tw, th), Image.Resampling.LANCZOS), (x, y))
        draw.text((x + 2, y + th + 6), label, font=fnt, fill=(210, 210, 216))
    sheet.save(path)
    return path


def _overlap(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def find_pops(thumbs: list[np.ndarray], expected: set[int], cuts: set[int]) -> list[int]:
    """返回出现突跳 / 单帧闪烁的帧号（剪辑点与转场帧除外）。"""
    if len(thumbs) < 3:
        return []
    diffs = np.array([float(np.abs(thumbs[i] - thumbs[i - 1]).mean()) for i in range(1, len(thumbs))])
    bad = []
    for i, d in enumerate(diffs, start=1):
        if i in cuts or i in expected or d < POP_MIN:
            continue
        lo, hi = max(0, i - 5), min(len(diffs), i + 4)
        neigh = np.delete(diffs[lo:hi], i - 1 - lo)
        if len(neigh) and d > POP_RATIO * max(float(np.median(neigh)), 1.0):
            bad.append(i)
    return bad


def review(r: MotionRenderer, thumbs: list[np.ndarray] | None = None, loudness: tuple[float, float] | None = None
           ) -> Review:
    rv = Review()
    ad, fps = r.ad, r.fps
    spans = r.spans
    # 1. 节拍：每个剪辑点离最近的拍点不超过半帧
    beat = 60 / ad.bpm
    off = [abs(sp.start / fps / beat - round(sp.start / fps / beat)) * beat for sp in spans]
    rv.add(max(off) <= 0.5 / fps + 1e-6, "剪辑点全部落在节拍上",
           f"{ad.bpm} BPM，一拍 {beat:.3f} 秒，共 {len(spans)} 个分镜、{len(spans) - 1} 个剪辑点")
    # 2. 黄金 3 秒
    first = spans[0]
    if first.scene.kind == "hook":
        rv.add(first.seconds <= HOOK_MAX_SECONDS + 1e-6, "黄金 3 秒钩子", f"开场大字 {first.seconds:.2f} 秒"
               + ("" if first.seconds <= HOOK_MAX_SECONDS + 1e-6 else "，超过 3 秒，建议减少拍数"))
    else:
        rv.add(False, "黄金 3 秒钩子", "第一个分镜不是「钩子大字」，开场 3 秒可能留不住人")
    # 3. 收尾停留
    last = spans[-1]
    if last.scene.kind == "cta":
        rv.add(last.seconds >= CTA_MIN_SECONDS - 1e-6, "收尾行动停留 ≥ 3 秒", f"{last.seconds:.2f} 秒")
    else:
        rv.add(False, "收尾行动停留 ≥ 3 秒", "最后一个分镜不是「收尾行动」")
    # 4. 阅读时间 + 5. 文字停稳
    short, unsettled = [], []
    for i, (sp, sc) in enumerate(zip(spans, r.scenes, strict=True)):
        hold = r.hold_seconds(i)
        visible = hold + sc.settled_at - max(0.0, sc.t0 - sc.lead)  # 剪辑点之前的入场看不见，不算
        if visible + 1e-6 < sp.scene.reading_seconds:
            short.append(f"{i + 1}. {sp.scene.label}（需要 {sp.scene.reading_seconds:.1f} 秒，实际 {visible:.1f} 秒）")
        if hold < MIN_HOLD_SECONDS:
            unsettled.append(f"{i + 1}. {sp.scene.label}（落定后只停 {max(hold, 0):.2f} 秒）")
    rv.add(not short, "每屏文字都有足够的阅读时间",
           "；".join(short) or f"按每秒 {READING_CHARS_PER_SECOND:g} 个字 + 入场时间计算")
    rv.add(not unsettled, "文字停稳后再退场", "；".join(unsettled) or f"每屏落定后至少停留 {MIN_HOLD_SECONDS} 秒")
    # 6. 安全区 + 7. 遮挡
    safe = r.ctx.safe
    tol = 2 * r.ctx.u
    outside, overlaps = [], []
    for i, sc in enumerate(r.scenes):
        boxes = sc.text_boxes
        if any(b[0] < safe.x0 - tol or b[1] < safe.y0 - tol or b[2] > safe.x1 + tol or b[3] > safe.y1 + tol
               for b in boxes):
            outside.append(f"{i + 1}. {sc.scene.label}")
        if any(_overlap(a, b) for k, a in enumerate(boxes) for b in boxes[k + 1:]):
            overlaps.append(f"{i + 1}. {sc.scene.label}")
    area = "竖屏中间 75% 宽度、避开顶部搜索栏与底部文案区" if r.ctx.portrait else "四周留出安全边距"
    rv.add(not outside, "文字都在安全区内", "、".join(outside) + " 有文字超出安全区" if outside else area)
    rv.add(not overlaps, "文字互不遮挡", "、".join(overlaps) + " 的文字互相重叠" if overlaps else "")
    # 8. 转场种类
    kinds = {sp.scene.transition_out for sp in spans[:-1]} - {"cut"}
    names = "、".join(MOTION_TRANSITION_LABELS[k] for k in sorted(kinds)) or "全部卡点硬切"
    rv.add(len(kinds) <= 3, "转场种类克制（≤3 种）", names)
    # 9. 第 0 帧 + 10. 突跳
    if thumbs:
        peak = float(thumbs[0].max())
        rv.add(peak > 24, "第 0 帧不是黑屏", f"最亮处 {peak:.0f}/255")
        cuts = {sp.start for sp in spans}
        pops = find_pops(thumbs, r.transition_frames(), cuts)
        rv.add(not pops, "没有画面突跳或单帧闪烁",
               "、".join(f"{f / fps:.2f}s" for f in pops[:8]) + " 附近有突跳" if pops else
               f"逐帧比对 {len(thumbs)} 帧（剪辑点与转场除外）")
    # 11. 响度
    if loudness is not None:
        lufs, tp = loudness
        rv.add(abs(lufs - TARGET_LUFS) <= 1.5 and tp <= -0.5, "整片响度统一",
               f"{lufs:.1f} LUFS（目标 {TARGET_LUFS:g}），真峰值 {tp:.1f} dBTP")
    # 12. 字号下限：最小的字也不低于画面短边的 2.6%
    floor = TEXT_FLOOR * min(r.W, r.H)
    tiny = [f"{i + 1}. {sc.scene.label}（{min(sc.font_px)} 像素）" for i, sc in enumerate(r.scenes)
            if sc.font_px and min(sc.font_px) + 0.5 < floor]
    rv.add(not tiny, "文字在手机上看得清",
           "；".join(tiny) + f" 低于下限 {floor:.0f} 像素，建议删减文字" if tiny else
           f"最小字号不低于画面短边的 {TEXT_FLOOR:.1%}（{floor:.0f} 像素）")
    # 13. 读完不干等：画面里没有东西在动的屏（纯文字、静止镜头），读完之后不再停超过 3 秒
    idle = []
    for i, (sp, sc) in enumerate(zip(spans, r.scenes, strict=True)):
        end = r.hold_seconds(i) + sc.settled_at  # 退场开始的时间
        spare = end - max(sp.scene.reading_seconds, sc.settled_at + MIN_HOLD_SECONDS)
        if not sc.alive and spare > IDLE_SECONDS + 1e-6:
            idle.append(f"{i + 1}. {sp.scene.label}（读完后还停 {spare:.1f} 秒，可减 {math.floor(spare / beat)} 拍）")
    rv.add(not idle, "读完就走，不干等", "；".join(idle) or f"静止的画面读完后最多再停 {IDLE_SECONDS:g} 秒")
    # 14. 版式不连续重复：相邻两屏是同一类版式时，观众会以为画面卡住了
    repeats = [f"{i + 1}、{i + 2}. {a.scene.label}" for i, (a, b) in enumerate(pairwise(spans))
               if a.scene.kind == b.scene.kind]
    rv.add(not repeats, "相邻两屏版式不重复", "；".join(repeats) + " 连续两屏同一版式" if repeats else "")
    return rv
