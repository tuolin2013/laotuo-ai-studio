"""零 Key 演示模式：用 PIL 绘制带真实中文排版与参考图缩略合成的高清预览图。

槽位带构图模式时按「三维设计体系」排版：美学流派的色板与字体、主体位置、虚线框标出的文字留白区，
让演示图直观体现所选的美学与构图；未设置构图（如视频首帧、白底图）时使用经典版式。
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from app.providers.demo.aesthetics import SEAL_RED, STYLES, VERTICAL_STYLES, StyleSpec, hex_rgb
from app.providers.demo.fonts import font, styled_font, wrap
from app.schemas.plan import AESTHETIC_STYLE_NAMES, COMPOSITION_MODE_NAMES

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

# 构图 -> (主体 / 参考图区域, 文字留白区)，坐标为画面比例 (x0, y0, x1, y1)
LAYOUT_BOXES: dict[str, tuple[tuple[float, ...], tuple[float, ...]]] = {
    "rule_of_thirds_negative_space": ((0.38, 0.40, 0.96, 0.92), (0.05, 0.06, 0.62, 0.38)),
    "extreme_macro_texture": ((0.02, 0.24, 0.88, 0.98), (0.52, 0.08, 0.96, 0.30)),
    "depth_layering": ((0.16, 0.36, 0.84, 0.90), (0.06, 0.06, 0.94, 0.31)),
    "diagonal_dynamic_flow": ((0.04, 0.38, 0.66, 0.92), (0.56, 0.08, 0.96, 0.38)),
    "split_screen_comparison": ((0.03, 0.25, 0.67, 0.92), (0.05, 0.04, 0.95, 0.21)),
    "knolling_flat_lay": ((0.06, 0.33, 0.94, 0.92), (0.08, 0.05, 0.92, 0.28)),
}
# 新中式 / 山野 + 三分留白：主体左下，右上竖排
VERTICAL_BOXES = ((0.04, 0.40, 0.62, 0.92), (0.68, 0.10, 0.95, 0.66))
COMPARE_BOX = (0.71, 0.25, 0.97, 0.92)

_HEX = re.compile(r"#[0-9A-Fa-f]{6}")


def _color(name: str) -> tuple[int, int, int]:
    if name in NAMED_COLORS:
        return NAMED_COLORS[name]
    m = _HEX.search(name)  # 如「山林绿（#2B7A0B）」
    if m:
        return hex_rgb(m.group())
    h = hashlib.md5(name.encode("utf-8")).digest()
    return (80 + h[0] % 150, 80 + h[1] % 150, 80 + h[2] % 150)


def _mix(a, b, t: float) -> tuple[int, int, int]:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _luma(c) -> float:
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def _hex(c) -> str:
    return "#{:02X}{:02X}{:02X}".format(*c)


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
    # 三维设计：美学流派 / 构图模式（auto 时使用经典版式与类目色板）/ 三级胶囊角标
    aesthetic: str = "auto"
    composition: str = "auto"
    pill: str = ""


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


def _paste_references(img: Image.Image, art: DemoArt, box: tuple[int, int, int, int] | None = None) -> None:
    """把参考图缩略图并排放进 box（默认画面中部）。"""
    w, h = img.size
    refs = art.references[:3]
    n = len(refs)
    if box is None:
        area_w, area_h = int(w * (0.78 if n == 1 else 0.9)), int(h * 0.42)
        x, y = (w - area_w) // 2, int(h * 0.3)
    else:
        x, y, area_w, area_h = box[0], box[1], box[2] - box[0], box[3] - box[1]
    gap = w // 40
    each_w = (area_w - gap * (n - 1)) // n
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


def _demo_badge(img: Image.Image, art: DemoArt) -> None:
    w = img.width
    draw = ImageDraw.Draw(img, "RGBA")
    tag = font(max(13, w // 52), True)
    badge = f"DEMO 演示预览 · {art.model}"
    bw = draw.textlength(badge, font=tag) + 20
    draw.rounded_rectangle([w - bw - 12, 12, w - 12, 12 + tag.size + 14], radius=8, fill=(220, 60, 60, 230))
    draw.text((w - bw - 2, 19), badge, font=tag, fill=(255, 255, 255))


def _mode_text(art: DemoArt) -> str:
    text = MODE_LABELS.get(art.mode, art.mode)
    if art.mode == "multi_image_fusion":
        text += f" · {len(art.references)} 参考图"
    return text + (f" · {art.badge}" if art.badge else "")


# ---------------------------------------------------------------- 经典版式（未设置构图）


def _render_classic(art: DemoArt, size: tuple[int, int]) -> Image.Image:
    w, h = size
    rng = random.Random(art.seed or art.title)
    spec = STYLES.get(art.aesthetic)
    if spec is not None:
        colors = [hex_rgb(c) for c in (*spec.background, spec.accent)]
    else:
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

    # 角标：生图模式
    tag = font(max(13, w // 52), True)
    mode_txt = _mode_text(art)
    mw = draw.textlength(mode_txt, font=tag) + 20
    draw.rounded_rectangle([12, panel_top - tag.size - 26, 12 + mw, panel_top - 10], radius=8, fill=(0, 0, 0, 160))
    draw.text((22, panel_top - tag.size - 20), mode_txt, font=tag, fill=(255, 255, 255))
    return img


# ---------------------------------------------------------------- 三维设计版式


def _style(art: DemoArt) -> StyleSpec:
    """美学流派为 auto 时，用类目色板临时拼一套高反差配色。"""
    if art.aesthetic in STYLES:
        return STYLES[art.aesthetic]
    colors = [_color(c) for c in art.palette] or [(60, 110, 90), (230, 236, 230)]
    top, bottom, accent = colors[0], colors[1 % len(colors)], colors[2 % len(colors)]
    light = _luma(top) > 150
    return StyleSpec((_hex(top), _hex(bottom)), "#222222" if light else "#F5F2EB",
                     "#555555" if light else "#E8E2D0", _hex(accent), "sans")


def _box(size: tuple[int, int], frac) -> tuple[int, int, int, int]:
    w, h = size
    return int(frac[0] * w), int(frac[1] * h), int(frac[2] * w), int(frac[3] * h)


def _dashed_rect(draw, box, fill, width: int = 2, dash: int = 14) -> None:
    x0, y0, x1, y1 = box
    for x in range(x0, x1, dash * 2):
        draw.line([(x, y0), (min(x + dash, x1), y0)], fill=fill, width=width)
        draw.line([(x, y1), (min(x + dash, x1), y1)], fill=fill, width=width)
    for y in range(y0, y1, dash * 2):
        draw.line([(x0, y), (x0, min(y + dash, y1))], fill=fill, width=width)
        draw.line([(x1, y), (x1, min(y + dash, y1))], fill=fill, width=width)


def _subject_glow(img: Image.Image, box, color) -> None:
    """文生图没有商品参考图：用柔光椭圆标出画面主体所在区域。"""
    x0, y0, x1, y1 = box
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse([x0 + (x1 - x0) // 8, y0 + (y1 - y0) // 6, x1 - (x1 - x0) // 8,
                                   y1 - (y1 - y0) // 10], fill=(*color, 120))
    layer = layer.filter(ImageFilter.GaussianBlur(max(8, (x1 - x0) // 12)))
    img.paste(layer, (0, 0), layer)


def _flat_lay_tiles(img: Image.Image, box, accent, base) -> None:
    """俯拍平铺没有参考图时：用 2×3 网格色块示意按网格陈列的物品与接触阴影。"""
    x0, y0, x1, y1 = box
    draw = ImageDraw.Draw(img, "RGBA")
    gap = (x1 - x0) // 18
    cw, ch = (x1 - x0 - gap * 4) // 3, (y1 - y0 - gap * 3) // 2
    for r in range(2):
        for c in range(3):
            tx, ty = x0 + gap + c * (cw + gap), y0 + gap + r * (ch + gap)
            draw.rounded_rectangle([tx + 6, ty + 8, tx + cw + 6, ty + ch + 8], radius=gap // 2, fill=(0, 0, 0, 40))
            draw.rounded_rectangle([tx, ty, tx + cw, ty + ch], radius=gap // 2,
                                   fill=(*_mix(accent, base, 0.35 + 0.15 * ((r + c) % 2)), 235))


def _comparison_panel(img: Image.Image) -> None:
    """分屏对比：右侧 30% 为冷灰低饱和的普通对照视窗。"""
    x0, y0, x1, y1 = _box(img.size, COMPARE_BOX)
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rounded_rectangle([x0, y0, x1, y1], radius=img.width // 60, fill=(148, 154, 160, 215))
    draw.line([(x0 - img.width // 60, y0), (x0 - img.width // 60, y1)], fill=(255, 255, 255, 200), width=3)
    fnt = font(max(14, img.width // 34), True)
    label = "普通对照"
    draw.text((x0 + (x1 - x0 - draw.textlength(label, font=fnt)) // 2, y0 + (y1 - y0) // 2), label, font=fnt,
              fill=(235, 238, 240))


def _pill(draw, xy, text: str, fnt, spec: StyleSpec) -> None:
    """三级信任胶囊角标。"""
    x, y = xy
    fill = hex_rgb(spec.accent)
    h = fnt.size + 12
    draw.rounded_rectangle([x, y, x + draw.textlength(text, font=fnt) + 28, y + h], radius=h // 2, fill=fill)
    draw.text((x + 14, y + 5), text, font=fnt, fill=(255, 255, 255) if _luma(fill) < 150 else (34, 34, 34))


def _horizontal_text(draw, box, art: DemoArt, spec: StyleSpec, w: int) -> None:
    """主标题 : 副标题 ≈ 3 : 1.2，胶囊角标跟在副标题下方。"""
    x0, y0, x1, y1 = box
    pad = max(8, (x1 - x0) // 20)
    bw, bh = x1 - x0 - 2 * pad, y1 - y0 - 2 * pad
    # 字号同时受宽度与高度约束：标题 + 副标题 + 胶囊必须全部落在留白区内
    rows = 1.3 * (1 if len(art.title) <= 8 else 2) + (0.65 if art.subtitle else 0) + (0.6 if art.pill else 0)
    size = max(18, min(bw // max(min(len(art.title), 8), 4), w // 10, int(bh / rows)))
    head = styled_font(size, spec.font, bold=True)
    y = _text_block(draw, (x0 + pad, y0 + pad), art.title, head, hex_rgb(spec.text), bw, 2, shadow=False)
    if art.subtitle:
        sub = styled_font(max(14, int(size * 0.42)), spec.font)
        y = _text_block(draw, (x0 + pad, y + size // 6), art.subtitle, sub, hex_rgb(spec.sub), bw, 2, shadow=False)
    if art.pill:
        _pill(draw, (x0 + pad, y + size // 5), art.pill, styled_font(max(13, int(size * 0.3)), "sans", True), spec)


def _vertical_text(draw, box, art: DemoArt, spec: StyleSpec) -> None:
    """新中式竖排：从上往下写、从右往左列，主标题最多 2 列，旁缀朱砂小印章。"""
    x0, y0, x1, y1 = box
    title = "".join(art.title.split())
    size = max(20, min((x1 - x0) // 3, (y1 - y0) // max(min(len(title), 7), 4)))
    head = styled_font(size, spec.font, bold=True)
    step = int(size * 1.1)
    per_col = max(1, (y1 - y0 - size // 2) // step)
    cols = [title[i:i + per_col] for i in range(0, len(title), per_col)][:2]
    x = x1 - size - size // 4
    first_end = y0
    for c, col in enumerate(cols):
        y = y0 + size // 4
        for ch in col:
            draw.text((x, y), ch, font=head, fill=hex_rgb(spec.text))
            y += step
        if c == 0:
            first_end = y
        x -= int(size * 1.35)
    if art.subtitle:
        sub = styled_font(max(14, int(size * 0.45)), spec.font)
        y, sx = y0 + size // 2, x + size - sub.size
        for ch in "".join(art.subtitle.split())[: max(1, (y1 - y) // int(sub.size * 1.15))]:
            draw.text((sx, y), ch, font=sub, fill=hex_rgb(spec.sub))
            y += int(sub.size * 1.15)
    if spec.seal and title:
        side = int(size * 0.72)
        sx, sy = x1 - size - size // 4 + (size - side) // 2, min(first_end + size // 5, y1 - side - 4)
        draw.rectangle([sx, sy, sx + side, sy + side], fill=hex_rgb(SEAL_RED))
        seal = styled_font(int(side * 0.7), spec.font, bold=True)
        draw.text((sx + (side - seal.size) // 2, sy + (side - seal.size) // 2 - 2), title[0], font=seal,
                  fill=(255, 245, 235))


def _info_strip(draw, size, art: DemoArt) -> None:
    """底部一行说明：生图模式 · 分辨率 · 美学流派 · 构图模式。"""
    w, h = size
    tag = font(max(12, w // 56), True)
    top = h - tag.size - 18
    draw.rectangle([0, top, w, h], fill=(0, 0, 0, 150))
    parts = [_mode_text(art), AESTHETIC_STYLE_NAMES.get(art.aesthetic, "自动配色"),
             COMPOSITION_MODE_NAMES.get(art.composition, "")]
    lines = wrap(draw, " ｜ ".join(p for p in parts if p), tag, w - 24, 1)
    if lines:
        draw.text((12, top + 9), lines[0], font=tag, fill=(255, 255, 255))


def _render_designed(art: DemoArt, size: tuple[int, int]) -> Image.Image:
    rng = random.Random(art.seed or art.title)
    spec = _style(art)
    top, bottom = hex_rgb(spec.background[0]), hex_rgb(spec.background[1])
    img = _background(size, [top, bottom], rng)
    vertical = art.composition == "rule_of_thirds_negative_space" and art.aesthetic in VERTICAL_STYLES
    subject_f, text_f = VERTICAL_BOXES if vertical else LAYOUT_BOXES[art.composition]
    subject, text_box = _box(size, subject_f), _box(size, text_f)

    if art.references:
        _paste_references(img, art, subject)
    elif art.composition == "knolling_flat_lay":
        _flat_lay_tiles(img, subject, hex_rgb(spec.accent), bottom)
    else:
        _scenery(img, [top, bottom], rng)
        _subject_glow(img, subject, hex_rgb(spec.accent))
    if art.composition == "split_screen_comparison":
        _comparison_panel(img)

    draw = ImageDraw.Draw(img, "RGBA")
    # 留白区：保持干净底色，并用虚线框标出，中文只排在框内
    draw.rectangle(text_box, fill=(*top, 165))
    _dashed_rect(draw, text_box, (*hex_rgb(spec.text), 110))
    if vertical:
        _vertical_text(draw, text_box, art, spec)
    else:
        _horizontal_text(draw, text_box, art, spec, size[0])
    _info_strip(draw, size, art)
    return img


def render_demo_image(art: DemoArt, aspect_ratio: str, out_path: Path) -> Path:
    size = CANVAS_SIZES.get(aspect_ratio, (1024, 1024))
    img = _render_designed(art, size) if art.composition in LAYOUT_BOXES else _render_classic(art, size)
    _demo_badge(img, art)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, "PNG")
    return out_path
