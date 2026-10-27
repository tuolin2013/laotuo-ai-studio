"""工作台顶部「👑 老庹 AI 工坊」入口：开源 / Pro 卡带加载状态 + 商业版与定制服务说明。

入口是品牌栏右侧的一枚小徽章，点开才展开说明面板，不打扰日常操作。
"""

from __future__ import annotations

import gradio as gr

from app.engine.skill_loader import SkillRegistry
from app.schemas.skill import SKILL_SLOTS, SkillCard

BADGE_LABEL = "👑 老庹 AI 工坊 · 定制行业专属卡带 / 商业版"
# 商务联系方式：填入真实信息时，README.md「📬 商务合作」一节同步修改
CONTACTS = (("微信", "（待填写）"), ("邮箱", "（待填写）"), ("公众号", "（待填写）"))

COMMERCIAL_MD = (
    "#### 💼 版本与定制服务\n\n"
    "| 版本 | 包含内容 |\n|---|---|\n"
    "| 🌱 开源社区版（免费） | 完整工作台底座、通用策划卡带、零 Key 演示模式，本地自部署 |\n"
    "| 👑 商业专业版（Pro 卡带包） | 行业高转化卡带包（张家界莓茶 / 传统茗茶、宠物营养补充剂等），"
    "解压到 `config/skills_pro/` 即用；持续更新的爆款脚本库 |\n"
    "| 🏢 企业定制版 | 免配置云端独享工作台（含 Modal H3 视频后端）、品牌专属 SKU 卡带一对一定制、"
    "成品视觉包代制作 |\n\n"
    "**📬 商务合作 / 定制专属行业卡带 / 云端独享版**："
    + "　｜　".join(f"{k} {v}" for k, v in CONTACTS)
    + "\n\n开源底座按 Apache-2.0 附加条件发布：个人学习与自营店铺可免费使用；"
    "对外提供付费托管、去除品牌标识或转售本软件，需取得老庹 AI 工作室的书面授权（详见仓库 LICENSE）。"
)


def skill_status_md(skills: SkillRegistry, labels: dict[str, str]) -> str:
    """当前已加载的开源 / Pro 卡带数量（按卡槽）与 Pro 卡带清单。"""
    rows, pro_lines = [], []
    n_open = n_pro = 0
    for slot in SKILL_SLOTS:
        cards = skills.list(slot)
        pro = [c for c in cards if c.is_pro]
        n_open += len(cards) - len(pro)
        n_pro += len(pro)
        label = labels.get(slot, slot)
        rows.append(f"| {label} | {len(cards) - len(pro)} | {len(pro) or '—'} |")
        for c in pro:
            pro_lines.append(f"- {c.display_name}（{label}）")
            pro_lines += inheritance_lines(skills, c)
    table = "| 卡槽 | 🌱 开源 | 👑 Pro |\n|---|---|---|\n" + "\n".join(rows)
    if pro_lines:
        head = f"#### 卡带状态\n\n🌱 开源卡带 {n_open} 张　·　👑 Pro 卡带 {n_pro} 张（`config/skills_pro/`）"
        detail = "已加载的 Pro 卡带：\n\n" + "\n".join(pro_lines)
    else:
        head = f"#### 卡带状态\n\n🌱 开源卡带 {n_open} 张　·　👑 未安装 Pro 卡带包"
        detail = ("拿到 Pro 卡带包后，把 zip 解压到项目的 `config/skills_pro/` 文件夹，"
                  "再点「🔌 插卡式 Skill 管理」里的「🔄 重新扫描本地 .md 卡带」即可使用，不用重启。")
    errors = [e for p, e in skills.errors.items() if p.is_relative_to(skills.pro_dir)]
    if errors:
        detail += "\n\n" + "\n".join(f"- ⚠️ 无法加载：{e}" for e in errors)
    return f"{head}\n\n{table}\n\n{detail}"


def inheritance_lines(skills: SkillRegistry, card: SkillCard) -> list[str]:
    """继承了别的卡带时：继承链 + 本卡改写 / 删除 / 新增 / 追加了哪些区块（缩进的子列表）。"""
    if card.extends is None:
        return []
    lines = [f"  - 继承：{' → '.join(card.inheritance_chain[:-1])}"]
    for kind, titles in skills.changes(card).items():
        lines.append(f"  - {kind}：{'、'.join(t.split('（')[0] for t in titles)}")
    return lines


def panel_updates(open_now: bool, skills: SkillRegistry, labels: dict[str, str]) -> tuple:
    """切换面板：返回 (面板可见性, 新的展开状态, 卡带状态)。展开时才统计卡带，收起时不刷新。"""
    status = skill_status_md(skills, labels) if open_now else gr.update()
    return gr.update(visible=open_now), open_now, status


def pro_badge() -> gr.Button:
    """品牌栏右侧的小徽章（在调用处的布局里创建）。"""
    return gr.Button(BADGE_LABEL, size="sm", scale=0, min_width=0, elem_classes=["pro-badge"])


def build_pro_panel(skills: SkillRegistry, badge: gr.Button, labels: dict[str, str]) -> gr.Column:
    """徽章下方的说明面板，默认收起；点徽章展开 / 收起。"""
    opened = gr.State(False)
    with gr.Column(visible=False, elem_classes=["pro-panel"]) as panel:
        with gr.Row(elem_classes=["pro-panel-head"]):
            gr.Markdown("### 👑 老庹 AI 工坊 · 开源与商业版", elem_classes=["pro-title"])
            close = gr.Button("收起", size="sm", scale=0, min_width=0, elem_classes=["pro-close"])
        with gr.Row(elem_classes=["pro-grid"]):
            status = gr.Markdown(elem_classes=["pro-status"])
            gr.Markdown(COMMERCIAL_MD, elem_classes=["pro-offer"])

    badge.click(lambda is_open: panel_updates(not is_open, skills, labels), opened, [panel, opened, status],
                show_progress="hidden")
    close.click(lambda: panel_updates(False, skills, labels), None, [panel, opened, status], show_progress="hidden")
    return panel
