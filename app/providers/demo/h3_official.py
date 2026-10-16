"""零 Key 演示：按官方 h3-prompt-writing 结构生成 H3 提示词。

- i2va：references/base-en.txt 的 I2VA 指令行 + integrated_multimodal_description / overall_soundscape /
  non_diegetic_music 三段。
- ref2va：references/ref-en.txt 的六段式，<Picture N> 标签在各段保持一致。

画面与声音描述用英文；人物台词与包装可见文字保持中文原文。
"""

from __future__ import annotations

import re
from typing import Any

from app.engine.compiler import I2VA_INSTRUCTION, h3_target_seconds, picture_roles
from app.schemas.config import CategoryConfig

SHOT_SIZE_EN = {"微距": "a macro close-up", "特写": "a close-up", "近景": "a medium close-up",
                "中景": "a medium shot", "全景": "a wide shot", "远景": "an extreme wide shot"}

CAMERA_EN: list[tuple[tuple[str, ...], str]] = [
    (("微距固定",), "holds a static macro shot"),
    (("推近", "推进"), "pushes in with small amplitude at slow speed"),
    (("拉远", "拉出"), "pulls out with small amplitude at slow speed"),
    (("左摇",), "pans left with small amplitude at slow speed"),
    (("右摇",), "pans right with small amplitude at slow speed"),
    (("上移",), "tilts up with small amplitude at slow speed"),
    (("下移",), "tilts down with small amplitude at slow speed"),
    (("环绕",), "arcs around the subject with small amplitude at slow speed"),
    (("跟随", "跟拍"), "follows the moving hand in a slow tracking shot"),
    (("手持",), "shakes slightly as a handheld shot"),
    (("固定",), "holds a static shot"),
]

# 环境音关键词 → 英文具体音效（覆盖内置类目的 ambient_sound_library）
SOUND_EN: list[tuple[str, str]] = [
    ("注入", "boiling water pours into the glass with a bright, rushing splash and a faint ring of the glass wall"),
    ("鸟鸣", "early-morning birdsong echoes across the forested peaks with a distant stream trickling below"),
    ("摩挲", "dry leaves rustle with a crisp, sandy crackle between the fingertips"),
    ("轻放", "the cup is set down on the wooden table with a short, dull tap"),
    ("冰块", "ice cubes drop into the cold brew and clink against the glass"),
    ("嘶啦", "the foil sachet tears open with a sharp ripping sound"),
    ("粉末", "fine powder patters softly onto the dry kibble"),
    ("咀嚼", "the cat crunches the kibble in a steady rhythm"),
    ("爪子", "short, happy barks mix with claws tapping quickly across the wooden floor"),
    ("食盆", "the metal food bowl knocks lightly against the floor"),
    ("纸张", "a sheet of paper turns over with a soft, dry rustle"),
    ("翻动", "the packaging bag crinkles as it is turned over"),
    ("键盘", "light keyboard typing continues in the background"),
    ("底噪", "a low, quiet indoor room tone continues underneath"),
]

DELIVERY_EN: list[tuple[str, str]] = [
    ("惊喜", "a bright, surprised tone at a brisk pace"),
    ("无奈", "a resigned, self-mocking tone at a relaxed pace"),
    ("真诚", "a sincere tone at a deliberately slowed pace"),
    ("小声", "a soft, quiet voice at an even pace"),
    ("认真", "a focused, careful tone at an even pace"),
    ("笑", "a relaxed, smiling tone at a natural pace"),
]

PURPOSE_ACTION_EN = {
    "hook": "a single striking detail grabs attention within the first second",
    "selling_point": "the product's key detail is demonstrated up close",
    "proof": "the packaging and its supporting document are shown side by side for verification",
    "scene": "the surrounding environment establishes where the product comes from and how it is used",
    "call_to_action": "the presenter lifts the product toward the camera and gives a short recommendation",
}

PRODUCT_EN = {
    "zhangjiajie_berry_tea": ("a resealable package of Zhangjiajie vine tea",
                              "dried vine-tea leaves coated with a fine white frost"),
    "pet_supplement": ("a pouch of pet nutritional supplement", "fine supplement powder and small dry kibble"),
}


def _quoted(text: str) -> str:
    """取对白中引号内的原文台词；没有引号时整句保留。"""
    found = re.findall(r"[“\"「](.+?)[”\"」]", text or "")
    return found[-1] if found else (text or "").strip()


def _speaker(text: str) -> str:
    if "女" in text:
        return "the young woman"
    if "铲屎官" in text:
        return "the young pet owner"
    if "男" in text:
        return "the young man"
    return "the on-screen presenter"


def _delivery(text: str) -> str:
    return next((en for k, en in DELIVERY_EN if k in text), "a clear, natural tone at an even pace")


def _camera(cam: str) -> str:
    return next((en for keys, en in CAMERA_EN if any(k in cam for k in keys)), "holds a static shot")


def _sounds(ambient: str) -> list[str]:
    hits = [en for k, en in SOUND_EN if k in (ambient or "")]
    return hits or (["a low, quiet indoor room tone continues underneath"] if ambient else [])


def _product(cat: CategoryConfig) -> tuple[str, str]:
    return PRODUCT_EN.get(cat.category_id, (f"the {cat.category_id.replace('_', ' ')} product package",
                                            "the product's contents"))


def _beats(total: float) -> tuple[str, str, str]:
    return f"{total * 0.3:.2f}", f"{total * 0.4:.2f}", f"{total * 0.75:.2f}"


def _body(shot: dict[str, Any], cat: CategoryConfig, total: float, subject: str, frame_label: str) -> str:
    """单镜头时间轴正文：首帧锚定 → 动作起始 → 连续发展 → 结果，含声音出现时间点。"""
    package, content = _product(cat)
    size = next((en for k, en in SHOT_SIZE_EN.items() if k in shot.get("shot_size", "")), "a medium shot")
    t1, t_say, t3 = _beats(total)
    mode = shot.get("audio_mode")
    dialogue = shot.get("character_dialogue") or ""
    sounds = _sounds(shot.get("ambient_sound_description", ""))
    brand = cat.name
    parts = [
        f"[Shot 1] Live-action commercial product footage, {size} that begins exactly from {frame_label}, "
        f"preserving its composition, lighting, and the position of {subject}: {package} whose shape, colors, logo, "
        f'and printed Chinese brand text "{brand}" remain unchanged for the whole video.',
        f"From 0.00 to {t1} seconds, the frame stays on the opening composition while soft side light from the left "
        f"outlines {content}.",
        f"At {t1} seconds, the camera {_camera(shot.get('camera_movement', ''))} as {PURPOSE_ACTION_EN.get(shot.get('shot_purpose'), PURPOSE_ACTION_EN['selling_point'])}.",  # noqa: E501
    ]
    if sounds and mode in {"ambient_sfx", "dialogue_and_ambient"}:
        parts.append(f"At the same moment, {sounds[0]}.")
    if mode in {"native_dialogue", "dialogue_and_ambient"} and dialogue:
        parts.append(
            f"At {t_say} seconds, {_speaker(dialogue)} (S1) faces the camera and, in {_delivery(dialogue)}, says: "
            f"<d>[Chinese] {_quoted(dialogue)}</d> The lip movements stay synchronized with every syllable."
        )
    parts.append(
        f"From {t3} to {total:.2f} seconds, the movement slows to a stop and the shot settles on a clear frontal view "
        f'of the package with "{brand}" fully legible, ending at {total:.2f} seconds.'
    )
    return " ".join(parts)


def _soundscape(shot: dict[str, Any]) -> str:
    if shot.get("audio_mode") == "silent":
        return "N/A"
    sounds = _sounds(shot.get("ambient_sound_description", ""))
    if shot.get("audio_mode") == "native_dialogue" or not sounds:
        return "A quiet indoor room tone continues throughout, with soft fabric movement as the presenter shifts."
    text = " ".join(s[0].upper() + s[1:] + "." for s in sounds)
    if shot.get("audio_mode") == "dialogue_and_ambient":
        text += " These sounds stay lower than the spoken line."
    return text


def demo_official_h3_prompt(
    shot: dict[str, Any], aspect_ratio: str, cat: CategoryConfig, mode: str, reference_roles: list[str]
) -> dict[str, Any]:
    total = h3_target_seconds(float(shot.get("duration_seconds", 4)))
    package, _ = _product(cat)
    music = "N/A"
    if mode == "i2va":
        body = _body(shot, cat, total, "the product", "<Picture 1>")
        prompt = (f"{I2VA_INSTRUCTION}\n\n"
                  f"integrated_multimodal_description: {body}\n\n"
                  f"overall_soundscape: {_soundscape(shot)}\n\n"
                  f"non_diegetic_music: {music}")
        return {"mode": mode, "prompt": prompt}

    roles = picture_roles(reference_roles)
    defs = [f"<Picture 1> is the first frame of [Shot 1], the locked {aspect_ratio} opening frame in which the "
            "product packaging has been verified as undistorted."]
    # 商品参考图只用于定义 <Subject 1>：按 ref-en.txt 2.2 节，在主体定义里引用，不单独成行
    src = "".join(f", <Picture {i}> ({role.replace('_', ' ')})" for i, role in enumerate(roles[1:], start=2))
    defs.append(f"<Subject 1> is {package} shown in <Picture 1>{src}, with its exact shape, colors, logo, "
                f'and printed Chinese text "{cat.name}".')
    retention = [
        "<Picture 1> ([Shot 1] first frame): fully_preserved - the composition, lighting, and product placement "
        "of the opening frame are kept at 0.00 seconds.",
        "<Subject 1> (appears in [Shot 1]): fully_preserved - the packaging shape, colors, logo, and Chinese brand "
        "text stay identical throughout.",
    ]
    summary = (f"[keyframe completion + reference generation] The target video is a {total:.2f}-second {aspect_ratio} "
               "e-commerce product shot that starts from <Picture 1> and shows <Subject 1> with native synchronized "
               "sound.")
    detail = ("The target video uses realistic live-action commercial product photography with soft, even side "
              "lighting and natural colors.\n" + _body(shot, cat, total, "<Subject 1>", "<Picture 1>"))
    prompt = "\n\n".join([
        "subject_definitions:\n" + "\n".join(defs),
        "summary:\n" + summary,
        "retention_analysis:\n" + "\n".join(retention),
        "detailed_description:\n" + detail,
        "overall_soundscape:\n" + _soundscape(shot),
        "non_diegetic_music:\n" + music,
    ])
    return {"mode": mode, "prompt": prompt}
