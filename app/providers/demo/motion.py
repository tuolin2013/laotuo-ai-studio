"""演示模式动效导演：按 motion_director 卡带的编排规则在本地排出动效方案，输出与真实 Skill 完全一致的 JSON。

文案只取自类目配置与商品信息（卖点、痛点、价格活动），不编造数字；没有可靠数字时不排「数据冲击」屏。
「AI 生图」方式下按「钩子背景 → 缺素材的主视觉 → 收尾使用场景 → 卖点配图」的顺序写 AI 画面描述，
画面取自类目配置的视频场景建议与道具，有商品图时作为生图参考。
"""

from __future__ import annotations

import re
from typing import Any

from app.engine.compliance import sanitize
from app.providers.demo.content import short
from app.schemas.config import CategoryConfig, PlatformConfig
from app.schemas.motion import (
    MAX_HEADLINE_CHARS,
    MAX_ITEM_CHARS,
    MAX_STAT_CHARS,
    MAX_SUBLINE_CHARS,
    MAX_VISUAL_CHARS,
)

# 可以做「数据冲击」的数字：百分比、含量、重量、规格（如 39.3%、127g、0.4g）
_STAT = re.compile(r"(\d+(?:\.\d+)?\s*(?:%|％|g|kg|克|ml|mL|毫升|片|粒|袋|罐|盒))")
_PAREN = re.compile(r"[（(]([^）)]+)[）)]")
_CLAUSE = re.compile(r"[，,。；;！!]")


def _cut(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len("".join(text.split())) <= limit else text[:limit]


def _fit(text: str, limit: int) -> str:
    """取第一个小句；超长时按空格整词截断（不把「FHN20260732023」这样的编号截成半截）。"""
    first = short(text, 1000)
    if len("".join(first.split())) <= limit:
        return first
    words, out = first.split(" "), ""
    for w in words:
        cand = f"{out} {w}".strip()
        if len("".join(cand.split())) > limit:
            break
        out = cand
    return out if len(out) >= 4 else first[:limit]


# 各类分镜的拍数权重：按目标时长等比缩放（钩子另由纠正规则限制在 3 秒内）
BEAT_WEIGHTS = {"hook": 4, "contrast": 7, "hero": 6, "stat": 5, "features": 8, "steps": 8, "gallery": 6,
                "showcase": 8, "cta": 7}
# 痛点对比配对时不算的常用字（「不知道」和「不寡淡」不能因为都有「不」就配成一对）
_STOP_CHARS = set("的了是一不和与或都也在有就又很更")


def _find_stat(texts: list[str]) -> tuple[str, str, str] | None:
    """在卖点 / 商品信息里找一个带单位的数字：返回 (主标题, 数字, 副标题)。"""
    for text in texts:
        for clause in _CLAUSE.split(text or ""):
            m = _STAT.search(clause)
            if not m:
                continue
            value = m.group(1).replace(" ", "").replace("％", "%")
            head = re.sub(r"[\s：:·]+$", "", clause[: m.start()]).strip()
            paren = _PAREN.search(clause[m.end():])
            sub = paren.group(1).strip() if paren else ""
            if head and len(value) <= MAX_STAT_CHARS:
                return _cut(head, MAX_HEADLINE_CHARS), value, _cut(sub, MAX_SUBLINE_CHARS)
    return None


def _clauses(text: str) -> list[str]:
    return [c.strip() for c in _CLAUSE.split(re.sub(r"[、]", "，", text or "")) if c.strip()]


def _contrast(cat: CategoryConfig, clean, skip_first: bool) -> tuple[str, str] | None:
    """痛点对比：「之前」是某个痛点的首句，「之后」是和这个痛点用字最接近的卖点小句（两边都来自类目配置，不编造）。

    钩子已经用了第一个痛点时（skip_first）先试后面的痛点，免得两屏说同一句话。找不到相关的卖点时不排这一屏。
    """
    pains = [clean(p) for p in cat.pain_points]
    order = [*pains[1:], *pains[:1]] if skip_first else pains
    options = [c for p in cat.core_selling_points for c in _clauses(clean(p))
               if 4 <= len("".join(c.split())) <= MAX_ITEM_CHARS]
    for pain in order:
        before = next((c for c in _clauses(pain) if 3 <= len("".join(c.split())) <= MAX_ITEM_CHARS), "")
        words = set(pain) - _STOP_CHARS - set("，,。 ")
        scored = sorted(((len(words & set(c)), -k, c) for k, c in enumerate(options)), reverse=True)
        if before and scored and scored[0][0] >= 1:
            return before, scored[0][2]
    return None


def _pick(assets: list[dict], sources: tuple[str, ...], shapes: tuple[str, ...] | None = None,
          exclude: set[str] | None = None, n: int = 1) -> list[str]:
    exclude = exclude or set()
    out = []
    for src in sources:
        for a in assets:
            if a["key"] in exclude or a["key"] in out:
                continue
            if a["source"] == src and (shapes is None or a["shape"] in shapes):
                out.append(a["key"])
                if len(out) >= n:
                    return out
    return out


def _plain(text: str) -> str:
    """去掉括号里的补充说明（色号、温度、拍法），留下画面本身。"""
    return " ".join(_PAREN.sub("", text or "").split()).strip("，,。 ")


def _product_refs(assets: list[dict]) -> list[str]:
    """生图参考：拍到商品包装的图（包装参考图优先，其次主图、上传），最多 2 张，不用长图。"""
    pack = [a["key"] for a in assets if a["source"] == "参考图" and "product_package" in a["label"]]
    rest = _pick(assets, ("主图", "上传", "参考图"), ("方图", "竖图", "横图"), exclude=set(pack), n=2)
    return [*pack, *rest][:2]


class _Visuals:
    """「AI 生图」方式的画面描述：场景取自类目的视频场景建议与道具，色调与氛围取自类目视觉风格。"""

    def __init__(self, cat: CategoryConfig, assets: list[dict], clean, wants: list[str]):
        self.cat, self.clean = cat, clean
        self.refs = _product_refs(assets)
        self.scenes = [_plain(s) for s in cat.video_scene_suggestions if _plain(s)]
        self.props = [_plain(p) for p in cat.visual_style.props if _plain(p)]
        palette = "、".join(_plain(c) for c in cat.visual_style.palette[:3] if _plain(c))
        mood = _plain(cat.visual_style.mood)
        self.style = "，".join(x for x in (f"色调{palette}" if palette else "", f"氛围{mood}" if mood else "") if x)
        self.granted = set(wants)

    def _text(self, *parts: str) -> str:
        text = "，".join(p for p in (*parts, self.style, "画面中不要出现任何文字、logo 或水印") if p)
        return self.clean(text)[:MAX_VISUAL_CHARS]

    def _scene(self, i: int) -> str:
        return self.scenes[i % len(self.scenes)] if self.scenes else (self.props[0] if self.props else self.cat.name)

    def hook(self) -> dict[str, Any]:
        if "hook" not in self.granted:
            return {}
        bg = self._scene(1)
        return {"visual": self._text(bg, "全屏背景，主体放在画面下方，中部留出大面积干净背景"), "visual_refs": []}

    def hero(self, name: str) -> dict[str, Any]:
        if "hero" not in self.granted:
            return {}
        if self.refs:
            prop = self.props[0] if self.props else "简洁台面"
            desc = f"{name}商品包装居中摆放，按参考图保持包装外观不变，{prop}点缀"
        else:  # 没有商品图时不画包装特写，改画道具与原料
            desc = f"{'与'.join(self.props[:2]) or self.cat.name}的静物特写，居中构图，背景干净"
        return {"visual": self._text(desc, "柔和棚拍光"), "visual_refs": self.refs}

    def features(self) -> dict[str, Any]:
        if "features" not in self.granted:
            return {}
        detail = self.props[1] if len(self.props) > 1 else self._scene(2)
        return {"visual": self._text(f"{detail}特写", "构图简洁"), "visual_refs": []}

    def cta(self) -> dict[str, Any]:
        if "cta" not in self.granted:
            return {}
        place = "，商品放在画面一侧，按参考图保持包装外观不变" if self.refs else ""
        return {"visual": self._text(f"{self._scene(0)}{place}", "暖调自然光，构图干净"), "visual_refs": self.refs}


def demo_motion(assets: list[dict[str, Any]], cat: CategoryConfig, plat: PlatformConfig,
                product_info: dict[str, str], target_seconds: int, bpm: int, idea: str | None = None,
                product_name: str = "", asset_mode: str = "project", max_visuals: int = 4) -> dict[str, Any]:
    """assets 为 MotionAsset.payload() 列表（source / shape 为中文名）。"""
    clean = lambda t: sanitize(t, cat, plat)  # noqa: E731
    name = _cut(product_name or cat.name, 10)
    points = [clean(p) for p in cat.core_selling_points]
    used: set[str] = set()
    scenes: list[dict[str, Any]] = []

    def scene(kind: str, **fields: Any) -> None:
        base = {"kind": kind, "beats": 0, "headline": "", "accent": "", "subline": "", "items": [], "stat_value": "",
                "images": [], "camera": "push_in", "transition_out": "push", "visual": "", "visual_refs": []}
        base.update(fields)
        used.update(base["images"])
        scenes.append(base)

    hero = _pick(assets, ("主图", "视频首帧", "上传", "参考图", "详情页"))
    wants: list[str] = []
    if asset_mode == "generate":  # AI 画面的优先顺序，按预算截取
        wants = ["hook", *([] if hero else ["hero"]), "cta", "features"][:max(0, max_visuals)]
    ai = _Visuals(cat, assets, clean, wants)

    # 1. 钩子：用户想法优先，否则用第一个痛点
    pain = clean(cat.pain_points[0]) if cat.pain_points else ""
    hook = _cut(idea, 12) if idea else (short(pain, 9) + "？" if pain else f"认识一下 {name}")
    bg = _pick(assets, ("视频首帧", "主图"), ("竖图", "方图", "横图"))
    hook_ai = ai.hook()
    scene("hook", headline=hook, subline=_cut(short(points[0], 20), MAX_SUBLINE_CHARS) if points and not idea else "",
          images=bg[:1] if (idea or not pain) and not hook_ai else [], transition_out="zoom", **hook_ai)
    # 1.5 痛点对比（20 秒左右及更长的片子才排）：之前是痛点，之后是回应它的卖点，都取自类目配置
    pair = _contrast(cat, clean, skip_first=bool(pain) and not idea) if target_seconds >= 18 else None
    if pair:
        scene("contrast", headline="差别在这里", accent="差别", items=list(pair), camera="still")
    # 2. 主视觉：项目里没有商品图时由 AI 画面补上
    hero = _pick(assets, ("主图", "视频首帧", "上传", "参考图", "详情页"), exclude=used)
    hero_ai = {} if hero else ai.hero(name)
    if hero or hero_ai:
        sub = short(points[0], 20) if points else plat.name
        scene("hero", headline=name, accent="", subline=sub, images=hero, camera="push_in", **hero_ai)
    # 3. 数据冲击：只用配置 / 商品信息里真实存在的数字
    stat = _find_stat([*product_info.values(), *points])
    if stat:
        scene("stat", headline=stat[0], stat_value=stat[1], subline=stat[2], camera="still")
    # 4. 卖点清单
    items = []
    for p in points[1:] if hero else points:
        item = _fit(p, MAX_ITEM_CHARS)
        if item and item not in items and (not stat or stat[1] not in p) and (not pair or item != pair[1]):
            items.append(item)
    if len(items) >= 2:
        img = _pick(assets, ("主图", "详情页", "参考图"), exclude=used)
        scene("features", headline="看得见的讲究", items=items[:3],
              images=img, camera="pull_out", transition_out="wipe", **({} if img else ai.features()))
    # 5. 图集
    gallery = _pick(assets, ("主图", "视频首帧", "详情页"), ("方图", "竖图", "横图"), exclude=used, n=4)
    if len(gallery) >= 2:
        scene("gallery", headline="每一面都经得起看", images=gallery, camera="push_in")
    # 6. 长图滚动 / 手机里播放分镜视频
    show = (_pick(assets, ("详情页长图",)) or _pick(assets, ("界面截图",), exclude=used)
            or _pick(assets, ("分镜视频",), exclude=used))
    if show:
        source = next(a["source"] for a in assets if a["key"] == show[0])
        title = {"详情页长图": "详情一屏一屏看清楚", "界面截图": "打开就能用"}.get(source, "上手就是这个感觉")
        scene("showcase", headline=title, images=show, camera="still", transition_out="zoom")
    # 7. 收尾行动
    offer = clean(product_info.get("price_offer", ""))
    cta_ai = ai.cta()
    cta_img = [] if cta_ai else _pick(assets, ("主图", "上传"), exclude=used) or hero
    scene("cta", headline="喜欢就带回家", accent="带回家", stat_value=_fit(offer, MAX_STAT_CHARS) if offer else "",
          items=["立即下单"], images=cta_img[:1], camera="pull_out", transition_out="cut", **cta_ai)
    # 拍数：按权重分配到目标时长
    beat = 60 / bpm
    weights = [BEAT_WEIGHTS[s["kind"]] for s in scenes]
    k = target_seconds / beat / sum(weights)
    for s, w in zip(scenes, weights, strict=True):
        s["beats"] = max(2, round(w * k))
    # 相邻两屏交替推、拉
    cams = ("push_in", "pull_out")
    k = 0
    for s in scenes:
        if s["camera"] in cams:
            s["camera"] = cams[k % 2]
            k += 1
    n_ai = sum(bool(s["visual"]) for s in scenes)
    source = (f"{n_ai} 屏使用 AI 生图画面（确认描述后在 Step 2 生成），其余素材来自本项目（{len(assets)} 个）"
              if n_ai else f"全部素材来自本项目（{len(assets)} 个）")
    return {
        "title": _cut(f"{name} 动效广告", 20),
        "bpm": bpm,
        "notes": f"演示模式：钩子 → 主视觉 → 证据 → 卖点 → 收尾，约 {target_seconds} 秒，{source}",
        "scenes": scenes,
    }
