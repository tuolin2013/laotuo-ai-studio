"""剧本创作引擎：基于创意大纲,执行 story_scriptwriter Skill,输出完整剧本(Markdown)。"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from app.engine.compiler import SYSTEM_PROMPT
from app.engine.runtime import EngineRuntime
from app.providers.demo import content as demo
from app.schemas.video import ConceptOutline, StoryScript


def compile_story_prompt(
    skill: Any,
    concept: ConceptOutline,
    category: Any,
    platform: Any,
    product_info: dict[str, str] | None = None,
) -> str:
    """编译剧本创作提示词。"""
    import json
    # 将 ConceptOutline 转为字典，排除 datetime 字段
    concept_dict = concept.model_dump(exclude={'created_at'})
    return skill.render(
        concept_json=json.dumps(concept_dict, ensure_ascii=False, indent=2),
        category_json=json.dumps(category.model_dump(), ensure_ascii=False, indent=2),
        platform_json=json.dumps(platform.model_dump(), ensure_ascii=False, indent=2),
        product_info_json=json.dumps(product_info or {}, ensure_ascii=False, indent=2),
    )


def _extract_story_fields(text: str) -> dict[str, Any]:
    """从模型输出文本中用正则提取剧本结构化字段（JSON 解析失败的降级方案）。"""
    import json

    data = {
        "title": "",
        "logline": "",
        "content": "",
        "characters": [],
        "locations": [],
        "props": [],
    }

    # 尝试先找到 JSON 对象（即使不完整或格式不正确）
    json_match = re.search(r'"title"\s*:\s*"([^"]*)"', text)
    if json_match:
        data["title"] = json_match.group(1)

    logline_match = re.search(r'"logline"\s*:\s*"([^"]*)"', text)
    if logline_match:
        data["logline"] = logline_match.group(1)

    # 提取 content：从 "content": " 到下一个逗号前的字段或 } 为止
    content_match = re.search(r'"content"\s*:\s*"(.*?)"\s*(?:,\s*"|}")', text, re.S)
    if not content_match:
        # 备选方案：找到 content 字段后的最长连续非转义字符串
        content_match = re.search(r'"content"\s*:\s*"([^"]*(?:\\.[^"]*)*)"', text, re.S)

    if content_match:
        content_str = content_match.group(1)
        # 解转义
        try:
            data["content"] = json.loads('"' + content_str + '"')
        except (json.JSONDecodeError, ValueError):
            data["content"] = content_str.replace('\\n', '\n').replace('\\"', '"')

    # 提取 characters 数组
    chars_match = re.search(r'"characters"\s*:\s*\[(.*?)\]', text, re.S)
    if chars_match:
        chars_str = chars_match.group(1)
        data["characters"] = [c.strip().strip('"') for c in chars_str.split(',') if c.strip()]

    # 提取 locations 数组
    locs_match = re.search(r'"locations"\s*:\s*\[(.*?)\]', text, re.S)
    if locs_match:
        locs_str = locs_match.group(1)
        data["locations"] = [l.strip().strip('"') for l in locs_str.split(',') if l.strip()]

    # 提取 props 数组
    props_match = re.search(r'"props"\s*:\s*\[(.*?)\]', text, re.S)
    if props_match:
        props_str = props_match.group(1)
        data["props"] = [p.strip().strip('"') for p in props_str.split(',') if p.strip()]

    return data


class StoryEngine:
    def __init__(self, rt: EngineRuntime):
        self.rt = rt

    async def generate(
        self,
        *,
        concept: ConceptOutline,
        category_id: str,
        platform_id: str,
        product_info: dict[str, str] | None = None,
        skill_id: str | None = None,
    ) -> tuple[StoryScript, str]:
        """基于创意大纲生成完整剧本。"""
        cat, plat = self.rt.category(category_id), self.rt.platform(platform_id)
        info = {k: v for k, v in (product_info or {}).items() if v}

        # 根据视频类型自动选择专属 Skill
        if skill_id is None:
            skill_id = f"{concept.video_type}_story"

        skill = self.rt.skills.get("story_scriptwriter", skill_id)
        prompt = compile_story_prompt(skill, concept, cat, plat, info)

        if self.rt.is_live("llm"):
            raw_text = await self.rt.llm().complete(SYSTEM_PROMPT, prompt)
            mode = "真实 LLM"
            # 尝试先解析 JSON，如果失败则用正则提取
            try:
                from app.providers.llm import extract_json
                data = extract_json(raw_text)
            except Exception:
                # JSON 解析失败，降级为正则提取
                data = _extract_story_fields(raw_text)
        else:
            await asyncio.sleep(self.rt.demo_delay)
            data = demo.demo_story(concept, cat, plat, info)
            mode = "演示模式"

        try:
            story = StoryScript(
                concept_id=concept.concept_id,
                video_type=concept.video_type,
                aspect_ratio=concept.aspect_ratio,
                title=data.get("title", ""),
                logline=data.get("logline", ""),
                content=data.get("content", ""),
                characters=data.get("characters", []),
                locations=data.get("locations", []),
                props=data.get("props", []),
            )
        except Exception as e:
            import json
            # 显示完整的验证错误和原始数据
            error_details = f"{e.__class__.__name__}: {e}"
            data_preview = json.dumps(data, ensure_ascii=False, indent=2)[:1000]
            raise ValueError(
                f"模型输出的数据无法转为 StoryScript 对象：{error_details}\n\n"
                f"原始数据预览：\n{data_preview}"
            ) from e
        return story, mode
