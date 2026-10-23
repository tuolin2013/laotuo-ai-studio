"""演示模式的三维设计数据：美学流派色板 / 字体 / 光线、构图留白说明，以及「自动匹配」规则。

数值与 config/skills/image_prompt/references/ecommerce_aesthetics_guide.md 保持一致；
真实模式下由策划 / 生图 Skill 读取指南自行匹配，这里只服务零 Key 演示。
"""

from __future__ import annotations

from dataclasses import dataclass

TEA = "zhangjiajie_berry_tea"


@dataclass(frozen=True)
class StyleSpec:
    background: tuple[str, str]  # 渐变上 / 下（上方通常是主要留白区）
    text: str  # 主标题色（与留白底高反差）
    sub: str  # 副标题色
    accent: str  # 胶囊角标 / 点睛色
    font: str  # serif / sans / rounded
    seal: bool = False  # 朱砂红小印章
    light: str = ""  # 光线（英文，供演示提示词）
    font_en: str = ""  # 字体描述（英文，指南第三维「字体与层级」）


SEAL_RED = "#B83B2A"
FONT_NAMES_ZH = {"serif": "宋刻衬线体，旁缀朱砂小印章", "sans": "现代粗黑无衬线体", "rounded": "亲和圆黑体"}
SERIF_EN = ("elegant high-contrast Chinese Songti (Ming serif) typography with refined sharp strokes and generous "
            "letter-spacing, accented by a small traditional vermilion red seal stamp")
SANS_EN = ("bold modern geometric Chinese Heiti (sans-serif) typography with heavy uniform stroke weight and clean "
           "Swiss-grid alignment")
ROUNDED_EN = "warm rounded modern Chinese sans-serif typography with soft approachable edges"

STYLES: dict[str, StyleSpec] = {
    "new_chinese_zen": StyleSpec(
        ("#1B3328", "#1E1E1C"), "#F5F2EB", "#D4AF37", "#D4AF37", "serif", seal=True,
        light="soft 45-degree morning side-backlight through thin mountain mist, gentle Tyndall rays, "
              "warm wooden bounce light, chiaroscuro",
        font_en=SERIF_EN),
    "organic_nature_sunlight": StyleSpec(
        ("#F4F7F4", "#D8D2C5"), "#1B3328", "#4A7C59", "#4A7C59", "serif", seal=True,
        light="clear 5500K early-morning natural sunlight, high dynamic range, mountains and sea of clouds "
              "softly blurred with a wide aperture",
        font_en=SERIF_EN),
    "clinical_lab_minimal": StyleSpec(
        ("#F8FAFC", "#E2E8F0"), "#222222", "#0284C7", "#0284C7", "sans",
        light="high-key shadowless studio softbox lighting, razor-clean edges, no messy shadows",
        font_en=SANS_EN),
    "nordic_warm_lifestyle": StyleSpec(
        ("#FAF6F0", "#E6DEC8"), "#3E2F23", "#7D8C75", "#E08E45", "rounded",
        light="warm 4000K late-afternoon sunlight slanting in, soft window gobo shadows on the table",
        font_en=ROUNDED_EN),
    "high_impact_retail": StyleSpec(
        ("#0D3B2E", "#082A20"), "#FFD700", "#FFFFFF", "#FFD700", "sans",
        light="hard commercial flash with a strong rim light that separates the product like a 3D relief",
        font_en=SANS_EN),
}

# 新中式 / 山野 + 三分留白：主体放左下，右上方竖排宋体标题（指南排版方案 F 东方竖排）
VERTICAL_STYLES = frozenset({"new_chinese_zen", "organic_nature_sunlight"})

# 构图 -> (写进 visual_description 的中文留白说明, 演示提示词英文描述)
LAYOUTS: dict[str, tuple[str, str]] = {
    "rule_of_thirds_negative_space": (
        "构图：三分留白，主体位于右下三分点、约占画面 55%，左上方预留约 35% 干净留白区排版标题",
        "rule-of-thirds composition: the product sits on the lower-right third occupying about 55% of the frame, "
        "with a clean 35% negative space in the upper-left reserved for Chinese typography"),
    "extreme_macro_texture": (
        "构图：光学微距满画幅，质地细节约占 70%，右上角保留约 25% 柔焦暗部留白区排字",
        "extreme macro close-up with a 100mm f/2.8 lens: the texture fills about 70% of the frame, with a soft "
        "out-of-focus 25% negative space in the upper-right corner for typography"),
    "depth_layering": (
        "构图：前中远三层景深，前景微虚化道具约 15%，中景主体完全清晰，顶部约 30% 虚化远景留白区排字",
        "three-layer depth composition: a slightly blurred foreground prop (about 15%), the product razor-sharp in "
        "the midground, and a softly blurred background leaving a clean 30% band at the top for typography"),
    "diagonal_dynamic_flow": (
        "构图：45° 对角线动态，动作沿左上到右下贯穿，右上方三角区预留约 30% 留白排字",
        "45-degree diagonal dynamic composition frozen at 1/2000s: the motion runs from upper-left to lower-right, "
        "leaving a clean 30% triangle of negative space in the upper-right for typography"),
    "split_screen_comparison": (
        "构图：分屏对比，左侧 70% 为本品（暖光、高清），右侧 30% 为普通对照（冷灰光、客观呈现），顶部约 20% 通栏标题区",
        "split-screen comparison: the left 70% shows our product in warm crisp studio light, the right 30% shows an "
        "ordinary reference in neutral cool light presented objectively, with a clean 20% title band across the top"),
    "knolling_flat_lay": (
        "构图：90° 俯拍秩序平铺，物品按网格整齐排列并带真实接触阴影，上方约 25% 干净桌面留白区排字",
        "90-degree top-down knolling flat lay: items arranged on a neat grid with soft contact shadows, leaving a "
        "clean 25% area at the top for typography"),
}
VERTICAL_LAYOUT = (
    "构图：三分留白，主体位于左下三分点、约占画面 55%，右上方预留约 35% 干净留白区，"
    "排 1~2 列竖排宋体标题并缀朱砂小印章",
    "rule-of-thirds composition: the product sits on the lower-left third occupying about 55% of the frame, with a "
    "clean 35% negative space in the upper-right holding one or two columns of vertical Chinese Songti typography "
    "with a small vermilion seal")


def layout(composition: str, aesthetic: str) -> tuple[str, str] | None:
    if composition == "rule_of_thirds_negative_space" and aesthetic in VERTICAL_STYLES:
        return VERTICAL_LAYOUT
    return LAYOUTS.get(composition)


# 槽位类型 kind：hero 首图 / macro 微距视觉锤 / experience 实物体验 / pain 痛点 / proof 检测证据 / origin 产地氛围 /
# lifestyle 使用场景 / spec 规格 / white 平台白底图 / compare 选购对比 / promise 售后承诺
COMPOSITION_BY_KIND: dict[str, str] = {
    "hero": "rule_of_thirds_negative_space",
    "macro": "extreme_macro_texture",
    "experience": "depth_layering",
    "pain": "rule_of_thirds_negative_space",
    "proof": "knolling_flat_lay",
    "origin": "rule_of_thirds_negative_space",
    "lifestyle": "diagonal_dynamic_flow",
    "spec": "knolling_flat_lay",
    "compare": "split_screen_comparison",
    "promise": "rule_of_thirds_negative_space",
    "white": "auto",  # 平台白底图：纯白居中，不套用留白构图
}


def auto_aesthetic(category_id: str, platform_id: str, kind: str) -> str:
    """按指南第四部分的映射表，为演示槽位挑选美学流派。"""
    if platform_id == "pinduoduo" and kind in {"hero", "spec"}:
        return "high_impact_retail"
    if kind in {"proof", "spec", "white", "compare"}:
        return "clinical_lab_minimal"
    if kind in {"pain", "lifestyle", "promise"}:
        return "nordic_warm_lifestyle"
    if category_id == TEA:
        if kind == "experience":
            return "nordic_warm_lifestyle" if platform_id == "wechat_shop" else "organic_nature_sunlight"
        return "organic_nature_sunlight" if kind == "origin" else "new_chinese_zen"
    # 宠物营养品等其他类目：全系科学实证，使用环境氛围用北欧暖阳
    return "nordic_warm_lifestyle" if kind == "origin" else "clinical_lab_minimal"


def pill_from_copy(copy_text: str, title: str, subtitle: str) -> str:
    """上图文案去掉主副标题后剩下的 2~8 字短语，作为三级信任胶囊角标。"""
    rest = copy_text or ""
    for part in (title, subtitle):
        if part:
            rest = rest.replace(part, "", 1)
    rest = rest.strip(" ·|｜/，,")
    return rest if 2 <= len(rest) <= 8 else ""


def hex_rgb(code: str) -> tuple[int, int, int]:
    code = code.lstrip("#")
    return int(code[0:2], 16), int(code[2:4], 16), int(code[4:6], 16)
