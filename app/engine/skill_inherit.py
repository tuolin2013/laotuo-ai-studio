"""Skill 卡带继承：按区块组织正文，子卡带 `extends` 父卡带后按区块 id 替换 / 追加 / 删除。

区块语法（Markdown 一级标题 + 尾部标记，渲染给模型前去掉标记）::

    # 角色 {#role}                         定义 / 整块替换父卡带同 id 区块
    # 文案规则 {#copywriting append}        追加到父区块末尾（prepend 则加在开头）
    # {#anti_duplicate remove}              删除父区块
    # 单品表 {#catalog after=task}          新区块插在指定区块之后（before=xxx 同理）
    # 输出格式 {#output_format final}       子卡带不能替换 / 删除，只能 append / prepend
    # 单品表 {#catalog abstract}            抽象区块：非抽象子卡带必须实现

层叠顺序：根卡带 → 中间层 → 本卡带；新区块默认插在本卡带上一个区块之后，
第一个新区块默认插在父卡带第一个 final 区块之前（没有 final 则放在最后）。

继承约束：只能继承同一卡槽的卡带；开源卡带不能继承 Pro 卡带；不能循环继承；extends 不超过 MAX_DEPTH 层。
"""

from __future__ import annotations

import contextlib
import re

from app.schemas.skill import SkillBlock, SkillCard

PREAMBLE = "_preamble"
MAX_DEPTH = 3

_HEAD = re.compile(r"^#[ \t]+(.*?)[ \t]*\{#([a-z0-9_]+)((?:[ \t]+[a-z]+(?:=[a-z0-9_]+)?)*)[ \t]*\}[ \t]*$")
_OPS = {"append", "prepend", "remove"}
_FLAGS = {"final", "abstract"}


class InheritanceError(ValueError):
    pass


class _Parsed:
    __slots__ = ("after", "before", "block", "op")

    def __init__(self, block: SkillBlock, op: str | None, after: str | None, before: str | None):
        self.block, self.op, self.after, self.before = block, op, after, before


def _strip(lines: list[str]) -> str:
    return "\n".join(lines).strip("\n")


def parse_blocks(body: str, origin: str) -> tuple[str, list[_Parsed]]:
    """把正文拆成 (区块标题之前的正文, 区块列表)。代码块（```）内的行不当作区块标题。"""
    preamble: list[str] = []
    heads: list[tuple[str, str, list[str]]] = []  # (block_id, title, tokens)
    bodies: list[list[str]] = []
    current: list[str] = preamble
    in_fence = False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else _HEAD.match(line)
        if m is None:
            current.append(line)
            continue
        current = []
        heads.append((m.group(2), m.group(1), m.group(3).split()))
        bodies.append(current)

    blocks: list[_Parsed] = []
    seen: set[str] = set()
    for (block_id, title, tokens), lines in zip(heads, bodies, strict=True):
        if block_id == PREAMBLE or block_id in seen:
            raise InheritanceError(f"区块 id {block_id!r} 重复或为保留字")
        seen.add(block_id)
        op = after = before = None
        flags: set[str] = set()
        for tok in tokens:
            key, _, value = tok.partition("=")
            if tok in _OPS:
                if op:
                    raise InheritanceError(f"区块 {block_id!r} 只能有一个操作（append / prepend / remove）")
                op = tok
            elif tok in _FLAGS:
                flags.add(tok)
            elif key in ("after", "before") and value:
                if after or before:
                    raise InheritanceError(f"区块 {block_id!r} 只能指定一个 after= / before=")
                after, before = (value, None) if key == "after" else (None, value)
            else:
                raise InheritanceError(f"区块 {block_id!r} 有无法识别的标记 {tok!r}")
        block = SkillBlock(block_id=block_id, title=title, content=_strip(lines), final="final" in flags,
                           abstract="abstract" in flags, origin=origin)
        blocks.append(_Parsed(block, op, after, before))
    return _strip(preamble), blocks


def render_blocks(blocks: list[SkillBlock]) -> str:
    parts = []
    for b in blocks:
        if b.block_id == PREAMBLE:
            if b.content:
                parts.append(b.content)
        else:
            parts.append(f"# {b.title}\n\n{b.content}" if b.content else f"# {b.title}")
    return "\n\n".join(parts) + "\n"


def _join(a: str, b: str) -> str:
    return "\n\n".join(x for x in (a, b) if x)


def _merge(parent: list[SkillBlock], child: list[_Parsed], child_id: str) -> list[SkillBlock]:
    merged = [b.model_copy() for b in parent]

    def index(block_id: str) -> int | None:
        return next((i for i, b in enumerate(merged) if b.block_id == block_id), None)

    last: int | None = None
    for p in child:
        cb, bid = p.block, p.block.block_id
        i = index(bid)
        if i is not None:
            pb = merged[i]
            if p.after or p.before:
                raise InheritanceError(f"区块 {bid!r} 已在父卡带中定义，不能再指定 after= / before=")
            if pb.final and p.op not in ("append", "prepend"):
                raise InheritanceError(f"区块 {bid!r} 在父卡带中为 final，只能 append / prepend，不能替换或删除")
            if p.op == "remove":
                merged.pop(i)
                last = i - 1 if i > 0 else None
                continue
            if p.op in ("append", "prepend"):
                content = _join(pb.content, cb.content) if p.op == "append" else _join(cb.content, pb.content)
                merged[i] = pb.model_copy(update={"content": content, "origin": f"{pb.origin} + {child_id}({p.op})"})
            else:
                merged[i] = cb.model_copy(update={"title": cb.title or pb.title, "final": cb.final or pb.final})
            last = i
            continue
        if p.op:
            raise InheritanceError(f"父卡带中没有区块 {bid!r}，不能 {p.op}")
        if p.after or p.before:
            anchor = index(p.after or p.before)
            if anchor is None:
                raise InheritanceError(f"区块 {bid!r} 的定位锚点 {p.after or p.before!r} 不存在")
            pos = anchor + 1 if p.after else anchor
        elif last is not None:
            pos = last + 1
        else:
            pos = next((k for k, b in enumerate(merged) if b.final), len(merged))
        merged.insert(pos, cb)
        last = pos
    return merged


def _dedupe(*seqs):
    return list(dict.fromkeys(x for seq in seqs for x in seq))


_INHERITED_META = ("description", "version", "target_models", "source_url")


def resolve_card(card: SkillCard, parent: SkillCard | None) -> SkillCard:
    """把原始卡带解析为最终卡带（展开继承、去掉区块标记）。parent 必须已解析。"""
    preamble, parsed = parse_blocks(card.body, card.skill_id)
    if parent is None:
        for p in parsed:
            if p.op or p.after or p.before:
                raise InheritanceError(
                    f"区块 {p.block.block_id!r} 使用了 append / remove / after 等继承操作，但卡带没有 extends")
        if not parsed:
            # 没有区块标记的传统卡带：正文原样保留
            blocks = [SkillBlock(block_id=PREAMBLE, content=preamble, origin=card.skill_id)]
            body = card.body
        else:
            blocks = ([SkillBlock(block_id=PREAMBLE, content=preamble, origin=card.skill_id)] if preamble else []) + [
                p.block for p in parsed]
            body = render_blocks(blocks)
        chain = [card.skill_id]
        update: dict = {}
    else:
        if preamble:
            raise InheritanceError("继承卡带（extends）的正文必须全部写在 `# 标题 {#区块id}` 区块中")
        blocks = _merge(parent.blocks, parsed, card.skill_id)
        body = render_blocks(blocks)
        chain = [*parent.inheritance_chain, card.skill_id]
        declared = card.model_fields_set
        update = {f: getattr(parent, f) for f in _INHERITED_META if f not in declared}
        update.update(
            variables=_dedupe(parent.variables, card.variables),
            references=_dedupe(parent.references, card.references),
            reference_texts={**parent.reference_texts, **card.reference_texts},
            reference_files=_dedupe(parent.reference_files, card.reference_files),
        )
    if not card.abstract:
        missing = [b.block_id for b in blocks if b.abstract]
        if missing:
            raise InheritanceError(f"没有实现抽象区块：{', '.join(missing)}")
    return card.model_copy(update={**update, "body": body, "blocks": blocks, "inheritance_chain": chain})


def resolve_slot(raw: dict[str, SkillCard]) -> tuple[dict[str, SkillCard], dict[str, str]]:
    """展开同一卡槽内全部原始卡带的继承关系。

    返回 (展开后的卡带, {skill_id: 失败原因})；父卡带失败时，继承它的卡带同样失败。
    """
    resolved: dict[str, SkillCard] = {}
    failed: dict[str, str] = {}

    def visit(sid: str, stack: tuple[str, ...]) -> SkillCard:
        if sid in resolved:
            return resolved[sid]
        if sid in failed:
            raise InheritanceError(failed[sid])
        card = raw[sid]
        try:
            parent = None
            if card.extends is not None:
                pid = card.extends
                if pid == sid or pid in stack:
                    raise InheritanceError(f"循环继承：{' → '.join([*stack, sid, pid])}")
                if pid not in raw:
                    raise InheritanceError(f"同一卡槽中找不到父卡带 {pid!r}")
                if raw[pid].is_pro and not card.is_pro:
                    raise InheritanceError(f"开源卡带不能继承 Pro 卡带 {pid!r}")
                try:
                    parent = visit(pid, (*stack, sid))
                except InheritanceError as e:
                    if str(e).startswith("循环继承"):
                        raise
                    raise InheritanceError(f"父卡带 {pid!r} 无法加载") from e
                # 继承链 [根, ..., 父]：父卡带已有 MAX_DEPTH 次 extends 时，本卡带就超限了
                if len(parent.inheritance_chain) > MAX_DEPTH:
                    raise InheritanceError(
                        f"继承层级超过 {MAX_DEPTH} 层：{' → '.join([*parent.inheritance_chain, sid])}")
            result = resolve_card(card, parent)
        except InheritanceError as e:
            failed[sid] = str(e)
            raise
        resolved[sid] = result
        return result

    for sid in raw:
        with contextlib.suppress(InheritanceError):
            visit(sid, ())
    return resolved, failed


def block_changes(card: SkillCard, parent: SkillCard | None) -> dict[str, list[str]]:
    """本卡带相对父卡带改动了哪些区块（区块标题）：{"新增": [...], "改写": [...], "追加": [...], "删除": [...]}。"""
    if parent is None:
        return {}
    me = card.skill_id
    inherited = {b.block_id for b in parent.blocks}
    changes: dict[str, list[str]] = {"新增": [], "改写": [], "追加": [], "删除": []}
    for b in card.blocks:
        if b.origin == me:
            changes["改写" if b.block_id in inherited else "新增"].append(b.title or b.block_id)
        elif b.origin.endswith((f"+ {me}(append)", f"+ {me}(prepend)")):
            changes["追加"].append(b.title or b.block_id)
    kept = {b.block_id for b in card.blocks}
    changes["删除"] = [b.title or b.block_id for b in parent.blocks if b.block_id not in kept]
    return {k: v for k, v in changes.items() if v}
