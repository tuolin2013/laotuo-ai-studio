"""演示模式动效导演：按 motion_director 卡带的编排规则在本地排出动效方案，输出与真实 Skill 完全一致的 JSON。

文案只取自类目配置与商品信息（卖点、痛点、价格活动），不编造数字；没有可靠数字时不排「数据冲击」屏。
"""

from __future__ import annotations

import re
from typing import Any

from app.engine.compliance import sanitize
from app.providers.demo.content import short
from app.schemas.config import CategoryConfig, PlatformConfig
from app.schemas.motion import MAX_HEADLINE_CHARS, MAX_ITEM_CHARS, MAX_STAT_CHARS, MAX_SUBLINE_CHARS

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
BEAT_WEIGHTS = {"hook": 4, "hero": 6, "stat": 5, "features": 8, "gallery": 6, "showcase": 8, "cta": 7}


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


def demo_motion(assets: list[dict[str, Any]], cat: CategoryConfig, plat: PlatformConfig,
                product_info: dict[str, str], target_seconds: int, bpm: int, idea: str | None = None,
                product_name: str = "") -> dict[str, Any]:
    """assets 为 MotionAsset.payload() 列表（source / shape 为中文名）。"""
    clean = lambda t: sanitize(t, cat, plat)  # noqa: E731
    name = _cut(product_name or cat.name, 10)
    points = [clean(p) for p in cat.core_selling_points]
    used: set[str] = set()
    scenes: list[dict[str, Any]] = []

    def scene(kind: str, **fields: Any) -> None:
        base = {"kind": kind, "beats": 0, "headline": "", "accent": "", "subline": "", "items": [], "stat_value": "",
                "images": [], "camera": "push_in", "transition_out": "push"}
        base.update(fields)
        used.update(base["images"])
        scenes.append(base)

    # 1. 钩子：用户想法优先，否则用第一个痛点
    pain = clean(cat.pain_points[0]) if cat.pain_points else ""
    hook = _cut(idea, 12) if idea else (short(pain, 9) + "？" if pain else f"认识一下 {name}")
    bg = _pick(assets, ("视频首帧", "主图"), ("竖图", "方图", "横图"))
    scene("hook", headline=hook, subline=_cut(short(points[0], 20), MAX_SUBLINE_CHARS) if points and not idea else "",
          images=bg[:1] if idea or not pain else [], transition_out="zoom")
    # 2. 主视觉
    hero = _pick(assets, ("主图", "视频首帧", "上传", "参考图", "详情页"), exclude=used)
    if hero:
        sub = short(points[0], 20) if points else plat.name
        scene("hero", headline=name, accent="", subline=sub, images=hero, camera="push_in")
    # 3. 数据冲击：只用配置 / 商品信息里真实存在的数字
    stat = _find_stat([*product_info.values(), *points])
    if stat:
        scene("stat", headline=stat[0], stat_value=stat[1], subline=stat[2], camera="still")
    # 4. 卖点清单
    items = []
    for p in points[1:] if hero else points:
        item = _fit(p, MAX_ITEM_CHARS)
        if item and item not in items and (not stat or stat[1] not in p):
            items.append(item)
    if len(items) >= 2:
        img = _pick(assets, ("主图", "详情页", "参考图"), exclude=used)
        scene("features", headline="看得见的讲究", items=items[:3],
              images=img, camera="pull_out", transition_out="wipe")
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
    cta_img = _pick(assets, ("主图", "上传"), exclude=used) or hero
    scene("cta", headline="喜欢就带回家", accent="带回家", stat_value=_fit(offer, MAX_STAT_CHARS) if offer else "",
          items=["立即下单"], images=cta_img[:1], camera="pull_out", transition_out="cut")
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
    return {
        "title": _cut(f"{name} 动效广告", 20),
        "bpm": bpm,
        "notes": f"演示模式：钩子 → 主视觉 → 证据 → 卖点 → 收尾，约 {target_seconds} 秒，"
                 f"全部素材来自本项目（{len(assets)} 个）",
        "scenes": scenes,
    }
