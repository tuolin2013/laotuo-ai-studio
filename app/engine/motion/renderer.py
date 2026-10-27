"""逐帧渲染器：连续的环境背景 + 分镜层 + 动势衔接转场 + 暗角颗粒，逐帧写进 ffmpeg 编码。

- 背景是整支片子共用的一层（渐变 + 三团缓慢漂移的柔光），分镜在它上面切换，画面始终是「同一个世界」；
- 转场按「动势衔接」（cut-the-curve）：上一场 power4.in 加速离开、在剪辑点硬切、下一场 power4.out 接住速度，
  方向与轴向一致（竖屏向上、横屏向左）；穿梭缩放两侧都是放大方向；快速位移时按 180° 快门加运动模糊，
  模糊绝不跨越剪辑点；
- 片尾最后 0.45 秒内容收回，只留背景，声音同步淡出。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass

import numpy as np
from PIL import Image

from app.engine.motion import easing as ez
from app.engine.motion.compose import (
    directional_blur,
    fast_blur,
    finish_overlay,
    paste,
    radial_glow,
    scale_layer,
    shift_layer,
    vertical_gradient,
    with_opacity,
)
from app.engine.motion.media import Media
from app.engine.motion.scenes import RENDERERS, Ctx, SceneRenderer
from app.engine.motion.theme import get_theme
from app.engine.motion.typeset import hex_rgb
from app.schemas.motion import MotionAd

# 转场：剪辑点之前的退场时长 / 之后的入场时长（秒）
EXIT_SECONDS = {"cut": 0.0, "push": 0.3, "zoom": 0.24, "wipe": 0.3}
ENTRY_SECONDS = {"cut": 0.0, "push": 0.5, "zoom": 0.55, "wipe": 0.34}
PUSH_TRAVEL = 0.12  # 动势推移的位移：画面对应边长的 12%
OUTRO_SECONDS = 0.45
GRAIN_PATTERNS = 6
THUMB = (48, 48)


def even(v: float) -> int:
    v = max(2, round(v))
    return v - v % 2


@dataclass(frozen=True)
class SoundEvent:
    time: float
    kind: str  # thump / tick / pop / shimmer / whoosh / riser / impact
    gain: float = 0.15
    pan: float = 0.0
    step: int = 0
    length: float = 0.0


class Background:
    """1/4 分辨率合成后放大：柔光本来就是模糊的，低分辨率合成画质无差别、速度快十几倍。"""

    def __init__(self, ctx: Ctx):
        th = ctx.theme
        self.W, self.H = ctx.W, ctx.H
        self.sw, self.sh = max(16, ctx.W // 4), max(16, ctx.H // 4)
        self.base = vertical_gradient(self.sw, self.sh, th.bg_top, th.bg_bottom)
        g = round(max(self.sw, self.sh) * 1.05)
        self.glows = [radial_glow(g, c, th.glow_opacity) for c in th.glows]
        # 每团光的漂移：周期 20~28 秒，振幅约 18% 画面（环境层运动量远小于主体）
        self.paths = [(0.28, 0.30, 23.0, 0.0), (0.72, 0.62, 27.0, 2.1), (0.45, 0.85, 20.0, 4.2)]

    def frame(self, t: float) -> Image.Image:
        img = self.base.copy()
        for glow, (bx, by, period, phase) in zip(self.glows, self.paths, strict=False):
            a = 2 * math.pi * t / period + phase
            x = self.sw * (bx + 0.18 * math.sin(a))
            y = self.sh * (by + 0.14 * math.cos(a * 0.8))
            paste(img, glow, x - glow.width / 2, y - glow.height / 2)
        return img.resize((self.W, self.H), Image.Resampling.BILINEAR)


class MotionRenderer:
    def __init__(self, ad: MotionAd, media: dict[str, Media], *, scale: float = 1.0, grain: bool | None = None):
        self.ad = ad
        self.W, self.H = even(ad.width * scale), even(ad.height * scale)
        self.fps = ad.fps
        self.theme = get_theme(ad.theme)
        self.ctx = Ctx(self.W, self.H, self.fps, self.theme, media, beat=60.0 / ad.bpm)
        self.spans = ad.timeline()
        if not self.spans:
            raise ValueError("还没有动效分镜")
        n = len(self.spans)
        self.scenes: list[SceneRenderer] = [RENDERERS[sp.scene.kind](self.ctx, sp.scene, sp.seconds, sp.index, n)
                                            for sp in self.spans]
        # 第一屏从落定状态开场：平台和播放器默认拿第 0 帧当封面，逐字入场的第 0 帧只露出半个字。
        # 跳过第一屏的入场（时间整体后移），镜头运动按后移后的时长继续，文字从第 0 帧起完整可读
        first = self.scenes[0]
        first.lead = max(0.0, first.settle) + 1e-3
        first.D += first.lead
        self.starts = [sp.start for sp in self.spans]
        self.total = self.spans[-1].end
        self.bg = Background(self.ctx)
        self.grain = ad.grain if grain is None else grain
        # 每个分镜的入场 / 退场转场；退场不超过分镜长度的 25%，入场不超过 35%
        self.trans_in = ["cut", *[sp.scene.transition_out for sp in self.spans[:-1]]]
        self.trans_out = [sp.scene.transition_out for sp in self.spans[:-1]] + ["cut"]
        self.exit_s = [min(EXIT_SECONDS[k], sp.seconds * 0.25) for k, sp in zip(self.trans_out, self.spans,
                                                                                    strict=True)]
        self.entry_s = [min(ENTRY_SECONDS[k], sp.seconds * 0.35) for k, sp in zip(self.trans_in, self.spans,
                                                                                     strict=True)]
        self.axis = "y" if self.ctx.portrait else "x"

    # ---------------------------------------------------------------- 时间

    @property
    def seconds(self) -> float:
        return self.total / self.fps

    def scene_at(self, f: int) -> int:
        return max(0, min(len(self.spans) - 1, bisect_right(self.starts, f) - 1))

    def hold_seconds(self, i: int) -> float:
        """分镜内容全部落定之后、退场开始之前的静止阅读时间。"""
        sc = self.scenes[i]
        end = self.spans[i].seconds - (self.exit_s[i] if i < len(self.spans) - 1 else OUTRO_SECONDS)
        return round(end - sc.settled_at, 2)

    def transition_frames(self) -> set[int]:
        """转场与片尾收回占用的帧（自检时这些帧的画面突变是预期之内的）。"""
        out: set[int] = set()
        for i, sp in enumerate(self.spans):
            if self.trans_in[i] != "cut":
                out.update(range(sp.start, sp.start + math.ceil(self.entry_s[i] * self.fps) + 1))
            if i < len(self.spans) - 1 and self.trans_out[i] != "cut":
                out.update(range(sp.end - math.ceil(self.exit_s[i] * self.fps) - 1, sp.end))
        out.update(range(self.total - math.ceil(OUTRO_SECONDS * self.fps) - 1, self.total))
        return out

    # ---------------------------------------------------------------- 转场

    def _push_offset(self, phase: str, p: float) -> float:
        travel = PUSH_TRAVEL * (self.H if self.axis == "y" else self.W)
        return -travel * ez.POWER4_IN(p) if phase == "out" else travel * (1 - ez.POWER4_OUT(p))

    def _transform(self, i: int, lt: float) -> tuple[str, str, float] | None:
        """(转场类型, out / in / outro, 进度)；没有转场时返回 None。"""
        d = self.spans[i].seconds
        if i == len(self.spans) - 1 and lt > d - OUTRO_SECONDS:
            return "outro", "outro", ez.progress(lt, d - OUTRO_SECONDS, OUTRO_SECONDS)
        k_in, e_in = self.trans_in[i], self.entry_s[i]
        if k_in != "cut" and lt < e_in:
            return k_in, "in", ez.progress(lt, 0, e_in)
        k_out, e_out = self.trans_out[i], self.exit_s[i]
        if k_out != "cut" and lt >= d - e_out:
            return k_out, "out", ez.progress(lt, d - e_out, e_out)
        return None

    def _apply(self, layer: Image.Image, kind: str, phase: str, p: float, dt: float) -> tuple[Image.Image, float]:
        """对分镜层做转场变换，返回 (变换后的层, 不透明度)。dt 为一帧对应的进度增量（算运动模糊）。"""
        u = self.ctx.u
        if kind == "outro":
            s = 1 - 0.02 * ez.EASE_IN(p)
            return scale_layer(layer, s), 1 - ez.EASE_IN(p)
        if kind == "push":
            off = self._push_offset(phase, p)
            prev = self._push_offset(phase, max(0.0, p - dt))
            v = abs(off - prev)
            if v > 8:  # 180° 快门：模糊长度约为每帧位移的一半
                layer = directional_blur(layer, v * 0.5, self.axis)
            layer = shift_layer(layer, 0 if self.axis == "y" else off, off if self.axis == "y" else 0)
            op = 1 - ez.EASE_IN(p) * 0.92 if phase == "out" else 0.35 + 0.65 * ez.clamp(p / 0.4)
            return layer, op
        if kind == "zoom":
            if phase == "out":
                s = 1 + 0.2 * ez.POWER3_IN(p)
                blur = 10 * u * ez.POWER3_IN(p)
                op = 1 - 0.85 * p
            else:
                q = ez.expo_out(p)
                s = 0.82 + 0.18 * q
                blur = 6 * u * (1 - ez.clamp(p / 0.3))
                op = 0.35 + 0.65 * ez.clamp(p / 0.3)
            layer = scale_layer(layer, s)
            if blur > 0.6:
                layer = fast_blur(layer, blur)
            return layer, op
        return layer, 1.0  # wipe：分镜层本身不动，由色块遮挡

    def _wipe_block(self, frame: Image.Image, phase: str, p: float) -> None:
        """强调色色块擦除：剪辑点之前色块推进到盖满画面，之后从另一侧离开。"""
        W, H = self.W, self.H
        a, b = (hex_rgb(c) for c in self.theme.accent)
        block = vertical_gradient(W, H, a, b) if self.axis == "y" else _hgrad(W, H, a, b)
        if phase == "out":
            cover = ez.EASE_IN(p)
            lead = (1 - cover)
        else:
            cover = 1 - ez.EASE_OUT(p)
            lead = -(1 - cover)
        if self.axis == "y":
            paste(frame, block, 0, lead * H)
        else:
            paste(frame, block, lead * W, 0)

    # ---------------------------------------------------------------- 逐帧

    def frame(self, f: int) -> Image.Image:
        t = f / self.fps
        frame = self.bg.frame(t)
        i = self.scene_at(f)
        sp, sc = self.spans[i], self.scenes[i]
        lt = (f - sp.start) / self.fps
        tr = self._transform(i, lt)
        st = lt + sc.lead  # 分镜自己的时间（第一屏跳过了入场）
        if tr is None:
            sc.draw(frame, st)
        else:
            kind, phase, p = tr
            if kind == "wipe":
                sc.draw(frame, st)
                self._wipe_block(frame, phase, p)
            else:
                layer = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
                sc.draw(layer, st)
                span_s = EXIT_SECONDS.get(kind, 0) if phase == "out" else ENTRY_SECONDS.get(kind, 0)
                dt = 1 / (self.fps * span_s) if span_s > 0 else 0
                layer, op = self._apply(layer, kind, phase, p, dt)
                frame.alpha_composite(with_opacity(layer, op))
        overlay = finish_overlay(self.W, self.H, self.theme.vignette, self.theme.grain if self.grain else 0.0,
                                 (f // 2) % GRAIN_PATTERNS)
        frame.alpha_composite(overlay)
        return frame.convert("RGB")

    # ---------------------------------------------------------------- 声音

    def sound_events(self) -> list[SoundEvent]:
        """分镜音效提示点 + 转场呼啸声 + 钩子之后唯一一次「起势 → 重击」。"""
        events: list[SoundEvent] = []
        for i, (sp, sc) in enumerate(zip(self.spans, self.scenes, strict=True)):
            opened = False
            for cue in sc.cues:
                t = cue.time - sc.lead
                if t < 0:  # 跳过的入场动画：提示音不播，只留一记开场重击
                    if cue.kind != "thump" or opened:
                        continue
                    t, opened = 0.02, True
                if t < sp.seconds:
                    events.append(SoundEvent(sp.start_s + t, cue.kind, cue.gain, cue.pan, cue.step))
            if i == len(self.spans) - 1:
                continue
            kind = self.trans_out[i]
            cut = sp.end_s
            if kind in {"push", "zoom", "wipe"}:
                length = self.exit_s[i] + 0.18
                events.append(SoundEvent(cut - length, "whoosh", 0.12 if kind == "push" else 0.15,
                                         pan=-0.25 if self.axis == "x" else 0.0, length=length + 0.12))
        drop = self.drop_time()
        if drop is not None:
            beat = 60 / self.ad.bpm
            length = min(2 * beat, max(0.6, drop - 0.3))
            events.append(SoundEvent(drop - length, "riser", 0.1, length=length))
            events.append(SoundEvent(drop, "impact", 0.28))
        return sorted(events, key=lambda e: e.time)

    def drop_time(self) -> float | None:
        """音乐起势点：钩子（和紧跟在后面的痛点对比）之后的第一个剪辑点，也就是商品登场的那一刀。

        痛点对比是「问题」段，配乐保持留白；解决方案登场时才进鼓点。
        """
        if len(self.spans) > 1 and self.spans[0].scene.kind == "hook":
            for sp in self.spans[1:]:
                if sp.scene.kind != "contrast":
                    return sp.start_s
        return None

    def outro_time(self) -> float:
        for sp in self.spans:
            if sp.scene.kind == "cta":
                return sp.start_s
        return self.spans[-1].start_s


def _hgrad(w: int, h: int, a, b) -> Image.Image:
    t = np.linspace(0, 1, w, dtype=np.float32)[None, :, None]
    aa, bb = np.array(a, np.float32), np.array(b, np.float32)
    rgb = np.repeat((aa + (bb - aa) * t).astype(np.uint8), h, axis=0)
    return Image.fromarray(rgb, "RGB").convert("RGBA")
