"""零 Key 演示模式：用 PIL 绘制带真实中文排版与参考图缩略合成的高清预览图。"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from app.providers.demo.fonts import font, wrap

# 比例 -> 输出尺寸（长边 1024，宽高均为偶数，便于 ffmpeg 编码）
CANVAS_SIZES: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "3:4": (768, 1024),
    "4:3": (1024, 768),
    "2:3": (682, 1024),
    "3:2": (1024, 682),
    "9:16": (576, 1024),
    "16:9": (1024, 576),
}

# 类目配置 palette 中的中文色名 -> RGB
NAMED_COLORS: dict[str, tuple[int, int, int]] = {
    "山林绿": (46, 110, 78),
    "云雾白": (236, 240, 236),
    "茶汤黄绿": (196, 190, 92),
    "原木色": (196, 160, 118),
    "奶油白": (250, 243, 228),
    "暖橙": (240, 150, 70),
    "浅木色": (214, 186, 150),
    "薄荷绿": (150, 210, 186),
}
MODE_LABELS = {
    "text_to_image": "文生图 · 0 参考图",
    "single_image_edit": "单图保真编辑 · 1 参考图",
    "multi_image_fusion": "多图融合",
}


def _color(name: str) -> tuple[int, int, int]:
    if name in NAMED_COLORS:
        return NAMED_COLORS[name]
    h = hashlib.md5(name.encode("utf-8")).digest()
    return (80 + h[0] % 150, 80 + h[1] % 150, 80 + h[2] % 150)


def _mix(a, b, t: float) -> tuple[int, int, int]:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _luma(c) -> float:
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


@dataclass
class DemoArt:
    """一张演示图需要的全部内容。"""

    title: str
    subtitle: str = ""
    copy_text: str = ""
    description: str = ""
    mode: str = "text_to_image"
    model: str = "demo"
    palette: list[str] = field(default_factory=list)
    references: list[Path] = field(default_factory=list)
    reference_labels: list[str] = field(default_factory=list)
    badge: str = ""
    seed: str = ""


def _background(size, colors, rng: random.Random) -> Image.Image:
    w, h = size
    top, bottom = colors[0], colors[1 % len(colors)]
    img = Image.new("RGB", size, top)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        draw.line([(0, y), (w, y)], fill=_mix(top, bottom, y / max(h - 1, 1)))
    # 柔光光斑
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for _ in range(9):
        r = rng.randint(w // 12, w // 4)
        x, y = rng.randint(0, w), rng.randint(0, h)
        c = colors[rng.randrange(len(colors))]
        gd.ellipse([x - r, y - r, x + r, y + r], fill=(*_mix(c, (255, 255, 255), 0.5), 70))
    glow = glow.filter(ImageFilter.GaussianBlur(w // 30))
    img.paste(glow, (0, 0), glow)
    return img


def _scenery(img: Image.Image, colors, rng: random.Random) -> None:
    """0 参考图时绘制层叠山峦 / 雾气，模拟场景氛围图。"""
    w, h = img.size
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    base = colors[0] if _luma(colors[0]) < 160 else (60, 100, 80)
    for i in range(4):
        y0 = int(h * (0.45 + i * 0.1))
        pts = [(0, h)]
        x = 0
        while x <= w:
            pts.append((x, y0 - rng.randint(0, h // 6)))
            x += rng.randint(w // 10, w // 5)
        pts += [(w, y0), (w, h)]
        d.polygon(pts, fill=(*_mix(base, (230, 235, 230), 0.6 - i * 0.15), 200))
    layer = layer.filter(ImageFilter.GaussianBlur(2))
    img.paste(layer, (0, 0), layer)


def _thumb(path: Path, box: tuple[int, int]) -> Image.Image | None:
    try:
        with Image.open(path) as src:
            im = ImageOps.exif_transpose(src).convert("RGB")
    except (OSError, ValueError):
        return None
    im.thumbnail(box)
    framed = ImageOps.expand(im, border=max(4, box[0] // 60), fill=(255, 255, 255))
    return framed


def _paste_references(img: Image.Image, art: DemoArt) -> None:
    w, h = img.size
    refs = art.references[:3]
    n = len(refs)
    area_w, area_h = int(w * (0.78 if n == 1 else 0.9)), int(h * 0.42)
    gap = w // 40
    each_w = (area_w - gap * (n - 1)) // n
    x = (w - area_w) // 2
    y = int(h * 0.3)
    draw = ImageDraw.Draw(img)
    small = font(max(14, w // 40), bold=True)
    for i, path in enumerate(refs):
        th = _thumb(path, (each_w, area_h))
        if th is None:
            continue
        shadow = Image.new("RGBA", (th.width + 30, th.height + 30), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rectangle([15, 15, th.width + 15, th.height + 15], fill=(0, 0, 0, 90))
        shadow = shadow.filter(ImageFilter.GaussianBlur(10))
        px = x + i * (each_w + gap) + (each_w - th.width) // 2
        py = y + (area_h - th.height) // 2
        img.paste(shadow, (px - 8, py), shadow)
        img.paste(th, (px, py))
        if n > 1:
            label = f"图{i + 1}"
            if i < len(art.reference_labels):
                label += f" {art.reference_labels[i]}"
            draw.rounded_rectangle([px, py - small.size - 14, px + draw.textlength(label, font=small) + 16, py - 4],
                                   radius=6, fill=(0, 0, 0, 150) if img.mode == "RGBA" else (30, 30, 30))
            draw.text((px + 8, py - small.size - 10), label, font=small, fill=(255, 255, 255))


def _text_block(draw, xy, text, fnt, fill, max_w, max_lines, shadow=True) -> int:
    x, y = xy
    for line in wrap(draw, text, fnt, max_w, max_lines):
        if shadow:
            draw.text((x + 2, y + 2), line, font=fnt, fill=(0, 0, 0))
        draw.text((x, y), line, font=fnt, fill=fill)
        y += int(fnt.size * 1.25)
    return y


def render_demo_image(art: DemoArt, aspect_ratio: str, out_path: Path) -> Path:
    size = CANVAS_SIZES.get(aspect_ratio, (1024, 1024))
    w, h = size
    rng = random.Random(art.seed or art.title)
    colors = [_color(c) for c in art.palette] or [(60, 110, 90), (230, 236, 230)]
    if len(colors) == 1:
        colors.append(_mix(colors[0], (255, 255, 255), 0.6))
    rng.shuffle(colors)
    img = _background(size, colors[:3], rng)

    if art.references:
        _paste_references(img, art)
    else:
        _scenery(img, colors, rng)

    draw = ImageDraw.Draw(img, "RGBA")
    margin = w // 16
    # 顶部标题区
    draw.rectangle([0, 0, w, int(h * 0.27)], fill=(0, 0, 0, 70))
    y = _text_block(draw, (margin, margin), art.title, font(max(28, w // 12), True), (255, 255, 255),
                    w - 2 * margin, 2)
    if art.subtitle:
        _text_block(draw, (margin, y + 6), art.subtitle, font(max(18, w // 26)), (255, 240, 200),
                    w - 2 * margin, 2)

    # 底部文案与画面描述面板
    panel_top = int(h * 0.79)
    draw.rounded_rectangle([margin // 2, panel_top, w - margin // 2, h - margin // 2], radius=w // 40,
                           fill=(255, 255, 255, 215))
    yy = panel_top + margin // 3
    if art.copy_text:
        yy = _text_block(draw, (margin, yy), art.copy_text, font(max(18, w // 30), True), (40, 40, 40),
                         w - 2 * margin, 2, shadow=False)
    if art.description:
        _text_block(draw, (margin, yy + 4), "画面：" + art.description, font(max(13, w // 50)), (90, 90, 90),
                    w - 2 * margin, 3, shadow=False)

    # 角标：演示标识 + 模型 + 模式
    tag = font(max(13, w // 52), True)
    badge = f"DEMO 演示预览 · {art.model}"
    bw = draw.textlength(badge, font=tag) + 20
    draw.rounded_rectangle([w - bw - 12, 12, w - 12, 12 + tag.size + 14], radius=8, fill=(220, 60, 60, 230))
    draw.text((w - bw - 2, 19), badge, font=tag, fill=(255, 255, 255))
    mode_txt = MODE_LABELS.get(art.mode, art.mode)
    if art.mode == "multi_image_fusion":
        mode_txt += f" · {len(art.references)} 参考图"
    if art.badge:
        mode_txt += f" · {art.badge}"
    mw = draw.textlength(mode_txt, font=tag) + 20
    draw.rounded_rectangle([12, panel_top - tag.size - 26, 12 + mw, panel_top - 10], radius=8, fill=(0, 0, 0, 160))
    draw.text((22, panel_top - tag.size - 20), mode_txt, font=tag, fill=(255, 255, 255))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, "PNG")
    return out_path
