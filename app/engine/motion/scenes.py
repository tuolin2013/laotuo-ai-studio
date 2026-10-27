"""九类动效分镜的版式与动画：钩子大字 / 痛点对比 / 商品主视觉 / 卖点清单 / 步骤流程 / 数据冲击 / 图集 /
长图滚动 / 收尾行动。

每个分镜三层运动（主体 / 跟随 / 环境）：主体元素入场后静止供阅读，跟随元素滞后 100~300 毫秒入场，
环境层（背景光斑、图片里的缓推缓拉）持续缓慢运动；文字在被阅读时绝不移动。
所有尺寸按 u = 短边 / 1080 换算；竖屏文字放在中间约 75% 宽度、避开平台顶部搜索栏与底部文案区。
退场由转场统一处理（app/engine/motion/renderer.py），这里只负责入场与停留。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from app.engine.motion import easing as ez
from app.engine.motion.compose import (
    directional_blur,
    drop_shadow,
    glass_card,
    paste,
    pill,
    radial_glow,
    rounded_mask,
    vertical_gradient,
)
from app.engine.motion.media import Media
from app.engine.motion.theme import Theme
from app.engine.motion.typeset import (
    TEXT_LOCK,
    KineticText,
    TextStyle,
    font,
    hex_rgb,
    horizontal_gradient,
    text_width,
)
from app.schemas.motion import CONTRAST_HOLD_SECONDS, STEP_SECONDS, MotionScene


@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass(frozen=True)
class Cue:
    """音效提示点（分镜内时间，秒）：渲染器换算成全片时间交给音效引擎。"""

    time: float
    kind: str  # thump / tick / pop / shimmer
    gain: float = 0.15
    pan: float = 0.0
    step: int = 0


@dataclass
class Ctx:
    W: int
    H: int
    fps: int
    theme: Theme
    media: dict[str, Media]
    # 一拍的秒数（60 / BPM）：需要卡点的动作（数字落定、步骤点亮、对比落地）按拍点排期
    beat: float = 0.6
    u: float = 1.0
    portrait: bool = False
    landscape: bool = False
    safe: Box = field(default_factory=lambda: Box(0, 0, 1, 1))

    def __post_init__(self) -> None:
        W, H = self.W, self.H
        self.u = min(W, H) / 1080
        self.portrait = H > W * 1.15
        self.landscape = W > H * 1.15
        if self.portrait:  # 文字放在中间约 75% 宽度：右侧点赞 / 评论按钮、底部标题文案区都避开
            self.safe = Box(0.125 * W, 0.09 * H, 0.875 * W, 0.83 * H)
        elif self.landscape:
            self.safe = Box(0.07 * W, 0.1 * H, 0.93 * W, 0.9 * H)
        else:
            self.safe = Box(0.08 * W, 0.08 * H, 0.92 * W, 0.88 * H)

    def px(self, v: float) -> int:
        return max(1, round(v * self.u))

    def next_beat(self, t: float) -> float:
        """分镜内时间 t 之后（含 t）的第一个拍点。分镜从剪辑点开始，剪辑点本身就在拍点上。"""
        return math.ceil(t / self.beat - 1e-6) * self.beat


# ---------------------------------------------------------------- 可复用的画面元素


class Card:
    """圆角图片 / 视频卡片：按素材比例放进 box（不裁掉主图上的文字），带投影和镜头缓推 / 缓拉 / 横移。

    fill=True 时铺满 box（全屏背景用）。镜头运动用 resize(box=…) 的浮点裁切框实现，亚像素平滑、不抖动。
    """

    def __init__(self, ctx: Ctx, media: Media, box: Box, camera: str = "push_in", *, radius: int | None = None,
                 kb: float = 0.06, shadow: bool = True, fill: bool = False):
        self.ctx, self.media, self.camera, self.kb = ctx, media, camera, kb
        mw, mh = media.size
        if fill:
            self.w, self.h = round(box.w), round(box.h)
        else:
            s = min(box.w / mw, box.h / mh)
            self.w, self.h = max(2, round(mw * s)), max(2, round(mh * s))
        self.cx, self.cy = box.cx, box.cy
        self.r = round(min(self.w, self.h) * 0.055) if radius is None else radius
        self.alpha = media.image is not None and media.image.mode == "RGBA"
        self.src = None
        if media.image is not None:
            src = media.image
            need = max(self.w * (1 + kb) * 1.08 / src.width, self.h * (1 + kb) * 1.08 / src.height)
            if need < 0.95:  # 先一次性高质量缩到够用的尺寸，逐帧只做小幅缩放
                src = src.resize((max(2, round(src.width * need)), max(2, round(src.height * need))),
                                 Image.Resampling.LANCZOS)
            self.src = src
        self.mask = rounded_mask(self.w, self.h, self.r) if self.r > 0 else None
        self.shadow = (drop_shadow(self.w, self.h, self.r, ctx.px(34), ctx.theme.shadow_opacity)
                       if shadow and not self.alpha else None)

    @property
    def box(self) -> Box:
        return Box(self.cx - self.w / 2, self.cy - self.h / 2, self.cx + self.w / 2, self.cy + self.h / 2)

    def window(self, sw: int, sh: int, lt: float, dur: float) -> tuple[float, float, float, float]:
        aspect = self.w / self.h
        bw, bh = (sh * aspect, sh) if sw / sh > aspect else (sw, sw / aspect)
        p = ez.clamp(lt / dur) if dur > 0 else 0.0  # 镜头运动匀速：分镜一开始就在动，不会起步停顿
        z = {"push_in": 1 + self.kb * p, "pull_out": 1 + self.kb * (1 - p), "pan": 1 + self.kb}.get(self.camera, 1.0)
        ww, wh = bw / z, bh / z
        cx, cy = sw / 2, sh / 2
        if self.camera == "pan":
            cx += (bw - ww) * (p - 0.5)
        return cx - ww / 2, cy - wh / 2, cx + ww / 2, cy + wh / 2

    def render(self, lt: float, dur: float, scale: float = 1.0) -> Image.Image:
        size = (max(2, round(self.w * scale)), max(2, round(self.h * scale)))
        src = self.src if self.src is not None else self.media.frame(lt)
        img = src.resize(size, Image.Resampling.BILINEAR, box=self.window(src.width, src.height, lt, dur))
        img = img.convert("RGBA") if img.mode != "RGBA" else img
        if self.mask is not None:
            mask = self.mask if size == (self.w, self.h) else self.mask.resize(size, Image.Resampling.BILINEAR)
            if self.alpha:
                from PIL import ImageChops

                mask = ImageChops.multiply(mask, img.getchannel("A"))
            img.putalpha(mask)
        return img

    def draw(self, layer: Image.Image, lt: float, dur: float, *, scale: float = 1.0, dx: float = 0, dy: float = 0,
             opacity: float = 1.0) -> None:
        if opacity <= 0.002 or scale <= 0.01:
            return
        img = self.render(lt, dur, scale)
        w, h = img.size
        x, y = self.cx - w / 2 + dx, self.cy - h / 2 + dy
        if self.shadow is not None:
            sh = self.shadow
            if abs(scale - 1) > 1e-3:
                sh = sh.resize((max(2, round(sh.width * scale)), max(2, round(sh.height * scale))),
                               Image.Resampling.BILINEAR)
            paste(layer, sh, self.cx - sh.width / 2 + dx, self.cy - sh.height / 2 + dy + h * 0.03, opacity)
        paste(layer, img, x, y, opacity)


class Device:
    """手机 / 浏览器外框：长图在屏幕里匀加速再匀减速地滚动，视频直接在屏幕里播放。"""

    def __init__(self, ctx: Ctx, media: Media, box: Box):
        self.ctx, self.media = ctx, media
        th = ctx.theme
        # 界面截图放进浏览器外框；其余竖图（详情页长图、手机端素材、竖屏视频）放进手机外框
        self.phone = media.asset.source != "screen" and media.ratio >= 1.25
        if self.phone:
            aspect = 9 / 19.5
            h = min(box.h, box.w / aspect)
            w = h * aspect
            self.w, self.h = round(w), round(h)
            bezel = max(3, round(self.w * 0.032))
            self.screen_box = (bezel, bezel, self.w - bezel, self.h - bezel)
            r_body = round(self.w * 0.15)
            self.r_screen = max(2, r_body - bezel)
            body = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
            body.alpha_composite(glass_card(self.w, self.h, r_body, (14, 14, 18, 255), (92, 92, 104, 255),
                                            max(1, ctx.px(3))))
            self.body = body
            iw, ih = round(self.w * 0.27), max(4, round(self.h * 0.032))
            island = Image.new("RGBA", (iw, ih), (0, 0, 0, 255))
            island.putalpha(rounded_mask(iw, ih, ih // 2))
            self.island = (island, (self.w - iw) // 2, bezel + round(self.h * 0.016))
        else:
            w = box.w
            h = min(box.h, w * 0.68)
            if h < w * 0.68:
                w = h / 0.68
            self.w, self.h = round(w), round(h)
            bar = max(8, round(self.h * 0.07))
            border = max(1, ctx.px(2))
            self.screen_box = (border, bar, self.w - border, self.h - border)
            self.r_screen = 0
            bar_color = (34, 34, 40, 255) if th.dark else (236, 238, 242, 255)
            body = glass_card(self.w, self.h, ctx.px(18), bar_color, (80, 80, 90, 255) if th.dark else
                              (210, 214, 222, 255), border).copy()
            d = ImageDraw.Draw(body)
            rr = max(2, round(bar * 0.16))
            for k, c in enumerate(("#FF5F57", "#FEBC2E", "#28C840")):
                cx = round(bar * 0.6 + k * bar * 0.5)
                d.ellipse((cx - rr, bar // 2 - rr, cx + rr, bar // 2 + rr), fill=c)
            self.body = body
            self.island = None
        sx0, sy0, sx1, sy1 = self.screen_box
        self.sw, self.sh = sx1 - sx0, sy1 - sy0
        self.screen_mask = rounded_mask(self.sw, self.sh, self.r_screen) if self.r_screen else None
        self.cx, self.cy = box.cx, box.cy
        self.shadow = drop_shadow(self.w, self.h, round(self.w * 0.12), ctx.px(40), th.shadow_opacity)
        # 静态长图按屏幕宽度一次缩好；比屏幕高时滚动，否则缓推
        self.long = None
        if media.image is not None:
            img = media.image.convert("RGB")
            scaled_h = img.height * self.sw / img.width
            if scaled_h > self.sh * 1.15:
                self.long = img.resize((self.sw, max(self.sh, round(scaled_h))), Image.Resampling.LANCZOS)
        self.card = None if self.long is not None else Card(ctx, media, Box(0, 0, self.sw, self.sh), "push_in",
                                                            radius=0, shadow=False, fill=True)
        self.scroll_start, self.scroll_end = 0.9, 0.0
        self._last_offset = 0.0

    @property
    def box(self) -> Box:
        return Box(self.cx - self.w / 2, self.cy - self.h / 2, self.cx + self.w / 2, self.cy + self.h / 2)

    def _offset(self, lt: float, dur: float) -> float:
        end = max(self.scroll_start + 0.5, dur - 0.55)
        p = ez.EASE_IN_OUT(ez.progress(lt, self.scroll_start, end - self.scroll_start))
        return p * (self.long.height - self.sh)

    def screen(self, lt: float, dur: float) -> Image.Image:
        if self.long is not None:
            y = self._offset(lt, dur)
            img = self.long.resize((self.sw, self.sh), Image.Resampling.BILINEAR, box=(0, y, self.sw, y + self.sh))
            v = abs(y - self._offset(lt - 1 / self.ctx.fps, dur))
            if v > 6:  # 快速滚动时加一点竖向运动模糊（180° 快门 ≈ 每帧位移的一半）
                img = directional_blur(img, v * 0.4, "y")
            img = img.convert("RGBA")
        else:
            img = self.card.render(lt, dur)
        if self.screen_mask is not None:
            img.putalpha(self.screen_mask)
        return img

    def draw(self, layer: Image.Image, lt: float, dur: float, *, scale: float = 1.0, dx: float = 0, dy: float = 0,
             opacity: float = 1.0) -> None:
        if opacity <= 0.002:
            return
        dev = self.body.copy()
        sx0, sy0, _, _ = self.screen_box
        paste(dev, self.screen(lt, dur), sx0, sy0)
        if self.island:
            island, ix, iy = self.island
            paste(dev, island, ix, iy)
        sh = self.shadow
        if abs(scale - 1) > 1e-3:
            dev = dev.resize((max(2, round(dev.width * scale)), max(2, round(dev.height * scale))),
                             Image.Resampling.BILINEAR)
            sh = sh.resize((max(2, round(sh.width * scale)), max(2, round(sh.height * scale))),
                           Image.Resampling.BILINEAR)
        paste(layer, sh, self.cx - sh.width / 2 + dx, self.cy - sh.height / 2 + dy + dev.height * 0.03, opacity)
        paste(layer, dev, self.cx - dev.width / 2 + dx, self.cy - dev.height / 2 + dy, opacity)


def check_badge(d: int, colors: tuple[str, str], ink: str) -> Image.Image:
    """渐变圆形徽标 + 对勾（3 倍超采样）。"""
    k = 3
    big = pill(d * k, d * k, hex_rgb(colors[0]), hex_rgb(colors[1])).copy()
    draw = ImageDraw.Draw(big)
    s = d * k
    draw.line([(s * 0.29, s * 0.53), (s * 0.44, s * 0.67), (s * 0.72, s * 0.36)], fill=ink,
              width=max(2, round(s * 0.09)), joint="curve")
    return big.resize((d, d), Image.Resampling.LANCZOS)


def cross_badge(d: int, color: tuple[int, int, int], ink: str) -> Image.Image:
    """灰色圆形徽标 + ✕（痛点对比的「之前」，3 倍超采样）。"""
    k = 3
    s = d * k
    big = pill(s, s, color, color).copy()
    draw = ImageDraw.Draw(big)
    w = max(2, round(s * 0.085))
    a, b = s * 0.34, s * 0.66
    draw.line([(a, a), (b, b)], fill=ink, width=w)
    draw.line([(a, b), (b, a)], fill=ink, width=w)
    return big.resize((d, d), Image.Resampling.LANCZOS)


def number_node(d: int, n: int, colors: tuple[str, str] | None, ink: str, ring: str | None = None) -> Image.Image:
    """步骤编号圆点：colors 为渐变实心（已点亮），否则画一圈细线（还没轮到）。3 倍超采样。"""
    k = 3
    s = d * k
    if colors is not None:
        big = pill(s, s, hex_rgb(colors[0]), hex_rgb(colors[1])).copy()
    else:
        big = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        ImageDraw.Draw(big).ellipse((k, k, s - k - 1, s - k - 1), outline=ring or ink, width=max(2, round(s * 0.045)))
    with TEXT_LOCK:
        ImageDraw.Draw(big).text((s / 2, s / 2), str(n), font=font(round(s * 0.5), "bold", "numeric"), fill=ink,
                                 anchor="mm")
    return big.resize((d, d), Image.Resampling.LANCZOS)


def place(layer: Image.Image, img: Image.Image, x: float, y: float, *, scale: float = 1.0, dy: float = 0.0,
          opacity: float = 1.0) -> None:
    """以图片中心缩放后贴到 (x, y)（缩放为 1 时不做重采样）。"""
    if abs(scale - 1) > 1e-3:
        w, h = img.size
        im = img.resize((max(2, round(w * scale)), max(2, round(h * scale))), Image.Resampling.BILINEAR)
        x, y = x + (w - im.width) / 2, y + (h - im.height) / 2
        img = im
    paste(layer, img, x, y + dy, opacity)


def gradient_text(text: str, fnt, colors: tuple[str, str]) -> Image.Image:
    """整段文字填充横向渐变（数据冲击的大数字）。按字体基线对齐，计数过程中数字不会上下跳。"""
    with TEXT_LOCK:
        _, _, r, b = fnt.getbbox(text or " ")
        asc, desc = fnt.getmetrics()
        w, h = max(1, r), max(b, asc + desc)
        mask = Image.new("L", (w + 4, h + 4), 0)
        ImageDraw.Draw(mask).text((2, 2), text, font=fnt, fill=255)
    img = horizontal_gradient(mask.width, mask.height, *colors).convert("RGBA")
    img.putalpha(mask)
    box = mask.getbbox() or (0, 0, 1, 1)
    return img.crop((box[0], 0, box[2], img.height))  # 只裁左右，高度固定


def _stagger(n: int, per: float = 0.045, total: float = 0.45) -> float:
    """逐字错峰：单字间隔 per，但整段错峰不超过 total（总错峰 ≤ 0.5 秒）。"""
    return per if n <= 1 else min(per, total / (n - 1))


# ---------------------------------------------------------------- 分镜基类


class SceneRenderer:
    def __init__(self, ctx: Ctx, scene: MotionScene, duration: float, index: int, total: int):
        self.ctx, self.scene, self.D, self.index, self.total = ctx, scene, duration, index, total
        # 入场起点略早于剪辑点：剪辑点那一帧新画面已经在动（第 0 帧不是空背景，转场后也没有「加载中」的空档）
        self.t0 = -0.22 if index == 0 else -0.12
        self.text_boxes: list[tuple[int, int, int, int]] = []
        self.font_px: list[int] = []  # 这一屏所有文字的字号（像素，自检：手机上看不看得清）
        self.cues: list[Cue] = []
        self.settle = 0.0  # 所有元素落定的时间（自检：阅读时间从这里算起）
        # 跳过的入场时长：第一屏从落定状态开场时由渲染器设置（draw 收到的时间 = 剪辑点后的时间 + lead）
        self.lead = 0.0
        self.build()

    @property
    def settled_at(self) -> float:
        """剪辑点之后多久全部落定（第一屏从落定状态开场时为 0）。"""
        return max(0.0, self.settle - self.lead)

    # 子类实现
    def build(self) -> None:  # pragma: no cover - 由子类覆盖
        raise NotImplementedError

    def draw(self, layer: Image.Image, lt: float) -> None:  # pragma: no cover - 由子类覆盖
        raise NotImplementedError

    # ---- 工具

    @property
    def th(self) -> Theme:
        return self.ctx.theme

    def style(self, size: float, *, weight: str = "bold", color: str | None = None, kind: str | None = None,
              tracking: float = 0.02, max_lines: int = 2, accent: bool = True, shadow: float = 0.0,
              line_gap: float = 0.2) -> TextStyle:
        return TextStyle(size=self.ctx.px(size), color=color or self.th.text, weight=weight,
                         kind=kind or self.th.font_kind, tracking_em=tracking, line_gap=line_gap,
                         accent=self.th.accent if accent else None, shadow=shadow, max_lines=max_lines)

    def text(self, text: str, style: TextStyle, width: float, align: str = "center", accent: str = "") -> KineticText:
        return KineticText(text, style, width, align=align, accent_text=accent)

    def on_grid(self, t: float, room: float = 1.2) -> float:
        """t 之后最近的拍点（分镜内时间）：需要「打在拍子上」的动作（数字落定、对比落地、步骤点亮）用它排期。

        拍点之后要留出 room 秒（落定、阅读停留与退场）；分镜剩下的时间不够时不强求卡点，原样返回 t。
        """
        b = self.ctx.next_beat(t)
        return b if b <= self.D - room else t

    def record(self, kt: KineticText | None, x: float, y: float) -> None:
        if kt is not None and kt.text:
            w, h = kt.size
            p = kt.pad
            self.text_boxes.append((round(x + p), round(y + p), round(x + w - p), round(y + h - p)))
            self.font_px.append(kt.font_size)

    @property
    def alive(self) -> bool:
        """内容落定之后画面里还有东西在动（镜头缓推缓拉、长图滚动、视频播放、图集漂移、按钮扫光）。

        这样的屏读完之后多停一会儿不算「干等」；纯文字、静止镜头的屏读完就该走。
        """
        s = self.scene
        if any(k in self.ctx.media and self.ctx.media[k].is_video for k in s.images):
            return True
        if s.kind in {"showcase", "gallery"} or (s.kind == "cta" and s.items):
            return True
        return bool(s.images) and s.camera != "still"

    def head_stack(self, box: Box, align: str, *, head_size: float, sub_size: float = 42, max_lines: int = 2,
                   shadow: float = 0.0, valign: str = "center", gap: float = 0.028) -> tuple:
        """主标题 + 副标题叠放在 box 里，返回 (主标题, (x, y), 副标题, (x, y), 底边 y)。"""
        s = self.scene
        head = self.text(s.headline, self.style(head_size, max_lines=max_lines, shadow=shadow), box.w, align,
                         s.accent) if s.headline else None
        sub = self.text(s.subline, self.style(sub_size, weight="regular", color=self.th.sub, tracking=0.04,
                                              accent=False, shadow=shadow), box.w, align) if s.subline else None
        g = round(self.ctx.H * gap * (0.6 if self.ctx.landscape else 1))
        total = (head.size[1] if head else 0) + (sub.size[1] if sub else 0) + (g if head and sub else 0)
        y = box.y0 if valign == "top" else box.cy - total / 2 if valign == "center" else box.y1 - total

        def x_of(kt: KineticText) -> float:
            return box.x0 if align == "left" else box.cx - kt.size[0] / 2 if align == "center" else box.x1 - kt.size[0]

        head_xy = (x_of(head) - (head.pad if align == "left" else 0), y) if head else (0, y)
        y2 = y + (head.size[1] + g if head else 0)
        sub_xy = (x_of(sub) - (sub.pad if align == "left" else 0), y2) if sub else (0, y2)
        self.record(head, *head_xy)
        self.record(sub, *sub_xy)
        return head, head_xy, sub, sub_xy, y + total

    def draw_heads(self, layer, lt, head, head_xy, sub, sub_xy, start: float) -> float:
        """主标题逐字上升，副标题在主标题快落定时上浮淡入。返回全部落定的时间。"""
        settle = start
        if head is not None:
            st = _stagger(head.char_count)
            head.draw(layer, *head_xy, lt, start=start, stagger=st, dur=0.7)
            settle = head.settle_time(start, st, 0.7)
        if sub is not None:
            t_sub = max(start + 0.2, settle - 0.3)
            sub.draw(layer, *sub_xy, lt, start=t_sub, dur=0.6, mode="fade")
            settle = max(settle, t_sub + 0.6)
        return settle

    def heads_settle(self, head, sub, start: float) -> float:
        settle = start
        if head is not None:
            settle = head.settle_time(start, _stagger(head.char_count), 0.7)
        if sub is not None:
            settle = max(settle, max(start + 0.2, settle - 0.3) + 0.6)
        return settle


def entrance(lt: float, start: float, dur: float = 0.85) -> tuple[float, float]:
    """入场进度：返回 (缓动后的进度, 不透明度)。不透明度在前 35% 进度里到位，不做拖沓的整体淡入。"""
    p = ez.EASE_OUT(ez.progress(lt, start, dur))
    return p, ez.clamp(p / 0.35)


# ---------------------------------------------------------------- 钩子大字


class HookScene(SceneRenderer):
    def build(self) -> None:
        c, s = self.ctx, self.scene
        self.bg = None
        if s.images:
            self.bg = Card(c, c.media[s.images[0]], Box(0, 0, c.W, c.H), s.camera, radius=0, shadow=False,
                           fill=True, kb=0.08)
            top, bottom = (0, 0, 0), (0, 0, 0)
            shade = vertical_gradient(c.W, c.H, top, bottom).copy()
            alpha = Image.linear_gradient("L").resize((c.W, c.H)).point(lambda v: round(110 + v * 0.45))
            shade.putalpha(alpha)
            self.shade = shade
        size = 124 if c.portrait else 136 if c.landscape else 116
        shadow = 0.55 if self.bg else 0.0
        box = Box(c.safe.x0, c.safe.y0, c.safe.x1, c.safe.y0 + c.safe.h * (0.92 if c.portrait else 1))
        self.head, self.head_xy, self.sub, self.sub_xy, bottom = self.head_stack(
            box, "center", head_size=size, sub_size=44, max_lines=3, shadow=shadow, gap=0.05)
        # 主标题与副标题之间的强调细线（跟随层）：从中间向两边生长
        self.line_w = round(c.W * (0.16 if c.portrait else 0.1))
        self.line_h = c.px(6)
        self.line = pill(self.line_w, self.line_h, hex_rgb(self.th.accent[0]), hex_rgb(self.th.accent[1]))
        if self.sub is not None:
            self.line_y = (self.head_xy[1] + self.head.size[1] + self.sub_xy[1]) / 2 - self.line_h / 2
        else:
            self.line_y = self.head_xy[1] + self.head.size[1] + c.H * 0.02
        st = _stagger(self.head.char_count)
        self.t_line = self.head.settle_time(self.t0, st, 0.7) - 0.35
        self.settle = max(self.heads_settle(self.head, self.sub, self.t0), self.t_line + 0.55)
        if self.index == 0:
            self.cues.append(Cue(0.02, "thump", 0.2))
        self.cues.append(Cue(self.t_line + 0.05, "tick", 0.08, step=4))

    def draw(self, layer: Image.Image, lt: float) -> None:
        if self.bg is not None:
            self.bg.draw(layer, lt, self.D)
            paste(layer, self.shade, 0, 0)
        self.draw_heads(layer, lt, self.head, self.head_xy, self.sub, self.sub_xy, self.t0)
        p = ez.EASE_OUT(ez.progress(lt, self.t_line, 0.55))
        if p > 0:
            w = max(1, round(self.line_w * p))
            x0 = (self.line_w - w) // 2
            paste(layer, self.line.crop((x0, 0, x0 + w, self.line_h)), self.ctx.W / 2 - w / 2, self.line_y)


# ---------------------------------------------------------------- 商品主视觉


class HeroScene(SceneRenderer):
    def build(self) -> None:
        c, s = self.ctx, self.scene
        sf = c.safe
        m = c.media[s.images[0]]
        if c.landscape:
            text_box = Box(sf.x0 + sf.w * 0.56, sf.y0, sf.x1, sf.y1)
            self.head, self.head_xy, self.sub, self.sub_xy, _ = self.head_stack(text_box, "left", head_size=96,
                                                                                sub_size=44, max_lines=3)
            card_box = Box(sf.x0, sf.y0 - c.H * 0.02, sf.x0 + sf.w * 0.5, sf.y1 + c.H * 0.02)
        else:
            probe = self.head_stack(Box(sf.x0, 0, sf.x1, 0), "center", head_size=88, sub_size=42)
            self.text_boxes.clear()
            text_h = probe[4] - min(probe[1][1], probe[3][1])
            card_box = Box(sf.x0, sf.y0 + c.H * 0.01, sf.x1, sf.y1 - text_h - c.H * 0.05)
            self.card_probe = card_box
        self.card = Card(c, m, card_box, s.camera)
        if not c.landscape:
            cb = self.card.box
            text_box = Box(sf.x0, cb.y1 + c.H * 0.045, sf.x1, sf.y1)
            self.head, self.head_xy, self.sub, self.sub_xy, _ = self.head_stack(text_box, "center", head_size=88,
                                                                                sub_size=42, valign="top")
        g = round(max(self.card.w, self.card.h) * 1.7)
        self.glow = radial_glow(g, self.th.accent_rgb, 0.5 if self.th.dark else 0.3)
        self.t_head = self.t0 + 0.3
        self.settle = max(self.t0 + 0.9, self.heads_settle(self.head, self.sub, self.t_head))
        self.cues.append(Cue(self.t0 + 0.32, "thump", 0.14))

    def draw(self, layer: Image.Image, lt: float) -> None:
        c = self.ctx
        p, op = entrance(lt, self.t0, 0.9)
        breathe = 0.85 + 0.15 * math.sin(lt * 2 * math.pi / 3.2)  # 背光呼吸（环境层）
        paste(layer, self.glow, self.card.cx - self.glow.width / 2, self.card.cy - self.glow.height / 2,
              op * breathe)
        self.card.draw(layer, lt, self.D, scale=0.9 + 0.1 * p, dy=(1 - p) * c.H * 0.05, opacity=op)
        self.draw_heads(layer, lt, self.head, self.head_xy, self.sub, self.sub_xy, self.t_head)


# ---------------------------------------------------------------- 卖点清单


class FeaturesScene(SceneRenderer):
    def build(self) -> None:
        c, s = self.ctx, self.scene
        sf = c.safe
        th = self.th
        self.card = None
        if c.landscape:
            col = Box(sf.x0 + sf.w * 0.5, sf.y0, sf.x1, sf.y1) if s.images else \
                Box(c.W * 0.2, sf.y0, c.W * 0.8, sf.y1)
            if s.images:
                self.card = Card(c, c.media[s.images[0]], Box(sf.x0, sf.y0, sf.x0 + sf.w * 0.44, sf.y1), s.camera)
            align = "left" if s.images else "center"
            self.head, self.head_xy, self.sub, self.sub_xy, y = self.head_stack(col, align, head_size=80, sub_size=40,
                                                                                valign="top")
            area = Box(col.x0, y + c.H * 0.05, col.x1, col.y1)
        else:
            self.head, self.head_xy, self.sub, self.sub_xy, y = self.head_stack(
                Box(sf.x0, sf.y0, sf.x1, sf.y1), "center", head_size=80, sub_size=40, valign="top")
            if s.images:
                img_box = Box(sf.x0, y + c.H * 0.03, sf.x1, y + c.H * 0.03 + sf.h * 0.3)
                self.card = Card(c, c.media[s.images[0]], img_box, s.camera)
                y = self.card.box.y1
            area = Box(sf.x0, y + c.H * 0.035, sf.x1, sf.y1)
        n = len(s.items)
        gap = c.px(22)
        ch = min(c.px(118 if c.portrait else 104), (area.h - (n - 1) * gap) / max(1, n))
        ch = max(c.px(56), ch)
        total = n * ch + (n - 1) * gap
        y0 = area.y0 + max(0, (area.h - total) / 2) if not s.images or c.landscape else area.y0
        cw = round(area.w)
        r = round(ch * 0.3)
        fill = th.card_fill
        self.items: list[tuple[Image.Image, Image.Image, float, float, float, float]] = []
        bd = round(ch * 0.46)
        badge = check_badge(bd, th.accent, th.ink_on_accent)
        size = min(c.px(48), round(ch * 0.4))
        fnt = font(size, "bold", th.font_kind)
        for k, item in enumerate(s.items):
            card = glass_card(cw, round(ch), r, fill, th.card_border, c.px(2)).copy()
            d = ImageDraw.Draw(card)
            tx = round(ch * 0.3 + bd + ch * 0.26)
            max_w = cw - tx - ch * 0.3
            fs = size
            while text_width(item, font(fs, "bold", th.font_kind)) > max_w and fs > 12:
                fs -= 2
            fnt = font(fs, "bold", th.font_kind)
            asc, desc = fnt.getmetrics()
            d.text((tx, round(ch / 2 - (asc + desc) / 2)), item, font=fnt, fill=th.text)
            self.font_px.append(fs)
            x, y = area.x0, y0 + k * (ch + gap)
            self.items.append((card, badge, x, y, x + ch * 0.3, y + (ch - bd) / 2))
            self.text_boxes.append((round(x + tx), round(y), round(x + tx + text_width(item, fnt)), round(y + ch)))
        self.t_items = [self.t0 + 0.35 + k * 0.2 for k in range(n)]
        for k, t in enumerate(self.t_items):
            self.cues.append(Cue(t + 0.14, "tick", 0.1, pan=-0.2 + 0.4 * k / max(1, n - 1), step=k))
        self.settle = max(self.heads_settle(self.head, self.sub, self.t0), self.t_items[-1] + 0.75 if n else 0)

    def draw(self, layer: Image.Image, lt: float) -> None:
        c = self.ctx
        if self.card is not None:
            p, op = entrance(lt, self.t0 + 0.1, 0.9)
            self.card.draw(layer, lt, self.D, scale=0.94 + 0.06 * p, dy=(1 - p) * c.px(40), opacity=op)
        self.draw_heads(layer, lt, self.head, self.head_xy, self.sub, self.sub_xy, self.t0)
        for (card, badge, x, y, bx, by), t in zip(self.items, self.t_items, strict=True):
            p, op = entrance(lt, t, 0.6)
            if op <= 0:
                continue
            dy = (1 - p) * c.px(26)
            paste(layer, card, x, y + dy, op)
            sp = ez.spring(lt - t - 0.12, *ez.SPRING_SNAPPY)  # 对勾滞后 120 毫秒弹出（跟随层）
            if sp > 0.01:
                bs = max(2, round(badge.width * sp))
                b = badge if bs == badge.width else badge.resize((bs, bs), Image.Resampling.BILINEAR)
                paste(layer, b, bx + (badge.width - bs) / 2, by + dy + (badge.width - bs) / 2, op)


# ---------------------------------------------------------------- 痛点对比


def chevron(size: int, color: tuple[int, int, int], width: int, direction: str) -> Image.Image:
    """细线箭头（right / down），3 倍超采样。"""
    k = 3
    s = size * k
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    pts = ([(s * 0.32, s * 0.16), (s * 0.68, s * 0.5), (s * 0.32, s * 0.84)] if direction == "right" else
           [(s * 0.16, s * 0.32), (s * 0.5, s * 0.68), (s * 0.84, s * 0.32)])
    ImageDraw.Draw(img).line(pts, fill=(*color, 255), width=max(2, width * k), joint="curve")
    return img.resize((size, size), Image.Resampling.LANCZOS)


class ContrastScene(SceneRenderer):
    """痛点对比：「之前」先落定、单独停留约 0.8 秒供阅读，「之后」卡在拍点上落地，「之前」那一侧随即压暗。

    条目固定 2 条（之前、之后），素材 0 张或 2 张（之前、之后各一张）。横屏或有配图时左右分栏，
    竖屏 / 方形没有配图时上下两张宽卡片；「之后」用强调色描边与背光，箭头指向它。
    「之前」压暗时只改透明度、不移动（文字在被阅读时绝不移动）。
    """

    def build(self) -> None:
        c, s = self.ctx, self.scene
        sf, th = c.safe, self.th
        self.head, self.head_xy, self.sub, self.sub_xy, y = self.head_stack(
            Box(sf.x0, sf.y0, sf.x1, sf.y1), "center", head_size=84, sub_size=40, valign="top")
        top = y + c.H * 0.05 if (self.head or self.sub) else sf.y0
        area = Box(sf.x0, top, sf.x1, sf.y1)
        pad = c.px(30)
        self.cards: list[Card] = []
        self.columns = c.landscape or len(s.images) == 2
        if self.columns:
            mid = c.px(110 if c.landscape else 64)
            col_w = (area.w - mid) / 2
            xs = (area.x0, area.x1 - col_w)
            big = c.landscape and not s.images  # 横屏没有配图：两张大卡片撑满画面，不留一大片空
            d = c.px(92 if big else 64)
            size = 70 if big else 46
            kts = self._texts(col_w - 2 * pad, size, "center")
            h = round(2 * pad + d + c.px(18) + max(kt.size[1] for kt in kts))
            if big:
                h = max(h, round(area.h * 0.55))
            if s.images:
                img_gap = c.px(24)
                probe = [Card(c, c.media[k], Box(0, 0, col_w, max(c.px(80), area.h - h - img_gap)), s.camera)
                         for k in s.images[:2]]
                ih = max(p.h for p in probe)
                y0 = area.y0 + max(0.0, (area.h - ih - img_gap - h) / 2)
                self.cards = [Card(c, c.media[k], Box(x, y0, x + col_w, y0 + ih), s.camera, kb=0.05)
                              for k, x in zip(s.images[:2], xs, strict=True)]
                card_y = y0 + ih + img_gap
            else:
                card_y = area.y0 + max(0.0, (area.h - h) * 0.42)
            panels = [self._panel(round(col_w), h, kt, k == 1, True, d, pad) for k, kt in enumerate(kts)]
            self.slots = [(x, card_y) for x in xs]
            self.arrow = chevron(c.px(54), hex_rgb(th.accent[0]), c.px(5), "right")
            self.arrow_xy = (area.cx - self.arrow.width / 2, card_y + h / 2 - self.arrow.height / 2)
            self.arrow_dir = (1.0, 0.0)
        else:
            d = c.px(100 if c.portrait else 84)
            w = round(area.w)
            gap_x = c.px(30)
            kts = self._texts(w - 2 * pad - d - gap_x, 62 if c.portrait else 52, "left")
            h = round(max(c.px(230 if c.portrait else 180), max(kt.size[1] for kt in kts) + 2 * pad))
            gap = c.px(130 if c.portrait else 90)  # 箭头在两张卡片之间
            y0 = area.y0 + max(0.0, (area.h - 2 * h - gap) * 0.4)
            panels = [self._panel(w, h, kt, k == 1, False, d, pad, gap_x) for k, kt in enumerate(kts)]
            self.slots = [(area.x0, y0), (area.x0, y0 + h + gap)]
            self.arrow = chevron(c.px(54), hex_rgb(th.accent[0]), c.px(5), "down")
            self.arrow_xy = (area.cx - self.arrow.width / 2, y0 + h + gap / 2 - self.arrow.height / 2)
            self.arrow_dir = (0.0, 1.0)
        self.panels = [p for p, _ in panels]
        for (_, (tx, ty)), kt, (sx, sy) in zip(panels, kts, self.slots, strict=True):
            self.record(kt, sx + tx, sy + ty)
        pw, ph = self.panels[1].size
        self.glow = radial_glow(round(max(pw, ph) * 1.25), th.accent_rgb, 0.5 if th.dark else 0.3)
        # 排期：之前入场 → 落定后单独停留 → 之后卡在拍点上落地（动作比拍点早 0.15 秒开始，拍点时已基本落定）
        self.t_before = self.t0 + 0.3
        self.t_hit = self.on_grid(self.t_before + 0.6 + CONTRAST_HOLD_SECONDS, room=1.0)
        self.t_after = self.t_hit - 0.15
        self.settle = max(self.heads_settle(self.head, self.sub, self.t0), self.t_hit + 0.45)
        self.cues.append(Cue(self.t_before + 0.12, "tick", 0.06, pan=-0.2, step=0))
        self.cues.append(Cue(self.t_hit + 1 / c.fps, "thump", 0.16))  # 音效峰值比画面晚一帧

    def _texts(self, width: float, size: float, align: str) -> list[KineticText]:
        """之前用次要文字色，之后用主文字色；每条最多两行。"""
        return [self.text(item, self.style(size, color=self.th.text if k else self.th.sub, max_lines=2, accent=False),
                          width, align) for k, item in enumerate(self.scene.items[:2])]

    def _panel(self, w: int, h: int, kt: KineticText, good: bool, stack: bool, d: int, pad: int,
               gap_x: int = 0) -> tuple[Image.Image, tuple[float, float]]:
        """一张对比卡片（静态图，入场时整体动）：徽标（✕ / ✓）+ 文字。返回 (卡片, 文字块在卡片里的位置)。"""
        c, th = self.ctx, self.th
        border = (*hex_rgb(th.accent[0]), 220) if good else th.card_border
        r = c.px(28) if stack else round(h * 0.26)
        card = glass_card(w, h, r, th.card_fill, border, c.px(3 if good else 2)).copy()
        grey = (84, 84, 94) if th.dark else (170, 170, 178)
        badge = check_badge(d, th.accent, th.ink_on_accent) if good else cross_badge(d, grey, "#FFFFFF")
        if stack:  # 徽标在上、文字在下，整组在卡片里垂直居中
            content = d + c.px(18) + kt.size[1] - 2 * kt.pad
            bx, by = (w - d) / 2, max(pad, (h - content) / 2)
            tx, ty = (w - kt.size[0]) / 2, by + d + c.px(18) - kt.pad
        else:
            bx, by = pad, (h - d) / 2
            tx, ty = bx + d + gap_x - kt.pad, (h - kt.size[1]) / 2
        paste(card, badge, bx, by)
        paste(card, kt.settled(), tx, ty)
        return card, (tx, ty)

    def draw(self, layer: Image.Image, lt: float) -> None:
        c = self.ctx
        dim = 1 - 0.5 * ez.EASE_OUT(ez.progress(lt, self.t_hit, 0.35))
        p, op = entrance(lt, self.t_before, 0.6)
        if op > 0:
            dy = (1 - p) * c.px(30)
            if self.cards:
                self.cards[0].draw(layer, lt, self.D, scale=0.95 + 0.05 * p, dy=dy, opacity=op * dim)
            paste(layer, self.panels[0], self.slots[0][0], self.slots[0][1] + dy, op * dim)
        p2, op2 = entrance(lt, self.t_after, 0.55)
        if op2 > 0:
            dy = (1 - p2) * c.px(44)
            x, y = self.slots[1]
            pw, ph = self.panels[1].size
            flash = 1 - ez.EASE_OUT(ez.progress(lt, self.t_hit, 0.7))  # 落地瞬间背光亮一下，再回到常亮
            breathe = 0.9 + 0.1 * math.sin(lt * 2 * math.pi / 3.2)
            paste(layer, self.glow, x + pw / 2 - self.glow.width / 2, y + ph / 2 - self.glow.height / 2 + dy,
                  op2 * (0.45 + 0.4 * flash) * breathe)
            if len(self.cards) > 1:
                self.cards[1].draw(layer, lt, self.D, scale=0.92 + 0.08 * p2, dy=dy, opacity=op2)
            place(layer, self.panels[1], x, y, scale=0.92 + 0.08 * p2, dy=dy, opacity=op2)
            ax, ay = self.arrow_xy
            shift = (1 - p2) * c.px(18)
            paste(layer, self.arrow, ax - self.arrow_dir[0] * shift, ay - self.arrow_dir[1] * shift, op2)
        self.draw_heads(layer, lt, self.head, self.head_xy, self.sub, self.sub_xy, self.t0)


# ---------------------------------------------------------------- 步骤流程


class StepsScene(SceneRenderer):
    """步骤流程：2~4 步，整条路线（空心圆点 + 轨道）先画出来，然后编号圆点逐拍点亮、连线从上一步长到下一步。

    竖屏 / 方形、横屏有配图时竖排（圆点在左、文字在右）；横屏没有配图时横排（圆点一行、文字在圆点下方）。
    可配 1 张图：竖屏放在标题下方，横屏放在左侧。
    """

    def build(self) -> None:
        c, s = self.ctx, self.scene
        sf, th = c.safe, self.th
        n = len(s.items)
        side = c.landscape and bool(s.images)
        col = Box(sf.x0 + sf.w * 0.5, sf.y0, sf.x1, sf.y1) if side else sf
        self.head, self.head_xy, self.sub, self.sub_xy, y = self.head_stack(
            col, "left" if side else "center", head_size=80, sub_size=40, valign="top")
        self.card = None
        if side:
            self.card = Card(c, c.media[s.images[0]], Box(sf.x0, sf.y0, sf.x0 + sf.w * 0.44, sf.y1), s.camera)
        elif s.images:
            img_box = Box(sf.x0, y + c.H * 0.03, sf.x1, y + c.H * 0.03 + sf.h * 0.28)
            self.card = Card(c, c.media[s.images[0]], img_box, s.camera)
            y = self.card.box.y1
        area = Box(col.x0, y + c.H * 0.05, col.x1, col.y1)
        d = c.px(100 if c.portrait else 72)
        lw = c.px(6)
        self.horizontal = c.landscape and not s.images
        self.nodes: list[tuple[float, float]] = []
        self.texts: list[tuple[KineticText, float, float]] = []
        self.links: list[tuple[float, float, float, str]] = []  # (x, y, 长度, 方向)
        if self.horizontal:
            slot = area.w / n
            kts = [self.text(it, self.style(46, max_lines=2, accent=False), slot * 0.86, "center") for it in s.items]
            gap = c.px(28)
            top = area.y0 + max(0.0, (area.h - d - gap - max(kt.size[1] for kt in kts)) * 0.4)
            for k, kt in enumerate(kts):
                cx = area.x0 + slot * (k + 0.5)
                self.nodes.append((cx - d / 2, top))
                self.texts.append((kt, cx - kt.size[0] / 2, top + d + gap))
            m = c.px(16)
            for k in range(n - 1):
                x0, x1 = self.nodes[k][0] + d + m, self.nodes[k + 1][0] - m
                self.links.append((x0, top + d / 2 - lw / 2, x1 - x0, "x"))
        else:
            gap_x = c.px(34)
            row_gap = c.px(110 if c.portrait and not s.images else 64 if c.portrait else 40)
            size = 62 if c.portrait else 48
            while True:  # 步骤多、配了图时缩小字号直到放得下（不小于 30）
                kts = [self.text(it, self.style(size, max_lines=2, accent=False), area.w - d - gap_x, "left")
                       for it in s.items]
                rows = [max(d, kt.size[1]) for kt in kts]
                total = sum(rows) + row_gap * (n - 1)
                if total <= area.h or size <= 30:
                    break
                size = max(30, round(size * 0.9))
            block_w = d + gap_x + max(kt.size[0] - 2 * kt.pad for kt in kts)
            x0 = area.x0 if side else max(area.x0, area.cx - block_w / 2)
            ry = area.y0 + max(0.0, (area.h - total) * 0.35)
            for kt, rh in zip(kts, rows, strict=True):
                self.nodes.append((x0, ry + (rh - d) / 2))
                self.texts.append((kt, x0 + d + gap_x - kt.pad, ry + (rh - kt.size[1]) / 2))
                ry += rh + row_gap
            m = c.px(12)
            for k in range(n - 1):
                y0, y1 = self.nodes[k][1] + d + m, self.nodes[k + 1][1] - m
                self.links.append((x0 + d / 2 - lw / 2, y0, max(1.0, y1 - y0), "y"))
        for kt, x, yy in self.texts:
            self.record(kt, x, yy)
        self.d, self.lw = d, lw
        self.rings = [number_node(d, k + 1, None, th.sub, th.sub) for k in range(n)]
        self.lit = [number_node(d, k + 1, th.accent, th.ink_on_accent) for k in range(n)]
        self.font_px.append(round(d * 0.5))
        track = (255, 255, 255) if th.dark else (0, 0, 0)
        self.track_rgba = (*track, 255)
        self.fill_rgb = hex_rgb(th.accent[0])
        # 排期：路线先画出来；第一步卡在拍点上点亮，之后每步间隔整数拍（约 STEP_SECONDS），放不下时压缩间隔
        self.t_track = self.t0 + 0.2
        first = self.on_grid(self.t0 + 0.55)
        spacing = c.beat * max(1, round(STEP_SECONDS / c.beat))
        latest = self.D - 1.15  # 最后一步点亮后还要落定 0.6 秒、停留 0.5 秒
        if n > 1 and first + spacing * (n - 1) > latest:
            spacing = max(0.3, (latest - first) / (n - 1))
        self.t_steps = [first + k * spacing for k in range(n)]
        self.settle = max(self.heads_settle(self.head, self.sub, self.t0), self.t_steps[-1] + 0.6)
        for k, t in enumerate(self.t_steps):
            self.cues.append(Cue(t + 1 / c.fps, "tick", 0.09, pan=-0.2 + 0.4 * k / max(1, n - 1), step=k + 2))

    def _bar(self, length: float, axis: str, rgba: tuple[int, int, int, int]) -> Image.Image | None:
        n = round(length)
        if n < 1:
            return None
        size = (n, self.lw) if axis == "x" else (self.lw, n)
        return Image.new("RGBA", size, rgba)

    def draw(self, layer: Image.Image, lt: float) -> None:
        c = self.ctx
        if self.card is not None:
            p, op = entrance(lt, self.t0 + 0.1, 0.9)
            self.card.draw(layer, lt, self.D, scale=0.94 + 0.06 * p, dy=(1 - p) * c.px(40), opacity=op)
        self.draw_heads(layer, lt, self.head, self.head_xy, self.sub, self.sub_xy, self.t0)
        _, route = entrance(lt, self.t_track, 0.6)
        if route <= 0:
            return
        for k, (x, y, length, axis) in enumerate(self.links):
            track = self._bar(length, axis, self.track_rgba)
            if track is not None:
                paste(layer, track, x, y, route * (0.14 if self.th.dark else 0.1))
            t_a, t_b = self.t_steps[k] + 0.12, self.t_steps[k + 1]
            grow = ez.EASE_IN_OUT(ez.progress(lt, t_a, max(0.2, t_b - t_a)))
            fill = self._bar(length * grow, axis, (*self.fill_rgb, 255))
            if fill is not None:
                paste(layer, fill, x, y)
        for k, ((x, y), (kt, tx, ty)) in enumerate(zip(self.nodes, self.texts, strict=True)):
            t = self.t_steps[k]
            sp = ez.spring(lt - (t - 0.06), *ez.SPRING_SNAPPY)
            if sp < 0.999:
                paste(layer, self.rings[k], x, y, route * 0.8)
            if sp > 0.01:
                place(layer, self.lit[k], x, y, scale=max(0.05, sp), opacity=ez.clamp(sp * 3))
            kt.draw(layer, tx, ty, lt, start=t + 0.04, dur=0.5, mode="fade")


# ---------------------------------------------------------------- 数据冲击

_NUM = re.compile(r"^(\D*?)(\d+(?:\.\d+)?)(.*)$")


def _num_kind(text: str) -> str:
    return "numeric" if all(ord(ch) < 0x2E80 for ch in text) else "sans"
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


class StatScene(SceneRenderer):
    def build(self) -> None:
        c, s = self.ctx, self.scene
        sf = c.safe
        th = self.th
        m = _NUM.match(s.stat_value.replace(" ", ""))
        self.prefix, self.number, self.suffix = (m.group(1), m.group(2), m.group(3)) if m else ("", "", s.stat_value)
        self.decimals = len(self.number.split(".")[1]) if "." in self.number else 0
        self.value = float(self.number) if self.number else 0.0
        width = sf.w * (0.92 if c.portrait else 0.7)
        size = c.px(300 if c.landscape else 280)
        # DIN 风格数字字体没有中文字形：带中文的前缀 / 单位（如「到手价」「个」）改用黑体
        big_kind, small_kind = _num_kind(self.prefix + self.number), _num_kind(self.suffix)
        while size > 20:
            big = font(size, "bold", big_kind)
            small = font(round(size * 0.5), "bold", small_kind)
            w = text_width(self.prefix + self.number, big) + text_width(self.suffix, small) + size * 0.04
            if w <= width:
                break
            size = round(size * 0.92)
        self.big, self.small = font(size, "bold", big_kind), font(round(size * 0.5), "bold", small_kind)
        self.size = size
        self.font_px.append(round(size * 0.5) if self.suffix else size)
        final = gradient_text(self.prefix + self.number, self.big, th.accent)
        self.num_h = final.height
        suf = gradient_text(self.suffix, self.small, th.accent) if self.suffix else None
        self.suffix_img = suf
        self.num_w = final.width + (suf.width + round(size * 0.04) if suf else 0)
        # 百分比：横向进度条（含基准刻度，如「标准 ≥24%」）
        self.pct = self.value if self.suffix.startswith("%") and self.number else None
        bench = _PCT.search(s.subline) if self.pct is not None else None
        self.bench = float(bench.group(1)) if bench else None
        self.bar_w = round(width * 0.9)
        self.bar_h = c.px(16)
        label = self.text(s.headline, self.style(60, color=th.text, max_lines=2), sf.w, "center", s.accent) \
            if s.headline else None
        sub = self.text(s.subline, self.style(40, weight="regular", color=th.sub, tracking=0.04, accent=False),
                        sf.w, "center") if s.subline else None
        g = c.H * (0.025 if c.portrait else 0.035)
        parts = [label.size[1] if label else 0, self.num_h, self.bar_h * 3 if self.pct is not None else 0,
                 sub.size[1] if sub else 0]
        total = sum(parts) + g * (sum(1 for p in parts if p) - 1)
        y = sf.cy - total / 2 - (c.H * 0.02 if c.portrait else 0)
        self.label, self.label_xy = label, ((c.W - label.size[0]) / 2, y) if label else (0, y)
        y += (label.size[1] + g) if label else 0
        self.num_xy = ((c.W - self.num_w) / 2, y)
        self.text_boxes.append((round(self.num_xy[0]), round(y), round(self.num_xy[0] + self.num_w),
                                round(y + self.num_h)))
        y += self.num_h + g
        if self.pct is not None:
            self.bar_xy = ((c.W - self.bar_w) / 2, y + self.bar_h)
            y += self.bar_h * 3 + g
        self.sub, self.sub_xy = sub, ((c.W - sub.size[0]) / 2, y) if sub else (0, y)
        self.record(label, *self.label_xy)
        self.record(sub, *self.sub_xy)
        self.t_num = self.t0 + 0.15
        # 计数滚动约 0.8~1.4 秒，落定（顶一下 + 低音）卡在拍点上
        self.t_land = self.on_grid(self.t_num + 0.8)
        self.count_dur = max(0.5, self.t_land - self.t_num)
        self.settle = max(self.t_land + 0.4, self.heads_settle(label, None, self.t0))
        self.cues.append(Cue(self.t_land, "thump", 0.22))

    def _num_image(self, p: float) -> Image.Image:
        v = self.value * p
        txt = f"{v:.{self.decimals}f}" if self.number else ""
        main = gradient_text(self.prefix + txt, self.big, self.th.accent)
        if self.suffix_img is None:
            return main
        gap = round(self.size * 0.04)
        out = Image.new("RGBA", (main.width + gap + self.suffix_img.width, self.num_h), (0, 0, 0, 0))
        paste(out, main, 0, 0)
        # 单位（%、g）用小一号的字，与数字共用基线
        paste(out, self.suffix_img, main.width + gap, self.big.getmetrics()[0] - self.small.getmetrics()[0])
        return out

    def draw(self, layer: Image.Image, lt: float) -> None:
        c, th = self.ctx, self.th
        if self.label is not None:
            self.label.draw(layer, *self.label_xy, lt, start=self.t0, stagger=_stagger(self.label.char_count))
        p_in, op = entrance(lt, self.t_num, 0.6)
        if op > 0:
            count = ez.EASE_OUT(ez.progress(lt, self.t_num, self.count_dur))
            img = self._num_image(count)
            # 计数落定时轻轻「顶」一下（1 → 1.05 → 1），配合一记低音
            punch = 0.0
            if lt >= self.t_land:
                a = ez.progress(lt, self.t_land, 0.08)
                b = ez.EASE_OUT(ez.progress(lt, self.t_land + 0.08, 0.4))
                punch = 0.05 * (ez.EASE_OUT(a) - b)
            s = 1 + punch
            # 计数时右对齐到最终位置：单位（%）不动，数字向左长出来；落定后的「顶一下」以中心缩放
            x = self.num_xy[0] + self.num_w - img.width
            if abs(s - 1) > 1e-3:
                img = img.resize((round(img.width * s), round(img.height * s)), Image.Resampling.BILINEAR)
                x = c.W / 2 - img.width / 2
            y = self.num_xy[1] + self.num_h - img.height + (1 - p_in) * c.px(40) + (img.height - self.num_h) / 2
            paste(layer, img, x, y, op)
            if self.pct is not None:
                bx, by = self.bar_xy
                track = pill(self.bar_w, self.bar_h, (255, 255, 255) if th.dark else (0, 0, 0),
                             (255, 255, 255) if th.dark else (0, 0, 0))
                paste(layer, track, bx, by, op * (0.14 if th.dark else 0.08))
                fw = round(self.bar_w * min(1.0, self.pct / 100) * count)
                if fw > self.bar_h:
                    fill = pill(fw, self.bar_h, hex_rgb(th.accent[0]), hex_rgb(th.accent[1]))
                    paste(layer, fill, bx, by, op)
                if self.bench is not None:
                    _, mop = entrance(lt, self.t_num + 0.35, 0.5)
                    mx = bx + self.bar_w * min(1.0, self.bench / 100)
                    mark = Image.new("RGBA", (c.px(4), self.bar_h * 3), (*hex_rgb(th.sub), 255))
                    paste(layer, mark, mx - mark.width / 2, by - self.bar_h, mop * 0.9)
        if self.sub is not None:
            self.sub.draw(layer, *self.sub_xy, lt, start=self.t_num + 0.45, dur=0.6, mode="fade")


# ---------------------------------------------------------------- 图集


class GalleryScene(SceneRenderer):
    def build(self) -> None:
        c, s = self.ctx, self.scene
        sf = c.safe
        keys = s.images[:6]
        n = len(keys)
        align = "center"
        self.head, self.head_xy, self.sub, self.sub_xy, y = self.head_stack(
            Box(sf.x0, sf.y0, sf.x1, sf.y1), align, head_size=80, sub_size=40, valign="top")
        area = Box(sf.x0, y + c.H * 0.04, sf.x1, sf.y1)
        cols = (min(n, 3) if c.landscape else 2) if n > 1 else 1
        if c.landscape and n == 4:
            cols = 4
        rows = math.ceil(n / cols)
        gap = c.px(26)
        cell_w = (area.w - (cols - 1) * gap) / cols
        cell_h = (area.h - (rows - 1) * gap) / rows
        cams = ("push_in", "pull_out")
        self.cards: list[Card] = []
        for k, key in enumerate(keys):
            r, col = divmod(k, cols)
            in_row = min(cols, n - r * cols)
            x_off = (cols - in_row) * (cell_w + gap) / 2  # 最后一行不满时居中
            x0 = area.x0 + x_off + col * (cell_w + gap)
            y0 = area.y0 + r * (cell_h + gap)
            cam = s.camera if s.camera == "still" else cams[k % 2]
            self.cards.append(Card(c, c.media[key], Box(x0, y0, x0 + cell_w, y0 + cell_h), cam,
                                   radius=c.px(26), kb=0.05))
        step = min(0.1, 0.45 / max(1, n - 1)) if n > 1 else 0
        self.t_cards = [self.t0 + 0.25 + k * step for k in range(n)]
        self.settle = max(self.heads_settle(self.head, self.sub, self.t0), self.t_cards[-1] + 0.8)
        self.cues.append(Cue(self.t_cards[0] + 0.3, "tick", 0.07, step=2))

    def draw(self, layer: Image.Image, lt: float) -> None:
        c = self.ctx
        drift = ez.clamp(lt / self.D) if self.D > 0 else 0
        for k, (card, t) in enumerate(zip(self.cards, self.t_cards, strict=True)):
            p, op = entrance(lt, t, 0.75)
            if op <= 0:
                continue
            par = 1.0 if k % 2 == 0 else 0.6  # 视差：相邻卡片漂移速度不同
            card.draw(layer, lt, self.D, scale=0.92 + 0.08 * p, dy=(1 - p) * c.px(36) - drift * c.px(14) * par,
                      opacity=op)
        self.draw_heads(layer, lt, self.head, self.head_xy, self.sub, self.sub_xy, self.t0)


# ---------------------------------------------------------------- 长图滚动


class ShowcaseScene(SceneRenderer):
    def build(self) -> None:
        c, s = self.ctx, self.scene
        sf = c.safe
        m = c.media[s.images[0]]
        if c.landscape:
            col = Box(sf.x0, sf.y0, sf.x0 + sf.w * 0.44, sf.y1)
            self.head, self.head_xy, self.sub, self.sub_xy, _ = self.head_stack(col, "left", head_size=96,
                                                                                sub_size=44, max_lines=3)
            dev_box = Box(sf.x0 + sf.w * 0.5, c.H * 0.06, sf.x1, c.H * 0.94)
        else:
            self.head, self.head_xy, self.sub, self.sub_xy, y = self.head_stack(
                Box(sf.x0, sf.y0, sf.x1, sf.y1), "center", head_size=84, sub_size=40, valign="top")
            dev_box = Box(sf.x0, y + c.H * 0.035, sf.x1, sf.y1 + c.H * 0.06)
        self.device = Device(c, m, dev_box)
        self.glow = radial_glow(round(max(self.device.w, self.device.h) * 1.4), self.th.accent_rgb,
                                0.4 if self.th.dark else 0.24)
        self.t_head = self.t0 + 0.2
        self.settle = max(self.t0 + 0.9, self.heads_settle(self.head, self.sub, self.t_head))
        self.cues.append(Cue(self.t0 + 0.4, "thump", 0.12))

    def draw(self, layer: Image.Image, lt: float) -> None:
        c = self.ctx
        p, op = entrance(lt, self.t0, 0.9)
        d = self.device
        paste(layer, self.glow, d.cx - self.glow.width / 2, d.cy - self.glow.height / 2, op * 0.9)
        d.draw(layer, lt, self.D, scale=0.96 + 0.04 * p, dy=(1 - p) * c.H * 0.06, opacity=op)
        self.draw_heads(layer, lt, self.head, self.head_xy, self.sub, self.sub_xy, self.t_head)


# ---------------------------------------------------------------- 收尾行动


class CtaScene(SceneRenderer):
    def build(self) -> None:
        c, s = self.ctx, self.scene
        sf, th = c.safe, self.th
        self.card = None
        if c.landscape and s.images:
            self.card = Card(c, c.media[s.images[0]], Box(sf.x0, sf.y0, sf.x0 + sf.w * 0.42, sf.y1), s.camera)
            col = Box(sf.x0 + sf.w * 0.5, sf.y0, sf.x1, sf.y1)
        elif s.images:
            self.card = Card(c, c.media[s.images[0]], Box(sf.x0 + sf.w * 0.12, sf.y0, sf.x1 - sf.w * 0.12,
                                                          sf.y0 + sf.h * 0.36), s.camera)
            col = Box(sf.x0, self.card.box.y1 + c.H * 0.03, sf.x1, sf.y1)
        else:
            col = sf
        # 价格 / 活动胶囊与按钮
        self.pill_img = None
        if s.stat_value:
            ps = c.px(58)
            self.font_px.append(ps)
            fnt = font(ps, "bold", "sans")
            tw = text_width(s.stat_value, fnt)
            pw, ph = round(tw + ps * 1.3), round(ps * 1.7)
            img = pill(pw, ph, hex_rgb(th.accent[0]), hex_rgb(th.accent[1])).copy()
            asc, desc = fnt.getmetrics()
            ImageDraw.Draw(img).text(((pw - tw) / 2, (ph - asc - desc) / 2), s.stat_value, font=fnt,
                                     fill=th.ink_on_accent)
            self.pill_img = img
        self.btn = None
        if s.items:
            bs = c.px(44)
            self.font_px.append(bs)
            fnt = font(bs, "bold", "sans")
            tw = text_width(s.items[0], fnt)
            bw, bh = round(tw + bs * 2.2), round(bs * 2.1)
            fill = (255, 255, 255, 255) if th.dark else (*hex_rgb(th.text), 255)
            ink = "#0B0B0F" if th.dark else "#FFFFFF"
            img = glass_card(bw, bh, bh // 2, fill, fill, 1).copy()
            asc, desc = fnt.getmetrics()
            ImageDraw.Draw(img).text(((bw - tw) / 2, (bh - asc - desc) / 2), s.items[0], font=fnt, fill=ink)
            self.btn = img
            streak = horizontal_gradient(round(bh * 1.2), bh, "#FFFFFF", "#FFFFFF").convert("RGBA")
            a = Image.linear_gradient("L").rotate(90, expand=True).resize((streak.width, bh))
            a = a.point(lambda v: round(255 * max(0.0, 1 - abs(v / 127.5 - 1)) ** 2 * 0.55))
            streak.putalpha(a)
            self.streak = streak
        head_size = 104 if c.portrait else 112
        probe = self.head_stack(col, "center", head_size=head_size, sub_size=44, max_lines=2)
        self.text_boxes.clear()
        extra = (self.pill_img.height + c.H * 0.03 if self.pill_img else 0) + \
                (self.btn.height + c.H * 0.03 if self.btn else 0)
        block = probe[4] - probe[1][1] + extra
        top = col.cy - block / 2 if not (s.images and not c.landscape) else col.y0
        self.head, self.head_xy, self.sub, self.sub_xy, y = self.head_stack(
            Box(col.x0, top, col.x1, col.y1), "center", head_size=head_size, sub_size=44, valign="top")
        y += c.H * 0.03
        self.pill_xy = (col.cx - self.pill_img.width / 2, y) if self.pill_img else None
        if self.pill_img:
            self.text_boxes.append((round(self.pill_xy[0]), round(y), round(self.pill_xy[0] + self.pill_img.width),
                                    round(y + self.pill_img.height)))
            y += self.pill_img.height + c.H * 0.03
        self.btn_xy = (col.cx - self.btn.width / 2, y) if self.btn else None
        if self.btn:
            self.text_boxes.append((round(self.btn_xy[0]), round(y), round(self.btn_xy[0] + self.btn.width),
                                    round(y + self.btn.height)))
        self.t_head = self.t0 + (0.25 if self.card else 0.1)
        head_settle = self.heads_settle(self.head, None, self.t_head)
        self.t_pill = head_settle - 0.1
        self.t_btn = self.t_pill + 0.3
        self.t_shine = self.t_btn + 0.8
        self.settle = max(self.heads_settle(self.head, self.sub, self.t_head), self.t_btn + 0.7)
        if self.pill_img:
            self.cues.append(Cue(self.t_pill + 0.03, "pop", 0.2))
        if self.btn:
            self.cues.append(Cue(self.t_shine + 0.2, "shimmer", 0.07, pan=0.3))

    def draw(self, layer: Image.Image, lt: float) -> None:
        c = self.ctx
        if self.card is not None:
            p, op = entrance(lt, self.t0, 0.9)
            self.card.draw(layer, lt, self.D, scale=0.9 + 0.1 * p, dy=(1 - p) * c.px(50), opacity=op)
        self.draw_heads(layer, lt, self.head, self.head_xy, self.sub, self.sub_xy, self.t_head)
        if self.pill_img is not None:
            sp = ez.spring(lt - self.t_pill, *ez.SPRING_POP)  # 整支片子唯一的回弹时刻
            if sp > 0.01:
                img = self.pill_img
                w, h = max(2, round(img.width * sp)), max(2, round(img.height * sp))
                img = img.resize((w, h), Image.Resampling.BILINEAR)
                x, y = self.pill_xy
                paste(layer, img, x + (self.pill_img.width - w) / 2, y + (self.pill_img.height - h) / 2,
                      ez.clamp(sp * 3))
        if self.btn is not None:
            p, op = entrance(lt, self.t_btn, 0.6)
            if op > 0:
                btn = self.btn
                if lt > self.t_shine:  # 按钮扫光：每 2.6 秒一次，停留期间保持「活着」
                    phase = ((lt - self.t_shine) % 2.6) / 0.9
                    if phase < 1:
                        btn = btn.copy()
                        sx = -self.streak.width + (btn.width + self.streak.width) * ez.EASE_IN_OUT(phase)
                        glow = Image.new("RGBA", btn.size, (0, 0, 0, 0))
                        paste(glow, self.streak, sx, 0)
                        from PIL import ImageChops

                        glow.putalpha(ImageChops.multiply(glow.getchannel("A"), btn.getchannel("A")))
                        btn.alpha_composite(glow)
                x, y = self.btn_xy
                paste(layer, btn, x, y + (1 - p) * c.px(24), op)


RENDERERS: dict[str, type[SceneRenderer]] = {
    "hook": HookScene, "contrast": ContrastScene, "hero": HeroScene, "features": FeaturesScene, "steps": StepsScene,
    "stat": StatScene, "gallery": GalleryScene, "showcase": ShowcaseScene, "cta": CtaScene,
}
