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


# Noto CJK 的 .ttc 里第 0 个字形是日文（JP），简体中文（SC）是第 2 个：不指定时 Linux 上会用日文字形
_NOTO_CJK_SC_INDEX = 2


def ttc_index(path: str) -> int:
    """字体集合文件里要用的字形序号：Noto CJK 取简体中文，其他字体取第一个。"""
    return _NOTO_CJK_SC_INDEX if Path(path).name.startswith(("NotoSansCJK", "NotoSerifCJK")) else 0


# 美学流派绑定的字体气质：宋刻衬线 / 亲和圆体；找不到时退回黑体
_STYLED = {
    "serif": [
        "C:/Windows/Fonts/STSONG.TTF",
        "C:/Windows/Fonts/simsun.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    ],
    "rounded": [
        "C:/Windows/Fonts/SIMYOU.TTF",
    ],
}


@cache
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _BOLD if bold else _REGULAR:
        if Path(path).exists():
            return ImageFont.truetype(path, size, index=ttc_index(path))
    # 没有中文字体时退化为 PIL 内置字体（中文会显示为方框，但不影响流程）
    return ImageFont.load_default(size)


@cache
def styled_font(size: int, kind: str = "sans", bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """kind：serif（宋刻衬线）/ rounded（圆体）/ sans（黑体）。"""
    for path in _STYLED.get(kind, []):
        if Path(path).exists():
            return ImageFont.truetype(path, size, index=ttc_index(path))
    return font(size, bold)


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
