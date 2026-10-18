"""主图 / 详情页策划引擎：执行 ecommerce_planner Skill，输出 draft 状态的策划案。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.engine.compiler import SYSTEM_PROMPT, compile_planner
from app.engine.runtime import EngineRuntime
from app.providers.demo import content as demo
from app.schemas.common import mode_for_reference_count
from app.schemas.plan import ImagePlan, ImageSlot, slot_from_llm


def _normalize_slot_data(data: dict[str, Any]) -> dict[str, Any]:
    """LLM 偶尔会给出与参考图数量不符的模式：以参考图数量为准自动纠正。"""
    refs = (data.get("required_references") or [])[:3]
    data["required_references"] = refs
    data["generation_mode"] = mode_for_reference_count(len(refs))
    return data


def _apply_style(slot: ImageSlot, aesthetic_style: str) -> ImageSlot:
    """用户选定了全局美学流派时，LLM 漏写或写错也以界面选择为准。"""
    if aesthetic_style != "auto":
        slot.aesthetic_style = aesthetic_style
    return slot


class PlannerEngine:
    def __init__(self, rt: EngineRuntime):
        self.rt = rt

    def _resolve_count(self, deliverable_type: str, platform_id: str, count: int | None) -> int | None:
        plat = self.rt.platform(platform_id)
        if count is None:
            return None if deliverable_type == "detail_page" else plat.main_image.default_count
        lo, hi = (1, 10) if deliverable_type == "main_image" else (3, 20)
        return max(lo, min(hi, int(count)))

    async def _run_skill(self, prompt: str) -> dict[str, Any]:
        return await self.rt.llm().complete_json(SYSTEM_PROMPT, prompt)

    async def generate(
        self,
        *,
        deliverable_type: str,
        category_id: str,
        platform_id: str,
        aspect_ratio: str,
        count: int | None,
        user_creative_idea: str | None,
        product_info: dict[str, str] | None = None,
        skill_id: str | None = None,
        existing: ImagePlan | None = None,
        aesthetic_style: str = "auto",
    ) -> tuple[ImagePlan, str]:
        """生成（或覆盖重新生成）整套策划案。返回 (策划案, 使用的模式说明)。

        aesthetic_style 为界面选择的全局美学流派：auto 时由 Skill 逐槽位匹配，否则整套统一使用该流派。
        """
        cat, plat = self.rt.category(category_id), self.rt.platform(platform_id)
        info = {k: v for k, v in (product_info or {}).items() if v}
        n = self._resolve_count(deliverable_type, platform_id, count)
        if not skill_id:
            # 未指定时使用该类目的专属策划卡带（没有则回落到通用默认卡带）
            cards = self.rt.skills.list("ecommerce_planner", category_id)
            if not cards:
                raise KeyError(f"卡槽 ecommerce_planner 中没有适用于类目 {category_id} 的 Skill")
            skill_id = cards[0].skill_id
        skill = self.rt.skills.get("ecommerce_planner", skill_id)
        prompt = compile_planner(skill, deliverable_type, cat, plat, aspect_ratio, n, user_creative_idea,
                                 product_info=info, aesthetic_style=aesthetic_style)
        if self.rt.is_live("llm"):
            data = await self._run_skill(prompt)
            mode = "真实 LLM"
        else:
            await asyncio.sleep(self.rt.demo_delay)
            data = demo.demo_plan(deliverable_type, cat, plat, n, aspect_ratio, user_creative_idea, info,
                                  aesthetic_style=aesthetic_style)
            mode = "演示模式"
        slots = [_apply_style(slot_from_llm(_normalize_slot_data(s), aspect_ratio), aesthetic_style)
                 for s in data.get("slots") or []]
        if existing is not None and existing.plan_status == "draft":
            plan = existing
            plan.default_aspect_ratio = aspect_ratio
            plan.planner_skill_id = skill_id
        else:
            plan = ImagePlan(
                deliverable_type=deliverable_type, platform_id=platform_id, category_id=category_id,
                planner_skill_id=skill_id, default_aspect_ratio=aspect_ratio,
            )
        plan.aesthetic_style = aesthetic_style
        plan.replace_all_items(slots, user_creative_idea)
        plan.plan_summary = data.get("plan_summary", "")
        return plan, mode

    async def regenerate_slot(
        self, plan: ImagePlan, slot_id: str, product_info: dict[str, str] | None = None, skill_id: str | None = None
    ) -> ImageSlot:
        """单独重新生成某个槽位（保留位置与 ID）。"""
        plan._require_draft()
        cat, plat = self.rt.category(plan.category_id), self.rt.platform(plan.platform_id)
        index = plan._index_of(slot_id)
        old = plan.slots[index]
        info = {k: v for k, v in (product_info or {}).items() if v}
        if self.rt.is_live("llm"):
            skill = self.rt.skills.get("ecommerce_planner", skill_id or plan.planner_skill_id)
            base = compile_planner(skill, plan.deliverable_type, cat, plat, old.aspect_ratio, 1,
                                   plan.user_creative_idea, product_info=info, aesthetic_style=plan.aesthetic_style)
            others = [s.title for s in plan.slots if s.slot_id != slot_id]
            original = json.dumps(old.model_dump(mode="json", include={"title", "visual_description"}),
                                  ensure_ascii=False)
            prompt = (
                f"{base}\n\n# 补充要求\n只需重新策划第 {index + 1} 个槽位（输出 slots 数组只含 1 个元素）。"
                f"原槽位内容：{original}。"
                f"必须与其他槽位不雷同：{'、'.join(others)}。"
            )
            data = (await self._run_skill(prompt)).get("slots") or [{}]
            slot_data = data[0]
        else:
            await asyncio.sleep(self.rt.demo_delay)
            slot_data = demo.demo_slot(plan.deliverable_type, cat, plat, old.aspect_ratio, plan.user_creative_idea,
                                       info, index, aesthetic_style=plan.aesthetic_style)
        new = _apply_style(slot_from_llm(_normalize_slot_data(slot_data), old.aspect_ratio), plan.aesthetic_style)
        new.candidate_count = old.candidate_count
        return plan.regenerate_slot(slot_id, new)
