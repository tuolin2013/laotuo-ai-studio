"""插卡式 Skill 加载器：扫描 config/skills/{slot}/*.md，解析 YAML front matter，支持热重载。

同时扫描可选的本地 Pro 卡带包 config/skills_pro/{slot}/*.md（付费发放、不入开源仓库），目录存在才加载，
界面上名称前标记「[👑 Pro]」。
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.engine.config_loader import CONFIG_DIR
from app.schemas.skill import SKILL_SLOTS, SkillCard, SkillSlot

logger = logging.getLogger(__name__)

SKILLS_DIR = CONFIG_DIR / "skills"
# 本地 Pro 卡带包：与开源卡带目录同级，目录结构相同（{slot}/*.md + 可选 references/）
PRO_DIR_NAME = "skills_pro"
PRO_SKILLS_DIR = CONFIG_DIR / PRO_DIR_NAME


class SkillParseError(ValueError):
    pass


# 正文中引用的附属指南文件，如 `references/base-en.txt`
_REF_PATTERN = re.compile(r"references/[A-Za-z0-9_.\-]+\.(?:txt|md)")
MAX_REFERENCE_BYTES = 200_000


def _load_references(path: Path, declared: list[str], body: str,
                     fallback_dir: Path | None = None) -> tuple[dict[str, str], list[Path]]:
    """读取卡带声明或正文中引用的 references/* 附属文件（只允许卡带同目录下的 references/）。

    fallback_dir：Pro 卡带在自己目录找不到时，再到开源卡槽目录的 references/ 下找同名指南，
    这样 Pro 卡带包不必重复携带开源仓库已有的指南。返回 ({相对路径: 内容}, [实际文件])。
    """
    names = list(dict.fromkeys([*declared, *_REF_PATTERN.findall(body)]))
    bases = [path.parent.resolve()] + ([fallback_dir.resolve()] if fallback_dir else [])
    texts: dict[str, str] = {}
    files: list[Path] = []
    for rel in names:
        found: Path | None = None
        for base in bases:
            target = (base / rel).resolve()
            if not target.is_relative_to(base / "references"):
                raise SkillParseError(f"{path.name}：附属文件 {rel} 必须位于同目录的 references/ 下")
            if target.is_file():
                found = target
                break
        if found is None:
            raise SkillParseError(f"{path.name}：找不到附属文件 {rel}")
        if found.stat().st_size > MAX_REFERENCE_BYTES:
            raise SkillParseError(f"{path.name}：附属文件 {rel} 超过 {MAX_REFERENCE_BYTES} 字节")
        texts[rel] = found.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        files.append(found)
    return texts, files


def parse_skill_file(path: Path, slot: SkillSlot, is_pro: bool = False,
                     fallback_dir: Path | None = None) -> SkillCard:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise SkillParseError(f"{path.name}：缺少顶部 YAML 元数据（--- 开头）")
    end = text.find("\n---", 4)
    if end == -1:
        raise SkillParseError(f"{path.name}：YAML 元数据缺少结束分隔符 ---")
    try:
        meta = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as e:
        raise SkillParseError(f"{path.name}：YAML 解析失败：{e}") from e
    if not isinstance(meta, dict):
        raise SkillParseError(f"{path.name}：YAML 元数据必须是键值对")
    body = text[end + 4 :].lstrip("\n")
    try:
        card = SkillCard(**meta, slot=slot, body=body, source_path=path, is_pro=is_pro)
    except (ValidationError, TypeError) as e:
        raise SkillParseError(f"{path.name}：元数据不合法：{e}") from e
    texts, files = _load_references(path, card.references, body, fallback_dir)
    return card.model_copy(update={"reference_texts": texts, "reference_files": files}) if texts else card


def _signature(path: Path, fallback_dir: Path | None = None) -> tuple[float, float]:
    """卡带及其 references/ 目录的修改时间：附属文件改动同样触发热重载。"""
    ref_mtime = 0.0
    for ref_dir in (path.parent / "references", *([fallback_dir / "references"] if fallback_dir else [])):
        if ref_dir.is_dir():
            ref_mtime = max([ref_mtime, *(p.stat().st_mtime for p in ref_dir.iterdir() if p.is_file())])
    return path.stat().st_mtime, ref_mtime


class SkillRegistry:
    """线程安全的 Skill 注册表。refresh() 只重新解析修改过的文件。

    pro_dir 默认为 skills_dir 同级的 skills_pro/；目录不存在时只加载开源卡带。
    """

    def __init__(self, skills_dir: Path = SKILLS_DIR, pro_dir: Path | None = None):
        self.skills_dir = Path(skills_dir)
        self.pro_dir = Path(pro_dir) if pro_dir is not None else self.skills_dir.with_name(PRO_DIR_NAME)
        self._cards: dict[SkillSlot, dict[str, SkillCard]] = {s: {} for s in SKILL_SLOTS}
        self._mtimes: dict[Path, tuple[float, float]] = {}
        self._overrides: dict[tuple[SkillSlot, str], str] = {}
        self.errors: dict[Path, str] = {}
        self._lock = threading.RLock()
        self.refresh()

    def refresh(self) -> bool:
        """扫描目录，新增 / 修改 / 删除的卡带都会生效。返回是否有变化。"""
        with self._lock:
            seen: set[Path] = set()
            changed = False
            new_cards: dict[SkillSlot, dict[str, SkillCard]] = {s: {} for s in SKILL_SLOTS}
            roots = [(self.skills_dir, False)]
            if self.pro_dir.is_dir():
                roots.append((self.pro_dir, True))
            for slot in SKILL_SLOTS:
                # 先开源后 Pro：skill_id 与开源卡带重复的 Pro 卡带报错，不覆盖开源卡带
                for root, is_pro in roots:
                    slot_dir = root / slot
                    if not slot_dir.is_dir():
                        continue
                    fallback = self.skills_dir / slot if is_pro else None
                    # 按修改时间排序：skill_id 重复时先放入的卡带优先，新放入的冲突卡带报错
                    for path in sorted(slot_dir.glob("*.md"), key=lambda p: (p.stat().st_mtime, p.name)):
                        seen.add(path)
                        mtime = _signature(path, fallback)
                        cached = self._find_cached(slot, path)
                        if cached is not None and self._mtimes.get(path) == mtime:
                            card = cached
                        else:
                            changed = True
                            self._mtimes[path] = mtime
                            try:
                                card = parse_skill_file(path, slot, is_pro=is_pro, fallback_dir=fallback)
                            except SkillParseError as e:
                                self.errors[path] = str(e)
                                logger.warning("跳过无法解析的 Skill：%s", e)
                                continue
                            self.errors.pop(path, None)
                        if card.skill_id in new_cards[slot]:
                            other = new_cards[slot][card.skill_id].source_path
                            self.errors[path] = f"{path.name}：skill_id {card.skill_id!r} 与 {other.name} 重复"
                            continue
                        new_cards[slot][card.skill_id] = card
            removed = set(self._mtimes) - seen
            for path in removed:
                self._mtimes.pop(path, None)
                self.errors.pop(path, None)
                changed = True
            self._cards = new_cards
            return changed

    def _find_cached(self, slot: SkillSlot, path: Path) -> SkillCard | None:
        for card in self._cards[slot].values():
            if card.source_path == path:
                return card
        return None

    def list(self, slot: SkillSlot, category_id: str | None = None) -> list[SkillCard]:
        """按类目过滤：类目专属卡带 + 通用卡带；类目专属排最前，其次 default: true（界面下拉框默认选中）。

        category_id 为 None 时返回全部卡带。
        """
        with self._lock:
            cards = [c for c in self._cards[slot].values() if c.applies_to(category_id)]
        return sorted(cards, key=lambda c: (not (category_id and category_id in c.categories), not c.default))

    def choices(self, slot: SkillSlot, category_id: str | None = None) -> list[tuple[str, str]]:
        """Gradio 下拉框选项：(显示名, skill_id)；Pro 卡带的显示名带「[👑 Pro]」标记。"""
        return [(f"{c.display_name}（{c.skill_id}）", c.skill_id) for c in self.list(slot, category_id)]

    def get(self, slot: SkillSlot, skill_id: str) -> SkillCard:
        """获取卡带；若界面上设置了临时覆盖，返回覆盖版本。"""
        with self._lock:
            try:
                card = self._cards[slot][skill_id]
            except KeyError:
                raise KeyError(f"卡槽 {slot} 中没有 Skill：{skill_id}") from None
            override = self._overrides.get((slot, skill_id))
            return card.with_override(override) if override is not None else card

    def set_override(self, slot: SkillSlot, skill_id: str, body: str) -> None:
        """临时覆盖当前 Skill 的指令正文（仅内存，不写回 .md 文件）。"""
        self.get(slot, skill_id)
        with self._lock:
            self._overrides[(slot, skill_id)] = body

    def clear_override(self, slot: SkillSlot, skill_id: str) -> None:
        with self._lock:
            self._overrides.pop((slot, skill_id), None)
