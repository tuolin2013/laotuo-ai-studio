"""零 Key 演示模式：结合类目配置、商品信息与用户想法，生成与真实 Skill 输出格式一致的 JSON。

这些 JSON 与真实 LLM 执行 Skill 卡带后的输出结构完全一致，后续流程（编辑 / 确认 / 渲染）无差别。
"""

from __future__ import annotations

import re
from typing import Any

from app.engine.compliance import sanitize
from app.providers.demo.aesthetics import (
    COMPOSITION_BY_KIND,
    FONT_NAMES_ZH,
    STYLES,
    auto_aesthetic,
    layout,
    pill_from_copy,
)
from app.schemas.config import CategoryConfig, PlatformConfig
from app.schemas.plan import AESTHETIC_STYLE_NAMES

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
                               "shot_purpose", "aspect_ratio", "aesthetic_style", "composition_mode"}
                    else _clean(v, cat, plat))
                for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------- 主图 / 详情页策划


def _main_image_templates(cat: CategoryConfig, info: dict[str, str]) -> list[dict[str, Any]]:
    """kind 为槽位作用，决定演示用的构图与自动美学（见 app/providers/demo/aesthetics.py）。"""
    r = _roles(cat)
    sp, pain = cat.core_selling_points, cat.pain_points
    props = "、".join(cat.visual_style.props[:3])
    offer = info.get("price_offer") or info.get("spec") or "规格清晰 · 放心购"
    origin = short(cat.product_attributes.get("产地", ""), 6)
    action = _pick(cat.video_scene_suggestions, 0, "商品被拿起使用的瞬间")
    return [
        dict(kind="hero", title=short(_pick(sp, 0)), subtitle=cat.name, pill=origin, selling_point=_pick(sp, 0),
             visual="商品包装正面完整清晰，干净背景，柔和光线，搜索首图",
             diff="首图用一个大字利益点，不堆砌卖点", mode="single_image_edit", refs=[r["PKG"]]),
        dict(kind="macro", title=short(_pick(sp, 1)), subtitle="细节看得见", pill="细节实拍",
             selling_point=_pick(sp, 1), visual="核心卖点微距特写，浅景深，侧逆光勾出质感",
             diff="用微距证据代替口号", mode="single_image_edit", refs=[r["DETAIL"]]),
        dict(kind="experience", title=short(_pick(sp, 2)), subtitle="包装与实物同框", selling_point=_pick(sp, 2),
             visual=f"实物（{cat.reference_image_roles.get(r['CONTENT'], '内容物')}）在前景，包装在中景，统一侧光",
             diff="前后景深层次，实物与包装同框可信", mode="multi_image_fusion", refs=[r["PKG"], r["CONTENT"]]),
        dict(kind="pain", title=short(_pick(pain, 0)), subtitle="你是不是也这样？", selling_point="痛点共鸣",
             visual=f"生活化痛点场景：{_pick(pain, 0)}，人物侧脸或背影，光线略暗",
             diff="不出现商品，先引发共鸣", mode="text_to_image", refs=[]),
        dict(kind="proof", title="检测报告可查", subtitle="安心看得见", pill="如实呈现",
             selling_point=_pick(sp, 4, "第三方检测"),
             visual="包装与检测报告纸质页面平铺在桌面，报告内容如实呈现，不生成新数字与印章",
             diff="真实证据同框，拒绝空口承诺", mode="multi_image_fusion", refs=[r["PKG"], r["REPORT"]]),
        dict(kind="origin", title=short(cat.visual_style.mood or cat.name),
             subtitle=cat.product_attributes.get("产地", "源头好物"),
             visual=f"原产地 / 使用环境氛围大景，{cat.visual_style.mood}，晨光与薄雾",
             selling_point=_pick(sp, 0), diff="大景别氛围图，拉开与同质化主图的差距",
             mode="text_to_image", refs=[]),
        dict(kind="lifestyle", title=short(_pick(sp, 5, _pick(sp, 3))), subtitle="日常场景随手用",
             selling_point=_pick(sp, 5, _pick(sp, 3)),
             visual=f"使用场景的动态瞬间：{action}，商品包装清晰可见，道具 {props}",
             diff="真实使用场景，带入感强", mode="single_image_edit", refs=[r["PKG"]]),
        dict(kind="spec", title=short(offer), subtitle="规格一目了然", selling_point="规格与优惠",
             visual="多件商品按网格整齐陈列，规格标签清晰",
             diff="规格信息可视化", mode="single_image_edit", refs=[r["PKG"]]),
        dict(kind="white", title=cat.name[:12], subtitle="白底图", selling_point="平台白底图",
             visual="纯白背景商品正面图，无任何文字与道具，阴影柔和", diff="满足平台活动报名要求",
             mode="single_image_edit", refs=[r["PKG"]]),
        dict(kind="spec", title="一图看懂好在哪", subtitle="包装 · 实物 · 细节", selling_point="卖点汇总",
             visual="包装、实物与细节特写分区平铺，信息分区清晰",
             diff="三图融合的信息总览", mode="multi_image_fusion", refs=[r["PKG"], r["CONTENT"], r["DETAIL"]]),
    ]


def _detail_templates(cat: CategoryConfig, screens: int) -> list[dict[str, Any]]:
    r = _roles(cat)
    sp, pain = cat.core_selling_points, cat.pain_points
    head = [
        dict(kind="pain", title=short(_pick(pain, 0), 14), subtitle="首屏 · 痛点钩子", selling_point="痛点共鸣",
             visual=f"痛点情境大图：{_pick(pain, 0)}，人物情绪特写", mode="text_to_image", refs=[]),
        dict(kind="hero", title=short(_pick(sp, 0), 14), subtitle="核心利益", selling_point=_pick(sp, 0),
             visual="商品包装英雄图，光线高级，背景简洁", mode="single_image_edit", refs=[r["PKG"]]),
    ]
    tail = [
        dict(kind="origin", title="产地与工艺", subtitle="源头讲起",
             selling_point=cat.product_attributes.get("产地", "源头"),
             visual=f"原产地全景 + 工艺流程氛围图，{cat.visual_style.mood}", mode="text_to_image", refs=[]),
        dict(kind="proof", title="检测报告 如实呈现", subtitle="证据屏", pill="报告原件", selling_point="第三方检测",
             visual="检测报告页面与包装平铺于桌面，报告信息清晰可读", mode="multi_image_fusion",
             refs=[r["PKG"], r["REPORT"]]),
        dict(kind="experience", title="这样用更好", subtitle="使用方法", selling_point="使用 / 冲泡方法",
             visual=f"分步骤使用示意：{_pick(list(cat.product_attributes.values()), 4, '按说明使用')}",
             mode="single_image_edit", refs=[r["CONTENT"]]),
        dict(kind="spec", title="规格与包装", subtitle="收到长这样", selling_point="规格展示",
             visual="多角度包装平铺展示，标注净含量与规格", mode="single_image_edit", refs=[r["PKG"]]),
        dict(kind="promise", title="售后无忧", subtitle="放心下单", selling_point="售后保障",
             visual="温暖的品牌承诺屏，简洁图标", mode="text_to_image", refs=[]),
    ]
    middle_modes = [
        ("macro", "single_image_edit", [r["DETAIL"]], "卖点细节微距，浅景深"),
        ("compare", "single_image_edit", [r["DETAIL"]], "选购小知识：本品细节与普通形态客观并排对比"),
        ("experience", "multi_image_fusion", [r["PKG"], r["CONTENT"]], "包装与实物同框演示该卖点"),
        ("lifestyle", "text_to_image", [], "卖点的场景化动态表达，不出现包装"),
    ]
    middle_count = max(0, screens - len(head) - len(tail))
    middle = []
    for i in range(middle_count):
        kind, mode, refs, visual = middle_modes[i % len(middle_modes)]
        point = _pick(sp, i + 1)
        middle.append(dict(kind=kind, title=short(point, 14), subtitle=f"卖点 {i + 1}", selling_point=point,
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
    aesthetic_style: str = "auto",
) -> dict[str, Any]:
    """aesthetic_style 非 auto 时整套使用该美学流派；构图模式按每个槽位的作用匹配。"""
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
        role = t.get("kind", "hero")
        style = (aesthetic_style if aesthetic_style != "auto"
                 else auto_aesthetic(cat.category_id, plat.platform_id, role))
        composition = COMPOSITION_BY_KIND.get(role, "auto")
        lay = layout(composition, style)
        visual = t["visual"] + (f"；{lay[0]}" if lay else "") + _idea_clause(idea)
        if info_line and i == 0:
            visual += f"；商品信息：{info_line}"
        slots.append({
            "title": t["title"],
            "subtitle": t.get("subtitle", ""),
            "copy_text": " ".join(x for x in (t["title"], t.get("subtitle", ""), t.get("pill", "")) if x),
            "selling_point": t.get("selling_point", ""),
            "visual_description": visual,
            "differentiator": t.get("diff", f"第 {i + 1} 屏独立视角与景别"),
            "generation_mode": t["mode"],
            "aesthetic_style": style,
            "composition_mode": composition,
            "required_references": [_ref(cat, role) for role in t["refs"]],
            "aspect_ratio": aspect_ratio,
        })
    kind = "主图" if deliverable_type == "main_image" else "详情页"
    summary = f"{plat.name}{kind}共 {len(slots)} 张，风格「{plat.copy_style}」"
    summary += f"，按用户想法「{idea}」定制" if idea else "，由 AI 按类目与平台自动策划"
    return _clean({"plan_summary": summary, "slots": slots}, cat, plat)


def demo_slot(deliverable_type, cat, plat, aspect_ratio, idea, product_info, index: int,
              aesthetic_style: str = "auto") -> dict[str, Any]:
    """单独重新生成某个槽位：换一个模板视角。"""
    plan = demo_plan(deliverable_type, cat, plat, index + 4, aspect_ratio, idea, product_info, aesthetic_style)
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
    # 三维设计：美学流派给出色板 / 光线 / 字体，构图给出主体位置与留白区（首帧等无此字段时按 auto 处理）
    aesthetic = slot.get("aesthetic_style", "auto")
    spec = STYLES.get(aesthetic)
    lay = layout(slot.get("composition_mode", "auto"), aesthetic)
    title, sub = slot.get("title", ""), slot.get("subtitle", "")
    pill = pill_from_copy(slot.get("copy_text", ""), title, sub)
    if skill_id.startswith("gemini"):
        lines = [f"请生成一张用于电商的商业摄影图，最终画面必须是 {aspect_ratio} 比例。"]
        if lay and lay[0] not in visual:
            lines.append(f"{lay[0]}。")
        if len(roles) == 1:
            lines.append("以附件中的商品照片为准，商品本身的形状、颜色、logo 与印刷文字必须像素级保持不变，"
                         "只改变周围环境。")
        elif roles:
            refs = "、".join(f"第{i + 1}张附件（{r}）" for i, r in enumerate(roles))
            lines.append(f"按附件顺序使用 {refs}，所有元素统一光源方向，像同一次拍摄；包装与中文字体不得变形。")
        if spec:
            lines.append(f"画面：{visual}。美学流派「{AESTHETIC_STYLE_NAMES[aesthetic]}」：背景 {spec.background[0]} "
                         f"渐变到 {spec.background[1]}，点睛色 {spec.accent}；光线：{spec.light}。")
        else:
            lines.append(f"画面：{visual}。色调参考 {palette}，氛围 {cat.visual_style.mood}。")
        if spec and title:
            text = f"在留白区排版主标题“{title}”，{FONT_NAMES_ZH[spec.font]}，颜色 {spec.text}"
            if sub:
                text += f"；副标题“{sub}”约为主标题 40% 字号，颜色 {spec.sub}"
            if pill:
                text += f"；短卖点“{pill}”装进 {spec.accent} 圆角胶囊框"
            lines.append(text + "；文字全部落在留白区，不压商品。")
        elif copy:
            lines.append(f"在画面上方安全区排版中文文字“{copy}”，粗体无衬线，与背景高对比。")
        lines.append("不要水印、不要多余 logo、商品不变形、手部结构正确。")
        return {"prompt": "".join(lines), "negative_notes": "水印、错字、商品形变、多余肢体",
                "size_hint": aspect_ratio}
    parts = [f"E-commerce commercial photo for {cat.name}, {aspect_ratio} composition."]
    if lay:
        parts.append(lay[1][0].upper() + lay[1][1:] + ".")
    if len(roles) == 1:
        parts.append("Use the provided reference image as the exact product. Keep the packaging shape, colors, "
                     "logo, and all printed Chinese text exactly unchanged.")
    elif roles:
        desc = " ".join(f"Image {i + 1} is the {r.replace('_', ' ')} (keep exactly unchanged)." for i, r in
                        enumerate(roles))
        parts.append(desc + " Compose them naturally in one scene with unified lighting; do not distort the "
                     "packaging or the Chinese typography.")
    if spec:
        parts.append(f"Scene: {visual}. Color palette: background {spec.background[0]} fading to "
                     f"{spec.background[1]}, accent {spec.accent}. Lighting: {spec.light}.")
        parts.append("85mm lens, shallow depth of field, crisp product detail.")
    else:
        parts.append(f"Scene: {visual}. Color palette: {palette}. Mood: {cat.visual_style.mood}.")
        parts.append("85mm lens, shallow depth of field, crisp product detail, soft side light, 5500K.")
    if spec and title:
        text = f'Render the exact Chinese headline "{title}" in {spec.font_en}, color {spec.text}'
        if sub:
            text += f', with the subheadline "{sub}" at about 40% of the headline size in {spec.sub}'
        if pill:
            text += f', and the short badge "{pill}" inside a sleek rounded pill-shaped badge filled {spec.accent}'
        parts.append(text + ", all inside the reserved negative space and never overlapping the product.")
    elif copy:
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


# ---------------------------------------------------------------- 创意大纲 (三步式工作流 Step 1)


def demo_concept(
    video_type: str,
    cat: CategoryConfig,
    plat: PlatformConfig,
    aspect_ratio: str,
    target_duration: int,
    user_idea: str | None,
    product_info: dict[str, str],
) -> dict[str, Any]:
    """演示模式：创意大纲生成。"""
    sp = cat.core_selling_points
    pain = cat.pain_points

    # 根据视频类型设计不同的创意大纲
    concepts = {
        "lifestyle_seeding": {
            "hook_design": (
                f"痛点共鸣钩子：开场 3 秒展现「{_pick(pain, 0)}」的具体场景,"
                "让用户瞬间产生「说的就是我」的共鸣"
            ),
            "core_message": f"一杯解决你的{_pick(pain, 0).split('、')[0]},真实有效",
            "emotional_arc": "困扰焦虑 → 尝试产品 → 效果惊喜 → 想要分享",
            "call_to_action": "办公室/家里常备,不再被XX困扰",
            "scenes_count": 3,
            "scenes_outline": [
                f"场景1:痛点具象化 - {_pick(pain, 0)}的真实时刻",
                "场景2:尝试产品 - 同事/朋友推荐,抱着试试的心态",
                "场景3:效果与行动 - 明显改善,决定囤货"
            ]
        },
        "product_showcase": {
            "hook_design": "强ASMR钩子：撕袋声 + 微距白霜/颗粒特写,视听双重冲击",
            "core_message": f"亲眼看到的品质:{_pick(sp, 0)}",
            "emotional_arc": "好奇 → 惊艳 → 想拥有",
            "call_to_action": "这品质,你看到了",
            "scenes_count": 4,
            "scenes_outline": [
                "场景1:撕袋钩子 - 清脆撕袋声 + 包装正面特写",
                "场景2:产品细节 - 超微距展现独特之处(白霜/颗粒/质地)",
                "场景3:冲泡/使用 - 过程展示,色泽/形态变化",
                "场景4:成品呈现 - 最终效果,满足感"
            ]
        },
        "trust_building": {
            "hook_design": "证据先行钩子：检测报告/产地标识特写,关键数据醒目",
            "core_message": f"有证据的安心:{_pick(sp, 1)},第三方检测可查",
            "emotional_arc": "质疑 → 看到证据 → 验证真实 → 信任",
            "call_to_action": "报告编号可查,放心选",
            "scenes_count": 3,
            "scenes_outline": [
                "场景1:证据呈现 - 检测报告/认证证书特写,关键数据可见",
                "场景2:溯源细节 - 产地实拍/工艺流程/品控环节",
                "场景3:温度背书 - 负责人/工艺师傅真诚陈述,给出可验证信息"
            ]
        },
        "comparison_review": {
            "hook_design": "对比冲突钩子：两个产品并排,数据差距一目了然",
            "core_message": f"对比后你就知道:{_pick(sp, 0)},差距3倍",
            "emotional_arc": "疑惑选择困难 → 逐项对比 → 差异清晰 → 明智选择",
            "call_to_action": "对比完了,你知道怎么选",
            "scenes_count": 3,
            "scenes_outline": [
                "场景1:对比开场 - 产品A与B并排,宣布对比维度",
                "场景2:逐项对比 - 价格/成分/含量,数据可视化",
                "场景3:结论建议 - 客观陈述差异,给出选择建议"
            ]
        },
        "emotional_story": {
            "hook_design": "人物冲突钩子：主角遇到的困境/坚守的不易,情感张力",
            "core_message": f"支持这份坚持:{cat.product_attributes.get('产地', '传统工艺')},不让它失传",
            "emotional_arc": "平静 → 冲突困境 → 坚守转折 → 情感升华",
            "call_to_action": "支持这份手艺/改变,别让它消失",
            "scenes_count": 4,
            "scenes_outline": [
                "场景1:人物登场 - 创始人/用户/匠人的日常状态",
                "场景2:遇到冲突 - 面临的困难/问题/挑战",
                "场景3:坚守时刻 - 为什么没有放弃,内心的信念",
                "场景4:升华行动 - 现在的使命,呼吁支持"
            ]
        }
    }

    concept = concepts.get(video_type, concepts["product_showcase"])

    # 如果用户填写了创意想法,修改钩子设计
    if user_idea and user_idea.strip():
        concept["hook_design"] = f"用户创意钩子：{user_idea[:50]}..."
        concept["scenes_outline"][0] = f"场景1:用户想法落实 - {user_idea[:30]}..."

    return _clean(concept, cat, plat)


# ---------------------------------------------------------------- 完整剧本 (三步式工作流 Step 2)


def demo_story(
    concept: Any,
    cat: CategoryConfig,
    plat: PlatformConfig,
    product_info: dict[str, str],
) -> dict[str, Any]:
    """演示模式：基于创意大纲生成完整剧本(Markdown格式)。"""
    video_type = concept.video_type
    sp = cat.core_selling_points
    pain = cat.pain_points

    # 根据视频类型生成不同的剧本
    if video_type == "lifestyle_seeding":
        title = "办公室一杯茶,嗓子不干了"
        logline = f"办公室打工人被{_pick(pain, 0).split('、')[0]}困扰,同事推荐{cat.name},喝了一周明显改善,决定常备。"
        content = f"""# {title}

**类型**: 场景种草类
**时长**: 15秒
**一句话故事**: {logline}

---

## 人物

- **小林**: 互联网运营 - 长期加班,嗓子经常哑

## 场景

- **办公室**: 格子间、电脑屏幕、咖啡杯,下午3点的疲惫氛围

## 道具

- {cat.name}包装、透明杯、水壶

---

## 剧本正文

### 场景一:痛点时刻 (3秒)

**画面**: 办公室格子间,下午光线,小林坐在电脑前,揉着嗓子,眉头紧锁

**动作**: 小林揉嗓子,试图清嗓,表情痛苦

**对白**:
> 小林:[小声沙哑] 又要开会...嗓子完全哑了

**情感**: 焦虑、疲惫、无助

---

### 场景二:尝试时刻 (5秒)

**画面**: 同事递过来一个透明杯,金黄色茶汤,杯壁有白霜

**动作**: 小林接过杯子,闻香,小口尝试,眼睛逐渐亮起来

**对白**:
> 同事:[轻松] 试试这个,我喝了一周,真有用
> 小林:[惊讶] 这是什么?好香

**情感**: 好奇 → 惊喜

---

### 场景三:效果与行动 (7秒)

**画面**: 特写小林满意的表情,桌上放着{cat.name}包装,背景是忙碌的办公室

**动作**: 小林咕咚喝水,满意点头,拿起手机拍照包装

**对白**:
> 小林:[声音清亮多了,轻快] 润多了!我也囤几罐,办公室常备

**情感**: 满足、想分享

---

## 创作说明

**钩子设计**: 开场3秒用真实痛点场景(嗓子哑到说不出话)建立共鸣,办公室环境让目标用户产生代入感

**产品植入**: 不是硬广,而是同事关心递过来的解决方案,自然真实

**情感共鸣点**: 用户会在"嗓子哑了又要开会"产生强烈共鸣,这是真实发生的困扰

**转化触发点**: 看到明显效果(声音清亮了)+ 真实推荐(我也囤几罐)
"""
        characters = ["小林", "同事"]
        locations = ["办公室"]
        props = [cat.name, "透明杯", "水壶"]

    elif video_type == "product_showcase":
        title = f"{cat.name}开箱:这白霜也太厚了"
        logline = f"ASMR展示{cat.name}从撕袋到冲泡的全过程,微距捕捉白霜与茶汤变化。"
        content = f"""# {title}

**类型**: 产品展示类
**时长**: 15秒
**一句话故事**: {logline}

---

## 声音设计

- **主声音**: 撕袋声、倾倒声、沸水冲击声
- **环境音**: 厨房/桌面空间感

## 场景

- **桌面**: 木质桌面或白色台面,自然光或柔和顶光,背景简洁

## 道具

- {cat.name}包装、透明玻璃杯、热水壶

---

## 剧本正文

### 场景一:撕袋钩子 (3秒)

**画面**: 特写手指捏住包装封口,包装正面logo清晰

**动作**: 手指用力撕开包装

**声音**:
- 主声音:清脆的撕袋声(嘶啦~)
- 环境音:安静的室内空间感
- 对白:无

**情感**: 好奇、期待

---

### 场景二:白霜特写 (4秒)

**画面**: 超微距俯拍,干茶倾倒入透明杯中,白霜清晰可见

**动作**: 缓慢倾倒,干茶摩擦声,白霜在光线下反光

**声音**:
- 主声音:干茶摩擦、颗粒落入杯中的声音
- 对白(可选):[轻声惊叹] 看这个白霜

**情感**: 惊艳

---

### 场景三:冲泡瞬间 (5秒)

**画面**: 侧面中景,沸水注入透明杯,茶汤瞬间变色,白霜溶解

**动作**: 水流冲击茶叶,茶汤从透明变金黄,热气上升

**声音**:
- 主声音:沸水冲击声、气泡翻腾声
- 环境音:持续的空间感

**情感**: 满足

---

### 场景四:成品呈现 (3秒)

**画面**: 特写金黄茶汤,杯壁挂霜,包装立于一旁

**动作**: 固定机位,展现最终效果

**声音**:
- 环境音:安静收尾
- 对白(可选):[平静真诚] 这品质,你看到了

**情感**: 想拥有

---

## 创作说明

**ASMR 设计**: 撕袋声作为钩子,沸水冲击声为高潮,全程物理声音主导

**视觉分层**: 包装外观 → 撕开 → 白霜微距 → 冲泡过程 → 成品,递进清晰

**微距时刻**: 白霜特写(2秒超微距)+ 冲泡瞬间(捕捉颜色变化),展现产品独特之处

**转化触发点**: 用户亲眼看到白霜、亲耳听到ASMR,产生"这品质我也要"的冲动
"""
        characters = []
        locations = ["桌面"]
        props = [cat.name, "透明玻璃杯", "热水壶"]

    else:  # 其他类型使用通用模板
        title = f"{cat.name}的故事"
        logline = f"展现{cat.name}的独特之处。"
        type_label = {"trust_building": "信任背书类", "comparison_review": "对比测评类",
                      "emotional_story": "情感故事类"}.get(video_type, "产品展示类")
        content = f"""# {title}

**类型**: {type_label}
**时长**: 15秒
**一句话故事**: {logline}

---

## 剧本正文

### 场景一:开场钩子 (3秒)

**画面**: {concept.hook_design[:50]}

**动作**: 关键动作展现

**对白/旁白**:
> 人物:核心信息传递

**情感**: 好奇、关注

---

### 场景二:核心展开 (8秒)

**画面**: 卖点展示:{_pick(sp, 0)}

**动作**: 分层递进展现

**情感**: 惊喜、信任

---

### 场景三:行动号召 (4秒)

**画面**: 成品/结果呈现

**对白**:
> {concept.call_to_action}

**情感**: 想要购买/支持

---

## 创作说明

**钩子设计**: {concept.hook_design[:50]}...

**核心信息**: {concept.core_message}

**情感曲线**: {concept.emotional_arc}

**转化触发点**: {concept.call_to_action}
"""
        characters = ["主角"]
        locations = ["场景1"]
        props = [cat.name]

    result = {
        "title": title,
        "logline": logline,
        "content": content,
        "characters": characters,
        "locations": locations,
        "props": props,
    }

    return _clean(result, cat, plat)
