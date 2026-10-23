"""本草性状图谱：把类目配置中的原料性状按单品整理成给人看的 Markdown（审图、拍原料照时对照）。

生成：python -m app.engine.herb_atlas  →  docs/herb_atlas_{category_id}.md
数据只来自 config/categories/{category_id}.json；改了数据要重新生成，测试会检查文档与配置一致。
"""

from __future__ import annotations

import sys
from itertools import combinations
from pathlib import Path
from urllib.parse import quote

from app.engine.config_loader import PROJECT_ROOT, load_categories
from app.schemas.config import CategoryConfig, IngredientVisual, ProductLine

DOCS_DIR = PROJECT_ROOT / "docs"
# 辅料：不入画，也不列入图谱
EXCIPIENTS = frozenset({"鸡肝粉", "糊精"})
TABLE_HEAD = ["| 原料 | 学名 · 入料部位与饮片形态 | 外观要点（药典性状） | 易混 | 依据 · 实物图 |",
              "|---|---|---|---|---|"]


def atlas_path(category_id: str) -> Path:
    return DOCS_DIR / f"herb_atlas_{category_id}.md"


def _photo_search(name: str) -> str:
    """网络图片搜索链接：只供人眼对照，不下载、不转存。"""
    return f"https://image.baidu.com/search/index?tn=baiduimage&word={quote(name + '饮片')}"


def _cell(text: str) -> str:
    return text.replace("|", "／").replace("\n", " ")


def _row(v: IngredientVisual, hero: bool) -> str:
    name = f"⭐ {v.name}" if hero else v.name
    if v.aliases:
        name += f"<br>（{'、'.join(v.aliases)}）"
    links = ([f"[药典 2020]({v.source_url})"] if v.source_url else []) + [f"[实物图]({_photo_search(v.name)})"]
    cells = [name, f"{v.botanical}<br>{v.part}", v.appearance, "、".join(v.confusable) or "—", " · ".join(links)]
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _herbs_of(line: ProductLine, by_name: dict[str, IngredientVisual]) -> list[IngredientVisual]:
    """推荐入画的原料在前（按推荐顺序），其余按包装原料顺序。"""
    hero = [by_name[n] for n in line.hero_ingredients if n in by_name]
    rest = [by_name[n] for n in line.ingredients if n in by_name and n not in line.hero_ingredients]
    return hero + rest


def _confusable_pairs(herbs: list[IngredientVisual]) -> list[str]:
    return [f"{a.name} ↔ {b.name}" for a, b in combinations(herbs, 2)
            if b.name in a.confusable or a.name in b.confusable]


def _product_section(line: ProductLine, by_name: dict[str, IngredientVisual]) -> list[str]:
    herbs = _herbs_of(line, by_name)
    title = f"## {line.name} · {line.subtitle}" + (f"（包装{line.package_color}）" if line.package_color else "")
    out = [title, "",
           f"- 原料组成：{'、'.join(line.ingredients)}",
           f"- 推荐入画：{'、'.join(line.hero_ingredients) or '—'}（外观差异大，同框不易画混）"]
    pairs = _confusable_pairs(herbs)
    if pairs:
        out.append(f"- 本款同框易混：{'；'.join(pairs)}")
    hero = set(line.hero_ingredients)
    return [*out, "", *TABLE_HEAD, *(_row(h, h.name in hero) for h in herbs), ""]


def render_atlas(cat: CategoryConfig) -> str:
    by_name = {v.name: v for v in cat.ingredient_visuals}
    herb_count = len({n for p in cat.product_lines for n in p.ingredients if n not in EXCIPIENTS})
    lines = [
        f"# {cat.name} · 本草性状图谱（按单品）",
        "",
        "> 外观依据《中国药典》2020 年版一部各品种的【性状】与饮片【性状】（链接为药典条文的在线转载）；"
        "片、段、块、丝的规格依据药典四部通则 0213。",
        "> 只记外观（形状、尺寸、颜色、断面、质地），不涉及原料的作用；上图文案同样只写原料名。",
        f"> 本文件由 `python -m app.engine.herb_atlas` 从 `config/categories/{cat.category_id}.json` 自动生成，"
        "请勿手改；数据改动后重新生成。",
        "",
        f"共 {len(cat.product_lines)} 款单品、{herb_count} 味草本原料（辅料{'、'.join(sorted(EXCIPIENTS))}不列入）。",
        "",
        "## 怎么用",
        "",
        "- **审图**：AI 出图后，对照「外观要点」逐味检查形状、颜色、切面和大小；「易混」列里的原料最容易被画成彼此。",
        "- **拍原料实拍图**（上传为参考图「草本原料实拍」`herb_ingredients`）：优先拍 ⭐ 推荐入画的几味，"
        "干燥饮片各放一小碟，纯色背景、自然光、俯拍，看得清形状、颜色与切面。",
        "- **实物图**链接打开的是网络图片搜索结果，只供人眼对照；网图版权不明，不要直接用作商品图或参考图。",
        "",
        "## 单品一览",
        "",
        "| 单品 | 副名 | 包装主色 | 草本原料 | 推荐入画 |",
        "|---|---|---|---|---|",
    ]
    for p in cat.product_lines:
        n = sum(1 for i in p.ingredients if i not in EXCIPIENTS)
        lines.append(f"| {p.name} | {p.subtitle} | {p.package_color} | {n} 味 | {'、'.join(p.hero_ingredients)} |")
    lines.append("")
    for p in cat.product_lines:
        lines += _product_section(p, by_name)
    shared = {}
    for p in cat.product_lines:
        for i in p.ingredients:
            if i not in EXCIPIENTS:
                shared.setdefault(i, []).append(p.name)
    multi = [(n, ps) for n, ps in shared.items() if len(ps) > 1]
    if multi:
        lines += ["## 附：多款共用的原料（拍一次，多款复用）", ""]
        lines += [f"- {n}：{'、'.join(ps)}" for n, ps in sorted(multi, key=lambda x: (-len(x[1]), x[0]))]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    cats = load_categories()
    ids = argv or [cid for cid, c in cats.items() if c.ingredient_visuals]
    DOCS_DIR.mkdir(exist_ok=True)
    for cid in ids:
        path = atlas_path(cid)
        path.write_text(render_atlas(cats[cid]), encoding="utf-8", newline="\n")
        print(f"已生成 {path.relative_to(PROJECT_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
