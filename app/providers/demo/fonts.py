"""演示渲染用中文字体查找与文本排版工具。"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from PIL import ImageDraw, ImageFont

_REGULAR = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/Deng.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]
_BOLD = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/Dengb.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]


@cache
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _BOLD if bold else _REGULAR:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    # 没有中文字体时退化为 PIL 内置字体（中文会显示为方框，但不影响流程）
    return ImageFont.load_default(size)


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_width: int, max_lines: int = 4) -> list[str]:
    """按像素宽度逐字换行（适配中文无空格文本）。"""
    lines: list[str] = []
    for para in text.splitlines() or [""]:
        line = ""
        for ch in para:
            if draw.textlength(line + ch, font=fnt) <= max_width:
                line += ch
                continue
            lines.append(line)
            line = ch
            if len(lines) >= max_lines:
                break
        lines.append(line)
        if len(lines) >= max_lines:
            break
    lines = [ln for ln in lines if ln][:max_lines]
    full = "".join(text.split())
    if lines and len("".join(lines)) < len(full):
        lines[-1] = lines[-1][:-1] + "…"
    return lines
