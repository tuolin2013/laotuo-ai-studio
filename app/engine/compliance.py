"""广告法 / 类目合规违禁词检查。"""

from __future__ import annotations

from app.schemas.config import CategoryConfig, PlatformConfig


def forbidden_words(category: CategoryConfig, platform: PlatformConfig | None = None) -> list[str]:
    words = list(category.forbidden_words)
    if platform:
        words += [w for w in platform.forbidden_words if w not in words]
    # 长词优先，避免“最好”被“最”抢先命中
    return sorted(words, key=len, reverse=True)


def find_violations(text: str, category: CategoryConfig, platform: PlatformConfig | None = None) -> list[str]:
    """返回文本中命中的违禁词（去重，按出现顺序）。"""
    if not text:
        return []
    hits: list[str] = []
    for w in forbidden_words(category, platform):
        if w in text and not any(w in h for h in hits):
            hits.append(w)
    return sorted(hits, key=text.find)


def sanitize(text: str, category: CategoryConfig, platform: PlatformConfig | None = None) -> str:
    """去除违禁词（演示模式生成文案时使用）。"""
    for w in forbidden_words(category, platform):
        text = text.replace(w, "")
    return text


def compliance_report(texts: dict[str, str], category: CategoryConfig, platform: PlatformConfig | None) -> str:
    """生成给界面展示的 Markdown 合规提示。texts 为 {字段名: 文本}。"""
    lines = []
    for label, text in texts.items():
        hits = find_violations(text or "", category, platform)
        if hits:
            lines.append(f"- ⚠️ **{label}** 命中违禁词：{'、'.join(hits)}")
    if not lines:
        return "✅ 合规检查：未发现广告法 / 类目违禁词"
    return "**合规检查未通过，请修改：**\n" + "\n".join(lines)
