"""动效引擎的底层图像操作（Pillow）：带透明度的越界贴图、铺满 / 适配裁切、圆角、投影、光晕、暗角与胶片颗粒。

所有尺寸都由调用方按画面比例换算好再传进来；耗时的静态元素（圆角遮罩、投影、光晕、颗粒）全部缓存，
逐帧只做贴图，1080P 单帧控制在一两百毫秒内。
"""

from __future__ import annotations

from functools import cache, lru_cache

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from app.engine.motion.easing import clamp


def with_opacity(img: Image.Image, opacity: float) -> Image.Image:
    """返回整体透明度乘以 opacity 的副本（opacity ≥ 1 时原样返回）。"""
    if opacity >= 0.999:
        return img
    out = img.copy() if img.mode == "RGBA" else img.convert("RGBA")
    k = clamp(opacity)
    alpha = out.getchannel("A").point(lambda a: round(a * k))
    out.putalpha(alpha)
    return out


def paste(dst: Image.Image, src: Image.Image, x: float, y: float, opacity: float = 1.0) -> None:
    """把 src（RGBA）按透明度叠到 dst 的 (x, y)，超出画布的部分自动裁掉。"""
    if opacity <= 0.002:
        return
    x, y = round(x), round(y)
    w, h = src.size
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(w, dst.width - x), min(h, dst.height - y)
    if right <= left or bottom <= top:
        return
    part = src if (left, top, right, bottom) == (0, 0, w, h) else src.crop((left, top, right, bottom))
    if part.mode != "RGBA":
        part = part.convert("RGBA")
    dst.alpha_composite(with_opacity(part, opacity), (x + left, y + top))


def cover(img: Image.Image, w: float, h: float, focus: tuple[float, float] = (0.5, 0.5),
          resample=Image.Resampling.BICUBIC) -> Image.Image:
    """等比缩放后居中裁切，铺满 w×h（focus 为保留区域的中心，0~1）。"""
    w, h = max(1, round(w)), max(1, round(h))
    s = max(w / img.width, h / img.height)
    nw, nh = max(w, round(img.width * s)), max(h, round(img.height * s))
    big = img.resize((nw, nh), resample)
    x = round((nw - w) * clamp(focus[0]))
    y = round((nh - h) * clamp(focus[1]))
    return big.crop((x, y, x + w, y + h))


def contain(img: Image.Image, w: int, h: int, resample=Image.Resampling.BICUBIC) -> Image.Image:
    """等比缩放到能完整放进 w×h。"""
    s = min(w / img.width, h / img.height)
    return img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), resample)


@lru_cache(maxsize=96)  # 入场缩放时每帧尺寸不同，限制缓存数量
def rounded_mask(w: int, h: int, r: int) -> Image.Image:
    """抗锯齿圆角遮罩（3 倍超采样）。"""
    k = 3
    big = Image.new("L", (w * k, h * k), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, w * k - 1, h * k - 1), radius=max(0, r) * k, fill=255)
    return big.resize((w, h), Image.Resampling.LANCZOS)


def rounded(img: Image.Image, r: float) -> Image.Image:
    """圆角裁切；带透明通道的图（抠图商品）与圆角遮罩取交集。"""
    out = img.convert("RGBA")
    mask = rounded_mask(out.width, out.height, round(r))
    if img.mode == "RGBA":
        mask = ImageChops.multiply(mask, out.getchannel("A"))
    out.putalpha(mask)
    return out


@cache
def drop_shadow(w: int, h: int, r: int, blur: int, opacity: float, color: tuple[int, int, int] = (0, 0, 0)
                ) -> Image.Image:
    """圆角卡片的柔和投影，四周各多出 2×blur 像素。用小图模糊再放大，省时间。"""
    pad = blur * 2
    k = 4
    sw, sh = max(1, (w + 2 * pad) // k), max(1, (h + 2 * pad) // k)
    mask = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(mask).rounded_rectangle((pad // k, pad // k, (pad + w) // k, (pad + h) // k),
                                           radius=max(0, r // k), fill=round(255 * clamp(opacity)))
    mask = mask.filter(ImageFilter.GaussianBlur(max(1, blur / k))).resize((w + 2 * pad, h + 2 * pad),
                                                                          Image.Resampling.BILINEAR)
    out = Image.new("RGBA", mask.size, (*color, 0))
    out.putalpha(mask)
    return out


@cache
def radial_glow(size: int, color: tuple[int, int, int], opacity: float) -> Image.Image:
    """圆形柔光（中心不透明度 opacity，边缘透明），用作背景光斑与商品背光。"""
    n = max(8, size)
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)
    d = np.sqrt((xx - n / 2) ** 2 + (yy - n / 2) ** 2) / (n / 2)
    a = np.clip(1 - d, 0, 1) ** 2.2 * 255 * clamp(opacity)
    out = Image.new("RGBA", (n, n), (*color, 0))
    out.putalpha(Image.fromarray(a.astype(np.uint8)))
    return out


@cache
def vertical_gradient(w: int, h: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    t = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    a, b = np.array(top, np.float32), np.array(bottom, np.float32)
    rows = (a + (b - a) * t).astype(np.uint8)
    return Image.fromarray(np.repeat(rows, w, axis=1), "RGB").convert("RGBA")


@cache
def finish_overlay(w: int, h: int, vignette: float, grain: float, seed: int) -> Image.Image:
    """暗角 + 胶片颗粒合成一张叠加层（每两帧换一张颗粒，避免颗粒「冻住」或闪烁）。"""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2) / np.sqrt(2)
    # 56% 半径以内完全透明，往外逐渐压到 vignette（如 0.22）的黑
    vig = np.clip((d - 0.56) / 0.44, 0, 1) ** 1.6 * vignette
    if grain > 0:
        noise = rng.normal(0.5, 0.18, (h // 2 + 1, w // 2 + 1)).astype(np.float32)
        noise = np.clip(np.kron(noise, np.ones((2, 2), np.float32))[:h, :w], 0, 1)
        g_alpha = np.full((h, w), grain, np.float32)
    else:
        noise = np.zeros((h, w), np.float32)
        g_alpha = np.zeros((h, w), np.float32)
    # 两层合成：颗粒（灰度噪声，透明度 grain）在上，暗角（黑，透明度 vig）在下
    out_a = g_alpha + vig * (1 - g_alpha)
    rgb = np.where(out_a > 1e-4, noise * g_alpha / np.maximum(out_a, 1e-4), 0) * 255
    arr = np.dstack([rgb, rgb, rgb, out_a * 255]).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def fast_blur(img: Image.Image, radius: float) -> Image.Image:
    """大半径模糊：先缩小再高斯模糊再放大，效果接近、速度快很多。"""
    if radius < 0.6:
        return img
    if radius <= 4:
        return img.filter(ImageFilter.GaussianBlur(radius))
    k = max(1, int(radius // 3))
    small = img.resize((max(1, img.width // k), max(1, img.height // k)), Image.Resampling.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(radius / k))
    return small.resize(img.size, Image.Resampling.BILINEAR)


def directional_blur(img: Image.Image, amount: float, axis: str = "x") -> Image.Image:
    """运动模糊（快速移动时使用）：沿运动方向压缩再拉回，近似 180° 快门的拖影。"""
    k = max(1.0, amount)
    if k < 1.5:
        return img
    if axis == "x":
        small = img.resize((max(1, round(img.width / k)), img.height), Image.Resampling.BOX)
    else:
        small = img.resize((img.width, max(1, round(img.height / k))), Image.Resampling.BOX)
    return small.resize(img.size, Image.Resampling.BILINEAR)


def scale_layer(layer: Image.Image, s: float, cx: float | None = None, cy: float | None = None) -> Image.Image:
    """以 (cx, cy) 为中心缩放整层（转场用），画布大小不变。"""
    if abs(s - 1) < 1e-3:
        return layer
    w, h = layer.size
    cx = w / 2 if cx is None else cx
    cy = h / 2 if cy is None else cy
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    big = layer.resize((nw, nh), Image.Resampling.BILINEAR)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    paste(out, big, cx - cx * s, cy - cy * s)
    return out


def shift_layer(layer: Image.Image, dx: float, dy: float) -> Image.Image:
    if abs(dx) < 0.5 and abs(dy) < 0.5:
        return layer
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    paste(out, layer, dx, dy)
    return out


def glass_card(w: float, h: float, r: float, fill: tuple[int, int, int, int], border: tuple[int, int, int, int],
               border_px: float = 2) -> Image.Image:
    """毛玻璃风格的卡片：半透明底 + 细描边（不做真实背景模糊，背景本身已经很柔和）。"""
    return _glass_card(round(w), round(h), round(r), fill, border, max(1, round(border_px)))


@cache
def _glass_card(w: int, h: int, r: int, fill, border, border_px: int) -> Image.Image:
    k = 3
    big = Image.new("RGBA", (w * k, h * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle((0, 0, w * k - 1, h * k - 1), radius=r * k, fill=fill, outline=border,
                        width=border_px * k)
    return big.resize((w, h), Image.Resampling.LANCZOS)


@cache
def pill(w: int, h: int, color_a: tuple[int, int, int], color_b: tuple[int, int, int]) -> Image.Image:
    """横向渐变胶囊（价格 / 按钮）。"""
    t = np.linspace(0, 1, w, dtype=np.float32)[None, :, None]
    a, b = np.array(color_a, np.float32), np.array(color_b, np.float32)
    rgb = np.repeat((a + (b - a) * t).astype(np.uint8), h, axis=0)
    img = Image.fromarray(rgb, "RGB").convert("RGBA")
    img.putalpha(rounded_mask(w, h, h // 2))
    return img


def luminance(img: Image.Image) -> float:
    """平均亮度 0~255（自检用）。"""
    small = img.convert("L").resize((64, 64), Image.Resampling.BOX)
    return float(np.asarray(small, np.float32).mean())
