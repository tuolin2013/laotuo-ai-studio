"""动效文字：字体查找、中文断行与「遮罩上升」逐字入场（kinetic type）。

- 断行优先在空格处：「霜多叶嫩 看得见」会排成两行，各行尽量等宽；「·」这类分隔符落在断行处时省略；
  单段超宽时逐字换行，行首不放标点；
- 逐字入场：每个字在自己的行框里从下方 105% 处升起（EASE_OUT），字与字错峰 45 毫秒左右；
  不透明度在前 15% 的进度里迅速到位，不做慢吞吞的整体淡入；
- 强调词用主题强调色渐变（每行一个强调词就够）；文字落定后整块缓存，之后每帧只贴一次。
"""

from __future__ import annotations

import contextlib
import itertools
import math
import threading
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.engine.motion.compose import paste, with_opacity
from app.engine.motion.easing import EASE_OUT, progress

_FONT_FILES: dict[tuple[str, str], list[str]] = {
    ("sans", "light"): ["C:/Windows/Fonts/msyhl.ttc", "C:/Windows/Fonts/Dengl.ttf",
                        "/System/Library/Fonts/PingFang.ttc",
                        "/usr/share/fonts/opentype/noto/NotoSansCJK-Light.ttc"],
    ("sans", "regular"): ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/Deng.ttf",
                          "/System/Library/Fonts/PingFang.ttc",
                          "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                          "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"],
    ("sans", "bold"): ["C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/Dengb.ttf",
                       "/System/Library/Fonts/PingFang.ttc",
                       "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"],
    ("serif", "regular"): ["C:/Windows/Fonts/STSONG.TTF", "C:/Windows/Fonts/simsun.ttc",
                           "/System/Library/Fonts/Supplemental/Songti.ttc",
                           "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"],
    ("serif", "bold"): ["C:/Windows/Fonts/STZHONGS.TTF", "C:/Windows/Fonts/STSONG.TTF",
                        "/System/Library/Fonts/Supplemental/Songti.ttc",
                        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"],
    ("rounded", "regular"): ["C:/Windows/Fonts/SIMYOU.TTF"],
    ("rounded", "bold"): ["C:/Windows/Fonts/SIMYOU.TTF"],
    # 数字：DIN 风格的 Bahnschrift（可变字体），没有时退回黑体
    ("numeric", "bold"): ["C:/Windows/Fonts/bahnschrift.ttf"],
    ("numeric", "regular"): ["C:/Windows/Fonts/bahnschrift.ttf"],
}
_VARIATION = {"bold": b"SemiBold", "regular": b"Regular", "light": b"Light"}
# 渲染是多线程逐帧进行的：同一个 FreeType 字体对象不能被两个线程同时用来画字
TEXT_LOCK = threading.Lock()
# 不能出现在行首的标点
_NO_LINE_START = set("，。、；：！？）》」』”’,.;:!?)%…·")


@cache
def font(size: int, weight: str = "bold", kind: str = "sans") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """kind：sans / serif / rounded / numeric；weight：light / regular / bold。找不到时逐级退回黑体。"""
    size = max(8, int(size))
    for k, w in ((kind, weight), (kind, "bold"), (kind, "regular"), ("sans", weight), ("sans", "regular")):
        for path in _FONT_FILES.get((k, w), []):
            if not Path(path).exists():
                continue
            f = ImageFont.truetype(path, size)
            if k == "numeric":
                with contextlib.suppress(OSError, ValueError, AttributeError):
                    f.set_variation_by_name(_VARIATION.get(w, b"Regular"))
            return f
    return ImageFont.load_default(size)


def text_width(text: str, fnt, tracking: float = 0.0) -> float:
    if not text:
        return 0.0
    return fnt.getlength(text) + tracking * (len(text) - 1)


def _char_wrap(text: str, fnt, max_width: float, tracking: float) -> list[str]:
    """逐字换行，但按行数均分（9 个字排两行是 5 + 4，不是 7 + 2 留一个孤字）；行首不放标点。"""
    total = text_width(text, fnt, tracking)
    n = max(1, int(-(-total // max_width)))
    while n <= len(text):
        target = total / n
        lines, line = [], ""
        for ch in text:
            if (line and len(lines) < n - 1 and ch not in _NO_LINE_START
                    and text_width(line + ch, fnt, tracking) > target * 1.04):
                lines.append(line)
                line = ch
            else:
                line += ch
        lines.append(line)
        if all(text_width(ln, fnt, tracking) <= max_width for ln in lines):
            return lines
        n += 1
    return list(text)


_SEPARATORS = {"·", "|", "｜"}  # 纯分隔符：在断行处省略，不挂在行尾、不放在行首
_BALANCE_LIMIT = 2000  # 均衡断行最多尝试的断点组合数


def _phrases(text: str) -> list[tuple[str, str]]:
    """按空格 / 换行切成 (分隔符, 短语)；分隔符记在后一个短语前，单独的标点贴到前一个短语末尾。"""
    out: list[tuple[str, str]] = []
    sep = ""
    for w in (p for p in text.replace("\n", " ").split(" ") if p):
        if w in _SEPARATORS:
            sep = w
        elif out and all(ch in _NO_LINE_START for ch in w):
            out[-1] = (out[-1][0], out[-1][1] + w)
        else:
            out.append((sep, w))
            sep = ""
    return out


def _join(units: list[tuple[str, str]]) -> str:
    return " ".join(f"{sep} {w}" if k and sep else w for k, (sep, w) in enumerate(units))


def layout_lines(text: str, fnt, max_width: float, tracking: float = 0.0) -> list[str]:
    """断行：先按空格 / 换行分成短语，在最少的行数里让各行尽量等宽（不留孤行）；单个短语超宽时逐字换行。"""
    units = _phrases(text)
    if not units:
        return []
    if any(text_width(w, fnt, tracking) > max_width for _, w in units):
        lines: list[str] = []
        for _, w in units:
            if lines and text_width(f"{lines[-1]} {w}", fnt, tracking) <= max_width:
                lines[-1] = f"{lines[-1]} {w}"
            elif text_width(w, fnt, tracking) <= max_width:
                lines.append(w)
            else:
                lines.extend(_char_wrap(w, fnt, max_width, tracking))
        return lines
    # 先贪心求最少行数，再在这个行数下找最长一行最短的断法
    groups: list[list[tuple[str, str]]] = []
    for u in units:
        if groups and text_width(_join([*groups[-1], u]), fnt, tracking) <= max_width:
            groups[-1].append(u)
        else:
            groups.append([u])
    n, m = len(groups), len(units)
    if n == 1 or math.comb(m - 1, n - 1) > _BALANCE_LIMIT:
        return [_join(g) for g in groups]
    best, best_w = groups, max(text_width(_join(g), fnt, tracking) for g in groups)
    for cuts in itertools.combinations(range(1, m), n - 1):
        bounds = (0, *cuts, m)
        trial = [units[a:b] for a, b in itertools.pairwise(bounds)]
        w = max(text_width(_join(g), fnt, tracking) for g in trial)
        if w < best_w - 0.5:
            best, best_w = trial, w
    return [_join(g) for g in best]


def fit_font(text: str, size: int, max_width: float, max_lines: int, weight: str = "bold", kind: str = "sans",
             tracking_em: float = 0.0, min_size: int = 12):
    """在 max_lines 行内放下 text 的最大字号（不超过 size）。返回 (字体, 行列表)。"""
    s = int(size)
    while True:
        f = font(s, weight, kind)
        lines = layout_lines(text, f, max_width, tracking_em * s)
        if len(lines) <= max_lines or s <= min_size:
            return f, lines
        s = max(min_size, int(s * 0.92))


def hex_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def horizontal_gradient(w: int, h: int, a: str, b: str) -> Image.Image:
    grad = Image.linear_gradient("L").rotate(90, expand=True).transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    mask = grad.resize((max(1, w), max(1, h)))
    return Image.composite(Image.new("RGB", mask.size, hex_rgb(b)), Image.new("RGB", mask.size, hex_rgb(a)), mask)


@dataclass
class TextStyle:
    size: int
    color: str = "#FFFFFF"
    weight: str = "bold"
    kind: str = "sans"
    tracking_em: float = 0.0
    line_gap: float = 0.22  # 行距：字号的倍数
    accent: tuple[str, str] | None = None  # 强调词渐变色
    shadow: float = 0.0  # 阴影不透明度（压在图片上的字用）
    max_lines: int = 3


@dataclass
class _Line:
    image: Image.Image  # 整行（含阴影边距）
    chars: list[tuple[int, int]]  # 每个字在行图里的 [x0, x1)
    x: int  # 行图在文字块里的左上角
    y: int


@dataclass
class KineticText:
    """一块可动画的多行文字。size 为排好版后的宽高；draw() 按时间把它画进图层。"""

    text: str
    style: TextStyle
    max_width: float
    align: str = "center"  # left / center / right
    accent_text: str = ""
    lines: list[_Line] = field(default_factory=list)
    size: tuple[int, int] = (0, 0)
    pad: int = 0
    font_size: int = 0
    _settled: Image.Image | None = None

    def __post_init__(self) -> None:
        st = self.style
        fnt, rows = fit_font(self.text, st.size, self.max_width, st.max_lines, st.weight, st.kind, st.tracking_em)
        self.font_size = getattr(fnt, "size", st.size)
        tracking = st.tracking_em * self.font_size
        ascent, descent = fnt.getmetrics()
        line_h = ascent + descent
        self.pad = pad = max(2, round(self.font_size * (0.12 if st.shadow else 0.04)))
        gap = round(self.font_size * st.line_gap)
        accent_spans = _accent_spans(rows, self.accent_text)
        widths = [round(text_width(r, fnt, tracking)) for r in rows]
        block_w = max(widths, default=0)
        y = 0
        for row, w, spans in zip(rows, widths, accent_spans, strict=True):
            img, chars = _render_line(row, fnt, tracking, line_h, pad, st, spans)
            x = 0 if self.align == "left" else (block_w - w) // 2 if self.align == "center" else block_w - w
            self.lines.append(_Line(img, chars, x, y))
            y += line_h + gap
        self.size = (block_w + 2 * pad, max(0, y - gap) + 2 * pad)
        self.line_height = line_h

    @property
    def char_count(self) -> int:
        return sum(len(ln.chars) for ln in self.lines)

    def settle_time(self, start: float = 0.0, stagger: float = 0.045, dur: float = 0.7) -> float:
        return start + max(0, self.char_count - 1) * stagger + dur

    def draw(self, layer: Image.Image, x: float, y: float, t: float, *, start: float = 0.0, stagger: float = 0.045,
             dur: float = 0.7, mode: str = "rise", opacity: float = 1.0) -> None:
        """(x, y) 是文字块左上角。mode：rise 逐字遮罩上升 / fade 整块上浮淡入 / none 直接显示。"""
        x, y = round(x), round(y)
        if mode == "none" or t >= self.settle_time(start, stagger, dur):
            paste(layer, self.settled(), x, y, opacity)
            return
        if mode == "fade":
            p = EASE_OUT(progress(t, start, dur))
            if p > 0:
                rise = round((1 - p) * self.font_size * 0.45)
                paste(layer, self.settled(), x, y + rise, opacity * min(1.0, p * 2.5))
            return
        k = 0
        for ln in self.lines:
            canvas = None
            h = ln.image.height
            for x0, x1 in ln.chars:
                p = EASE_OUT(progress(t, start + k * stagger, dur))
                k += 1
                if p <= 0:
                    continue
                if canvas is None:
                    canvas = Image.new("RGBA", ln.image.size, (0, 0, 0, 0))
                col = ln.image.crop((x0, 0, x1, h))
                dy = round((1 - p) * h * 1.05)
                paste(canvas, col, x0, dy, min(1.0, p / 0.15))
            if canvas is not None:
                paste(layer, canvas, x + ln.x, y + ln.y, opacity)

    def settled(self) -> Image.Image:
        if self._settled is None:
            img = Image.new("RGBA", self.size, (0, 0, 0, 0))
            for ln in self.lines:
                paste(img, ln.image, ln.x, ln.y)
            self._settled = img
        return self._settled


def _accent_spans(rows: list[str], accent: str) -> list[tuple[int, int] | None]:
    """每行里强调词的字符区间（强调词跨行时两行各取一部分）。"""
    out: list[tuple[int, int] | None] = [None] * len(rows)
    if not accent:
        return out
    joined = "".join(rows)
    pos = joined.find(accent)
    if pos < 0:
        return out
    a0, a1, offset = pos, pos + len(accent), 0
    for i, r in enumerate(rows):
        s, e = max(a0, offset), min(a1, offset + len(r))
        if s < e:
            out[i] = (s - offset, e - offset)
        offset += len(r)
    return out


def _render_line(row: str, fnt, tracking: float, line_h: int, pad: int, st: TextStyle,
                 span: tuple[int, int] | None) -> tuple[Image.Image, list[tuple[int, int]]]:
    w = round(text_width(row, fnt, tracking)) + 2 * pad
    h = line_h + 2 * pad
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    starts, cx = [], float(pad)
    with TEXT_LOCK:
        for ch in row:
            draw.text((cx, pad), ch, font=fnt, fill=255)
            starts.append(round(cx))
            cx += fnt.getlength(ch) + tracking
    # 字列切片：相邻字首尾相接，第一个字从行首、最后一个字到行尾（含阴影边距）
    cols = [(0 if i == 0 else x0, starts[i + 1] if i + 1 < len(starts) else w) for i, x0 in enumerate(starts)]
    color = Image.new("RGB", (w, h), hex_rgb(st.color))
    if span and st.accent:
        x0, x1 = cols[span[0]][0], cols[span[1] - 1][1]
        grad = horizontal_gradient(x1 - x0, h, *st.accent)
        color.paste(grad, (x0, 0))
    line = color.convert("RGBA")
    line.putalpha(mask)
    if st.shadow > 0:
        shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        sm = mask.filter(ImageFilter.GaussianBlur(max(1, pad * 0.6))).point(lambda v: round(v * st.shadow))
        shadow.putalpha(sm)
        base = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        paste(base, shadow, 0, max(1, pad // 3))
        base.alpha_composite(line)
        line = base
    return line, cols


def draw_static(layer: Image.Image, text: str, x: float, y: float, style: TextStyle, opacity: float = 1.0,
                anchor: str = "lt", max_width: float = 10_000) -> tuple[int, int, int, int]:
    """一次性画一行静态文字（标签、编号）。返回实际占用的框。"""
    kt = KineticText(text, style, max_width, align="left")
    w, h = kt.size
    ox = {"l": 0, "m": w / 2, "r": w}[anchor[0]]
    oy = {"t": 0, "m": h / 2, "b": h}[anchor[1]]
    left, top = round(x - ox), round(y - oy)
    paste(layer, with_opacity(kt.settled(), 1.0), left, top, opacity)
    return left, top, left + w, top + h
