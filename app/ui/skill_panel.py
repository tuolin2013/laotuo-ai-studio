"""Skill 卡带选择 / 预览 / 临时覆盖面板（各选项卡复用）。"""

from __future__ import annotations

import gradio as gr

from app.engine.skill_loader import SkillRegistry
from app.schemas.skill import SkillSlot


def build_skill_panel(registry: SkillRegistry, slot: SkillSlot, label: str) -> gr.Dropdown:
    choices = registry.choices(slot)
    with gr.Accordion(f"{label}（插卡式 Skill）", open=False):
        with gr.Row():
            dropdown = gr.Dropdown(
                choices=choices, value=choices[0][1] if choices else None, label="选择 Skill 卡带", scale=4
            )
            refresh_btn = gr.Button("🔄 重新扫描卡带", scale=1)
        body = gr.Code(label="Skill 指令预览（可临时修改，不会写回文件）", language="markdown", lines=14)
        with gr.Row():
            apply_btn = gr.Button("应用临时覆盖")
            reset_btn = gr.Button("恢复原始指令")
        status = gr.Markdown()

    def _show(skill_id):
        return registry.get(slot, skill_id).body if skill_id else ""

    def _refresh(current):
        registry.refresh()
        new_choices = registry.choices(slot)
        ids = [c[1] for c in new_choices]
        value = current if current in ids else (ids[0] if ids else None)
        errs = "\n".join(f"- ⚠️ {e}" for e in registry.errors.values())
        return gr.update(choices=new_choices, value=value), f"已扫描到 {len(ids)} 个卡带\n{errs}"

    def _apply(skill_id, text):
        registry.set_override(slot, skill_id, text)
        return "✅ 已应用临时覆盖（仅本次运行有效）"

    def _reset(skill_id):
        registry.clear_override(slot, skill_id)
        return _show(skill_id), "已恢复原始指令"

    dropdown.change(_show, dropdown, body)
    refresh_btn.click(_refresh, dropdown, [dropdown, status])
    apply_btn.click(_apply, [dropdown, body], status)
    reset_btn.click(_reset, dropdown, [body, status])
    return dropdown
