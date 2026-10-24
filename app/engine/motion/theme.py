"""动效画面主题：背景渐变、环境光斑、文字色、强调渐变、卡片质感与配乐调式。

- keynote_dark「发布会暗场」：近黑底 + 青蓝紫三团缓慢漂移的柔光，白字 + 青→蓝强调渐变；
- 其余 5 个与主图 / 详情页共用的美学流派，色板取自 app/providers/demo/aesthetics.py（与美学指南一致），
  让动效广告和同一项目的主图、详情页是一个视觉系统。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.engine.motion.typeset import hex_rgb
from app.providers.demo.aesthetics import STYLES

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]


@dataclass(frozen=True)
class Theme:
    key: str
    bg_top: RGB
    bg_bottom: RGB
    glows: tuple[RGB, ...]
    glow_opacity: float
    text: str
    sub: str
    accent: tuple[str, str]
    card_fill: RGBA
    card_border: RGBA
    shadow_opacity: float
    font_kind: str  # sans / serif / rounded
    dark: bool
    vignette: float
    grain: float
    # 配乐：主音（MIDI 音高）与调式
    music_root: int = 62
    music_mode: str = "major"

    @property
    def accent_rgb(self) -> RGB:
        return hex_rgb(self.accent[0])

    @property
    def ink_on_accent(self) -> str:
        """强调色胶囊上的文字颜色：按强调色亮度取黑或白。"""
        r, g, b = self.accent_rgb
        return "#0B0B0F" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#FFFFFF"


KEYNOTE_DARK = Theme(
    key="keynote_dark", bg_top=(12, 12, 17), bg_bottom=(5, 5, 8),
    glows=((13, 148, 136), (37, 99, 235), (124, 58, 237)), glow_opacity=0.34,
    text="#F5F5F7", sub="#A1A1AA", accent=("#5EEAD4", "#60A5FA"),
    card_fill=(255, 255, 255, 18), card_border=(255, 255, 255, 38), shadow_opacity=0.55,
    font_kind="sans", dark=True, vignette=0.24, grain=0.045, music_root=62, music_mode="major",
)


def _luma(rgb: RGB) -> float:
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def _mix(a: RGB, b: RGB, k: float) -> RGB:
    return tuple(round(x + (y - x) * k) for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


def _hex(rgb: RGB) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _from_style(key: str) -> Theme:
    spec = STYLES[key]
    top, bottom = hex_rgb(spec.background[0]), hex_rgb(spec.background[1])
    accent = hex_rgb(spec.accent)
    dark = _luma(top) < 110
    light_accent = _mix(accent, (255, 255, 255), 0.35 if dark else 0.0)
    deep_accent = _mix(accent, (0, 0, 0), 0.0 if dark else 0.25)
    return Theme(
        key=key, bg_top=top, bg_bottom=bottom,
        glows=(accent, _mix(accent, top, 0.5), _mix(accent, (255, 255, 255), 0.4)),
        glow_opacity=0.30 if dark else 0.22,
        text=spec.text, sub=spec.sub, accent=(_hex(deep_accent), _hex(light_accent)),
        card_fill=(255, 255, 255, 20) if dark else (255, 255, 255, 205),
        card_border=(255, 255, 255, 40) if dark else (0, 0, 0, 20),
        shadow_opacity=0.5 if dark else 0.16,
        font_kind=spec.font, dark=dark, vignette=0.22 if dark else 0.10, grain=0.04 if dark else 0.03,
        music_root={"new_chinese_zen": 57, "organic_nature_sunlight": 60, "clinical_lab_minimal": 64,
                    "nordic_warm_lifestyle": 65, "high_impact_retail": 62}.get(key, 62),
        music_mode="pentatonic" if key in {"new_chinese_zen", "organic_nature_sunlight"} else "major",
    )


THEMES: dict[str, Theme] = {"keynote_dark": KEYNOTE_DARK, **{k: _from_style(k) for k in STYLES}}


def get_theme(key: str) -> Theme:
    return THEMES.get(key, KEYNOTE_DARK)
