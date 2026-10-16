"""零 Key 演示模式：结合类目配置、商品信息与用户想法，生成与真实 Skill 输出格式一致的 JSON。

这些 JSON 与真实 LLM 执行 Skill 卡带后的输出结构完全一致，后续流程（编辑 / 确认 / 渲染）无差别。
"""

from __future__ import annotations

import re
from typing import Any

from app.engine.compliance import sanitize
from app.schemas.config import CategoryConfig, PlatformConfig

# ---------------------------------------------------------------- 工具


def _role(cat: CategoryConfig, *candidates: str) -> str:
    """在类目参考图角色中挑选第一个存在的角色。"""
    for c in candidates:
        if c in cat.reference_image_roles:
            return c
    return candidates[0]


def _roles(cat: CategoryConfig) -> dict[str, str]:
    return {
        "PKG": _role(cat, "product_package"),
        "CONTENT": _role(cat, "tea_soup", "product_content"),
        "DETAIL": _role(cat, "dry_leaf_closeup", "ingredient_label", "product_content"),
        "REPORT": _role(cat, "inspection_report"),
        "MODEL": _role(cat, "pet_model", "origin_scene"),
    }


def short(text: str, limit: int = 12) -> str:
    """取卖点首个短句作为标题，并控制字数。"""
    first = re.split(r"[，,。；;：:！!、（(]", text.strip())[0] or text
    return first[:limit]


def _pick(items: list[str], i: int, default: str = "") -> str:
    return items[i % len(items)] if items else default


def _ref(cat: CategoryConfig, role: str) -> dict[str, str]:
    return {"role": role, "description": cat.reference_image_roles.get(role, "请上传清晰的实拍图")}


def _info_line(product_info: dict[str, str]) -> str:
    return "，".join(v for v in product_info.values() if v)


def _idea_clause(idea: str | None) -> str:
    return f"；整体贯彻用户想法「{idea}」" if idea else ""


def _clean(obj: Any, cat: CategoryConfig, plat: PlatformConfig) -> Any:
    """递归去除违禁词，保证演示输出也合规。"""
    if isinstance(obj, str):
        return sanitize(obj, cat, plat)
    if isinstance(obj, list):
        return [_clean(v, cat, plat) for v in obj]
    if isinstance(obj, dict):
        return {k: (v if k in {"role", "generation_mode", "first_frame_generation_mode", "audio_mode",
                               "shot_purpose", "aspect_ratio"} else _clean(v, cat, plat))
                for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------- 主图 / 详情页策划


def _main_image_templates(cat: CategoryConfig, info: dict[str, str]) -> list[dict[str, Any]]:
    r = _roles(cat)
    sp, pain = cat.core_selling_points, cat.pain_points
    props = "、".join(cat.visual_style.props[:3])
    offer = info.get("price_offer") or info.get("spec") or "规格清晰 · 放心购"
    return [
        dict(title=short(_pick(sp, 0)), subtitle=cat.name, selling_point=_pick(sp, 0),
             visual="商品包装正面居中，主体占画面约 70%，干净浅色背景，柔和顶光，搜索首图构图",
             diff="首图用一个大字利益点，不堆砌卖点", mode="single_image_edit", refs=[r["PKG"]]),
        dict(title=short(_pick(sp, 1)), subtitle="细节看得见", selling_point=_pick(sp, 1),
             visual="核心卖点微距特写，浅景深，侧逆光勾出质感，背景虚化",
             diff="用微距证据代替口号", mode="single_image_edit", refs=[r["DETAIL"]]),
        dict(title=short(_pick(sp, 2)), subtitle="包装与实物同框", selling_point=_pick(sp, 2),
             visual=f"包装在左后方，实物（{cat.reference_image_roles.get(r['CONTENT'], '内容物')}）"
                    "在右前方，统一暖调侧光",
             diff="前后景深层次，实物与包装同框可信", mode="multi_image_fusion", refs=[r["PKG"], r["CONTENT"]]),
        dict(title=short(_pick(pain, 0)), subtitle="你是不是也这样？", selling_point="痛点共鸣",
             visual=f"生活化痛点场景：{_pick(pain, 0)}，人物侧脸或背影，冷灰色调",
             diff="不出现商品，先引发共鸣", mode="text_to_image", refs=[]),
        dict(title="检测报告可查", subtitle="安心看得见", selling_point=_pick(sp, 4, "第三方检测"),
             visual="包装立于桌面，旁边平铺检测报告纸质页面，报告内容如实呈现，自然窗光",
             diff="真实证据同框，拒绝空口承诺", mode="multi_image_fusion", refs=[r["PKG"], r["REPORT"]]),
        dict(title=short(cat.visual_style.mood or cat.name), subtitle=cat.product_attributes.get("产地", "源头好物"),
             visual=f"原产地 / 使用环境氛围大景，{cat.visual_style.mood}，晨光与薄雾",
             selling_point=_pick(sp, 0), diff="大景别氛围图，拉开与同质化主图的差距",
             mode="text_to_image", refs=[]),
        dict(title=short(_pick(sp, 5, _pick(sp, 3))), subtitle="日常场景随手用",
             selling_point=_pick(sp, 5, _pick(sp, 3)),
             visual=f"使用场景：商品放在生活场景中，道具 {props}，中景",
             diff="真实使用场景，带入感强", mode="single_image_edit", refs=[r["PKG"]]),
        dict(title=short(offer), subtitle="规格一目了然", selling_point="规格与优惠",
             visual="多件商品陈列，规格标签清晰，大字价格利益点，高对比色块",
             diff="规格信息可视化", mode="single_image_edit", refs=[r["PKG"]]),
        dict(title=cat.name[:12], subtitle="白底图", selling_point="平台白底图",
             visual="纯白背景商品正面图，无任何文字与道具，阴影柔和", diff="满足平台活动报名要求",
             mode="single_image_edit", refs=[r["PKG"]]),
        dict(title="一图看懂好在哪", subtitle="包装 · 实物 · 细节", selling_point="卖点汇总",
             visual="包装、实物、细节特写三者同框的组合构图，信息分区清晰",
             diff="三图融合的信息总览", mode="multi_image_fusion", refs=[r["PKG"], r["CONTENT"], r["DETAIL"]]),
    ]


def _detail_templates(cat: CategoryConfig, screens: int) -> list[dict[str, Any]]:
    r = _roles(cat)
    sp, pain = cat.core_selling_points, cat.pain_points
    head = [
        dict(title=short(_pick(pain, 0), 14), subtitle="首屏 · 痛点钩子", selling_point="痛点共鸣",
             visual=f"痛点情境大图：{_pick(pain, 0)}，冷色调、人物情绪特写", mode="text_to_image", refs=[]),
        dict(title=short(_pick(sp, 0), 14), subtitle="核心利益", selling_point=_pick(sp, 0),
             visual="商品包装英雄图，居中大主体，光线高级，背景简洁留白", mode="single_image_edit", refs=[r["PKG"]]),
    ]
    tail = [
        dict(title="产地与工艺", subtitle="源头讲起", selling_point=cat.product_attributes.get("产地", "源头"),
             visual=f"原产地全景 + 工艺流程氛围图，{cat.visual_style.mood}", mode="text_to_image", refs=[]),
        dict(title="检测报告 如实呈现", subtitle="证据屏", selling_point="第三方检测",
             visual="检测报告页面平铺于桌面，包装立于一旁，报告信息清晰可读", mode="multi_image_fusion",
             refs=[r["PKG"], r["REPORT"]]),
        dict(title="这样用更好", subtitle="使用方法", selling_point="使用 / 冲泡方法",
             visual=f"分步骤使用示意：{_pick(list(cat.product_attributes.values()), 4, '按说明使用')}",
             mode="single_image_edit", refs=[r["CONTENT"]]),
        dict(title="规格与包装", subtitle="收到长这样", selling_point="规格展示",
             visual="多角度包装展示，标注净含量与规格", mode="single_image_edit", refs=[r["PKG"]]),
        dict(title="售后无忧", subtitle="放心下单", selling_point="售后保障",
             visual="温暖的品牌承诺屏，简洁图标与留白排版", mode="text_to_image", refs=[]),
    ]
    middle_modes = [
        ("single_image_edit", [r["DETAIL"]], "卖点细节微距，浅景深"),
        ("multi_image_fusion", [r["PKG"], r["CONTENT"]], "包装与实物同框演示该卖点"),
        ("text_to_image", [], "卖点的场景化表达，不出现包装"),
    ]
    middle_count = max(0, screens - len(head) - len(tail))
    middle = []
    for i in range(middle_count):
        mode, refs, visual = middle_modes[i % len(middle_modes)]
        point = _pick(sp, i + 1)
        middle.append(dict(title=short(point, 14), subtitle=f"卖点 {i + 1}", selling_point=point,
                           visual=f"{visual}：{point}", mode=mode, refs=refs))
    screens_all = head + middle + tail
    if screens < len(screens_all):
        # 屏数少时保留首屏、核心利益、证据屏，再按顺序截取
        keep = [screens_all[0], screens_all[1], tail[1]]
        rest = [s for s in screens_all if s not in keep]
        screens_all = keep + rest[: max(0, screens - len(keep))]
        screens_all = screens_all[:screens]
    return screens_all


def auto_detail_screens(cat: CategoryConfig, plat: PlatformConfig) -> int:
    """按卖点数量自动规划屏数：首屏 + 核心利益 + 每卖点 1 屏 + 5 屏固定尾部，再按平台范围裁剪。"""
    n = 2 + max(len(cat.core_selling_points) - 1, 0) + 5
    return max(plat.detail_page.min_screens, min(plat.detail_page.max_screens, n))


def demo_plan(
    deliverable_type: str,
    cat: CategoryConfig,
    plat: PlatformConfig,
    count: int | None,
    aspect_ratio: str,
    idea: str | None,
    product_info: dict[str, str],
) -> dict[str, Any]:
    info_line = _info_line(product_info)
    if deliverable_type == "main_image":
        n = count or plat.main_image.default_count
        templates = _main_image_templates(cat, product_info)
        picked = [templates[i % len(templates)] for i in range(n)]
    else:
        n = count or auto_detail_screens(cat, plat)
        picked = _detail_templates(cat, n)
    slots = []
    for i, t in enumerate(picked):
        visual = t["visual"] + _idea_clause(idea)
        if info_line and i == 0:
            visual += f"；商品信息：{info_line}"
        slots.append({
            "title": t["title"],
            "subtitle": t.get("subtitle", ""),
            "copy_text": " ".join(x for x in (t["title"], t.get("subtitle", "")) if x),
            "selling_point": t.get("selling_point", ""),
            "visual_description": visual,
            "differentiator": t.get("diff", f"第 {i + 1} 屏独立视角与景别"),
            "generation_mode": t["mode"],
            "required_references": [_ref(cat, role) for role in t["refs"]],
            "aspect_ratio": aspect_ratio,
        })
    kind = "主图" if deliverable_type == "main_image" else "详情页"
    summary = f"{plat.name}{kind}共 {len(slots)} 张，风格「{plat.copy_style}」"
    summary += f"，按用户想法「{idea}」定制" if idea else "，由 AI 按类目与平台自动策划"
    return _clean({"plan_summary": summary, "slots": slots}, cat, plat)


def demo_slot(deliverable_type, cat, plat, aspect_ratio, idea, product_info, index: int) -> dict[str, Any]:
    """单独重新生成某个槽位：换一个模板视角。"""
    plan = demo_plan(deliverable_type, cat, plat, index + 4, aspect_ratio, idea, product_info)
    slot = plan["slots"][(index + 3) % len(plan["slots"])]
    slot["differentiator"] = "（重新生成）" + slot["differentiator"]
    return slot


# ---------------------------------------------------------------- 视频分镜剧本

_TEA = "zhangjiajie_berry_tea"


def _shot_bank(cat: CategoryConfig, video_type: str) -> list[dict[str, Any]]:
    """按视频类型给出候选分镜（第 1 个总是钩子）。"""
    r = _roles(cat)
    snd = cat.ambient_sound_library or ["轻微环境底噪"]
    scenes = cat.video_scene_suggestions or ["商品特写"]
    is_tea = cat.category_id == _TEA
    person = "年轻女生" if is_tea else "年轻铲屎官"
    pain = _pick(cat.pain_points, 0)
    hooks = {
        "pain_point_seeding": dict(
            purpose="hook", size="近景", cam="固定机位",
            scene=f"{person}皱眉看着眼前的问题：{pain}",
            action=f"{person}叹气后抬头看向镜头", first="人物半身近景，表情无奈，背景生活场景虚化",
            audio="native_dialogue",
            dialogue=f"{person}对着镜头，无奈又带点自嘲地说：“{short(pain, 10)}，谁懂啊！”",
            ambient="", mode="text_to_image", refs=[]),
        "origin_story": dict(
            purpose="hook", size="全景", cam="缓慢推近",
            scene=_pick(scenes, 1), action="晨雾在山间缓慢流动，镜头向山谷推近",
            first="山峦与晨雾全景，清晨柔光，画面通透", audio="ambient_sfx", dialogue=None,
            ambient=_pick(snd, 1), mode="text_to_image", refs=[]),
        "ingredient_review": dict(
            purpose="hook", size="特写", cam="俯拍下移",
            scene="手拿商品翻到背面配料表", action="手指沿着配料表缓慢划过",
            first="商品背面配料表特写，桌面俯拍，光线均匀", audio="dialogue_and_ambient",
            dialogue=f"画面外伸入的手的主人{person}，认真地小声说：“先看配料表，不踩坑。”",
            ambient="包装袋翻动的窸窣声", mode="single_image_edit", refs=[r["DETAIL"]]),
        "immersive_demo": dict(
            purpose="hook", size="微距", cam="微距固定",
            scene=f"{cat.name}沉浸式特写，桌面干净、侧逆光", action=_pick(scenes, 0),
            first="透明容器与商品微距，侧逆光，背景虚化", audio="ambient_sfx", dialogue=None,
            ambient=_pick(snd, 0), mode="single_image_edit", refs=[r["CONTENT"]]),
    }
    body = [
        dict(purpose="selling_point", size="特写", cam="缓慢推近",
             scene=f"展示卖点：{_pick(cat.core_selling_points, 1)}", action=_pick(scenes, 2),
             first="商品细节特写，主体清晰，背景虚化", audio="ambient_sfx", dialogue=None,
             ambient=_pick(snd, 2), mode="single_image_edit", refs=[r["DETAIL"]]),
        dict(purpose="selling_point", size="中景", cam="固定机位",
             scene=f"{person}在生活场景中使用商品", action=_pick(scenes, 3),
             first=f"{person}与商品同框中景，包装正面朝向镜头", audio="dialogue_and_ambient",
             dialogue=f"{person}眼睛一亮，惊喜轻快地说：“{short(_pick(cat.core_selling_points, 2), 10)}，真的可以！”",
             ambient=_pick(snd, 3), mode="multi_image_fusion", refs=[r["PKG"], r["MODEL"]]),
        dict(purpose="proof", size="近景", cam="右摇",
             scene="包装与检测报告同框", action="镜头从包装缓慢右摇到检测报告",
             first="包装立于左侧，检测报告平铺右侧，光线均匀", audio="ambient_sfx", dialogue=None,
             ambient="纸张轻轻翻动的声音，室内安静底噪", mode="multi_image_fusion",
             refs=[r["PKG"], r["REPORT"]]),
        dict(purpose="scene", size="全景", cam="环绕",
             scene=f"产地 / 使用环境氛围：{cat.visual_style.mood}", action="镜头缓慢环绕主体一周的四分之一",
             first="环境全景，主体位于画面三分线", audio="ambient_sfx", dialogue=None,
             ambient=_pick(snd, 1), mode="text_to_image", refs=[]),
        dict(purpose="selling_point", size="特写", cam="微距固定",
             scene=f"实物细节：{_pick(cat.core_selling_points, 3)}", action=_pick(scenes, 0),
             first="实物微距，质感清晰", audio="ambient_sfx", dialogue=None,
             ambient=_pick(snd, 4), mode="single_image_edit", refs=[r["CONTENT"]]),
        dict(purpose="selling_point", size="近景", cam="跟随",
             scene=f"{person}拿起商品向镜头展示", action="手持商品从桌面拿起，移到画面中央",
             first="桌面上的商品正面，手部自然放在一旁", audio="native_dialogue",
             dialogue=f"{person}笑着对镜头说：“{short(_pick(cat.core_selling_points, 4), 10)}，自己看。”",
             ambient="", mode="single_image_edit", refs=[r["PKG"]]),
    ]
    cta = dict(purpose="call_to_action", size="中景", cam="缓慢拉远",
               scene=f"{person}与商品同框，真诚推荐", action=f"{person}举起商品，微笑点头",
               first=f"{person}手持商品中景，包装正面清晰", audio="dialogue_and_ambient",
               dialogue=f"{person}真诚、语速放慢地说：“{'办公室常备一罐' if is_tea else '家里毛孩子的日常补给'}，"
                        "想试试的话，左下角就能带走。”",
               ambient="室内轻微环境底噪", mode="single_image_edit", refs=[r["PKG"]])
    return [hooks.get(video_type, hooks["immersive_demo"]), *body, cta]


def demo_script(
    video_type: str,
    cat: CategoryConfig,
    plat: PlatformConfig,
    aspect_ratio: str,
    shot_count: int,
    target_duration: int,
    idea: str | None,
    product_info: dict[str, str],
) -> dict[str, Any]:
    bank = _shot_bank(cat, video_type)
    hook, cta, body = bank[0], bank[-1], bank[1:-1]
    middle = [body[i % len(body)] for i in range(max(shot_count - 2, 0))]
    chosen = ([hook, *middle, cta] if shot_count >= 2 else [hook])[:shot_count]
    rest = max(target_duration - 3, len(chosen))
    per = round(rest / max(len(chosen) - 1, 1), 1) if len(chosen) > 1 else 3
    info_line = _info_line(product_info)
    shots = []
    for i, t in enumerate(chosen):
        scene = t["scene"] + (_idea_clause(idea) if i == 0 else "")
        if info_line and t["purpose"] == "proof":
            scene += f"（{info_line}）"
        shots.append({
            "duration_seconds": 3 if i == 0 else min(per, 10),
            "shot_purpose": t["purpose"],
            "shot_size": t["size"],
            "camera_movement": t["cam"],
            "scene_description": scene,
            "subject_action": t["action"],
            "first_frame_description": t["first"],
            "audio_mode": t["audio"],
            "character_dialogue": t["dialogue"],
            "ambient_sound_description": t["ambient"],
            "first_frame_generation_mode": t["mode"],
            "required_references": [_ref(cat, role) for role in t["refs"]],
        })
    summary = f"{len(shots)} 镜原生音画带货短视频，黄金 3 秒钩子开场，全片无画外音"
    summary += f"，按用户想法「{idea}」定制" if idea else ""
    return _clean({"script_summary": summary, "shots": shots}, cat, plat)


def demo_shot(video_type, cat, plat, aspect_ratio, idea, product_info, index: int) -> dict[str, Any]:
    script = demo_script(video_type, cat, plat, aspect_ratio, 8, 30, idea, product_info)
    shot = script["shots"][(index + 2) % len(script["shots"])]
    if index == 0:
        shot = script["shots"][0]
    return shot


# ---------------------------------------------------------------- 提示词编译

CAMERA_TAGS: list[tuple[tuple[str, ...], str]] = [
    (("微距固定", "微距"), "[Macro static]"),
    (("推近", "推进"), "[Push in]"),
    (("拉远", "拉出"), "[Pull out]"),
    (("左摇",), "[Pan left]"),
    (("右摇",), "[Pan right]"),
    (("上移", "仰拍上"), "[Tilt up]"),
    (("下移", "俯拍下"), "[Tilt down]"),
    (("环绕",), "[Orbit]"),
    (("跟随", "跟拍"), "[Tracking shot]"),
    (("变焦",), "[Zoom in]"),
    (("手持",), "[Handheld]"),
    (("固定",), "[Static shot]"),
]


def camera_tags(camera_movement: str, shot_size: str = "") -> list[str]:
    """把中文运镜描述映射为 H3 运镜标签（最多 2 个）。"""
    text = f"{camera_movement} {shot_size}"
    tags: list[str] = []
    for keys, tag in CAMERA_TAGS:
        if any(k in text for k in keys) and tag not in tags:
            tags.append(tag)
    if "[Macro static]" in tags and "[Static shot]" in tags:
        tags.remove("[Static shot]")
    return tags[:2] or ["[Static shot]"]


def demo_image_prompt(skill_id: str, slot: dict[str, Any], roles: list[str], aspect_ratio: str,
                      cat: CategoryConfig) -> dict[str, Any]:
    palette = "、".join(cat.visual_style.palette)
    copy = slot.get("copy_text") or slot.get("title", "")
    visual = slot.get("visual_description") or slot.get("first_frame_description", "")
    if skill_id.startswith("gemini"):
        lines = [f"请生成一张用于电商的商业摄影图，最终画面必须是 {aspect_ratio} 比例。"]
        if len(roles) == 1:
            lines.append("以附件中的商品照片为准，商品本身的形状、颜色、logo 与印刷文字必须像素级保持不变，"
                         "只改变周围环境。")
        elif roles:
            refs = "、".join(f"第{i + 1}张附件（{r}）" for i, r in enumerate(roles))
            lines.append(f"按附件顺序使用 {refs}，所有元素统一光源方向，像同一次拍摄；包装与中文字体不得变形。")
        lines.append(f"画面：{visual}。色调参考 {palette}，氛围 {cat.visual_style.mood}。")
        if copy:
            lines.append(f"在画面上方安全区排版中文文字“{copy}”，粗体无衬线，与背景高对比。")
        lines.append("不要水印、不要多余 logo、商品不变形、手部结构正确。")
        return {"prompt": "".join(lines), "negative_notes": "水印、错字、商品形变、多余肢体",
                "size_hint": aspect_ratio}
    parts = [f"E-commerce commercial photo for {cat.name}, {aspect_ratio} composition."]
    if len(roles) == 1:
        parts.append("Use the provided reference image as the exact product. Keep the packaging shape, colors, "
                     "logo, and all printed Chinese text exactly unchanged.")
    elif roles:
        desc = " ".join(f"Image {i + 1} is the {r.replace('_', ' ')} (keep exactly unchanged)." for i, r in
                        enumerate(roles))
        parts.append(desc + " Compose them naturally in one scene with unified lighting; do not distort the "
                     "packaging or the Chinese typography.")
    parts.append(f"Scene: {visual}. Color palette: {palette}. Mood: {cat.visual_style.mood}.")
    parts.append("85mm lens, shallow depth of field, crisp product detail, soft side light, 5500K.")
    if copy:
        parts.append(f'With the Chinese headline "{copy}" in bold sans-serif at the top safe area, high contrast.')
    parts.append("No watermark, no extra logos, no distorted packaging text.")
    return {"prompt": " ".join(parts), "negative_notes": "watermark, garbled text, deformed packaging, extra fingers",
            "size_hint": aspect_ratio}


def demo_h3_prompt(shot: dict[str, Any], aspect_ratio: str) -> dict[str, Any]:
    tags = camera_tags(shot.get("camera_movement", ""), shot.get("shot_size", ""))
    mode = shot.get("audio_mode")
    dialogue = shot.get("character_dialogue")
    ambient = shot.get("ambient_sound_description", "")
    if mode == "native_dialogue":
        sound = f"声音：{dialogue}口型与台词同步，画面内人物同期声，无背景音乐。"
    elif mode == "ambient_sfx":
        sound = f"声音：{ambient}，与画面动作同步发生。无人声，无背景音乐。"
    elif mode == "dialogue_and_ambient":
        sound = f"声音：{dialogue}口型与台词同步；同时伴随{ambient}，环境音音量低于人声。无背景音乐。"
    else:
        sound = "声音：静音。"
    dur = f"{float(shot.get('duration_seconds', 3)):g}"
    prompt = (
        f"{''.join(tags)} 以首帧画面为起点，商品包装的形状、颜色、文字和 logo 全程保持不变。"
        f"{shot.get('scene_description', '')}。{shot.get('subject_action', '')}，动作自然连贯、速度舒缓。"
        f"{sound}画面比例 {aspect_ratio}，时长约 {dur} 秒，写实商业广告质感，无字幕，无水印，商品不变形。"
    )
    audio_summary = {"native_dialogue": "人物同期声对白", "ambient_sfx": "原生环境音效",
                     "dialogue_and_ambient": "人物对白 + 环境音", "silent": "静音"}.get(mode, "")
    return {"prompt": prompt, "camera_tags": tags, "audio_summary": audio_summary}
