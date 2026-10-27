"""动效广告（Motion Graphics）方案：代码渲染的带货动效短片。

- 分镜按「拍」计时：1 拍 = 60 / BPM 秒，每个分镜的长度都是整数拍，所以每一刀都落在节拍上；
- 素材来自项目里已有的主图、详情页、视频首帧、分镜视频与参考图（也可另外上传），分镜用素材编号 A1、A2… 引用；
- 两种素材方式：「项目素材」零成本，不调用生图接口；「AI 生图」由动效导演给分镜写 AI 画面描述（visual），
  用户确认后调用生图接口为这一屏生成专属画面，生成的图作为新素材（来源 generated）绑定到该屏；
- 渲染、配乐、音效、自检全部在本机完成（app/engine/motion/），动效法则见
  config/skills/motion_director/references/motion_design_principles.md。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.common import AspectRatio, EditRecord, new_id, utc_now
from app.schemas.edit import OUTPUT_SIZES, visible_len
from app.schemas.plan import AESTHETIC_STYLE_NAMES

SceneKind = Literal["hook", "contrast", "hero", "features", "steps", "stat", "gallery", "showcase", "cta"]
SCENE_KINDS: tuple[SceneKind, ...] = ("hook", "contrast", "hero", "features", "steps", "stat", "gallery", "showcase",
                                      "cta")
SCENE_LABELS: dict[str, str] = {
    "hook": "钩子大字", "contrast": "痛点对比", "hero": "商品主视觉", "features": "卖点清单", "steps": "步骤流程",
    "stat": "数据冲击", "gallery": "图集", "showcase": "长图滚动", "cta": "收尾行动",
}
# 每类分镜的拍数范围（1 拍 = 60 / BPM 秒）
BEAT_RANGE: dict[str, tuple[int, int]] = {
    "hook": (2, 6), "contrast": (5, 12), "hero": (4, 10), "features": (5, 14), "steps": (6, 14), "stat": (4, 8),
    "gallery": (4, 10), "showcase": (6, 16), "cta": (5, 10),
}
# 需要素材的分镜：hero / showcase 至少 1 个，gallery 至少 2 个
MIN_IMAGES: dict[str, int] = {"hero": 1, "showcase": 1, "gallery": 2}
# 痛点对比：条目固定 2 条（之前、之后），素材 0 个或 2 个（之前、之后各一张）；
# 「之前」先停留约 0.8 秒供阅读，「之后」再落地（阅读时间里另加这一段）
CONTRAST_HOLD_SECONDS = 0.8
# 步骤流程：2~4 步，逐拍点亮（每步约一拍，阅读时间里每多一步另加 STEP_SECONDS），可配 1 张图
STEP_SECONDS = 0.65
MAX_STEPS_IMAGES = 1

MotionTransition = Literal["cut", "push", "zoom", "wipe"]
MOTION_TRANSITIONS: tuple[MotionTransition, ...] = ("cut", "push", "zoom", "wipe")
MOTION_TRANSITION_LABELS: dict[str, str] = {"cut": "卡点硬切", "push": "动势推移", "zoom": "穿梭缩放",
                                            "wipe": "色块擦除"}
CameraMove = Literal["push_in", "pull_out", "pan", "still"]
CAMERA_MOVES: tuple[CameraMove, ...] = ("push_in", "pull_out", "pan", "still")
CAMERA_LABELS: dict[str, str] = {"push_in": "缓推", "pull_out": "缓拉", "pan": "横移", "still": "静止"}

# 画面主题：发布会暗场 + 主图 / 详情页共用的 5 大美学流派
THEME_LABELS: dict[str, str] = {"keynote_dark": "发布会暗场", **AESTHETIC_STYLE_NAMES}
DEFAULT_THEME = "keynote_dark"

MOTION_FPS = 30
BPM_RANGE = (72, 128)
DEFAULT_BPM = 100
TARGET_RANGE = (8, 90)

MAX_HEADLINE_CHARS = 16
MAX_SUBLINE_CHARS = 28
MAX_ITEM_CHARS = 14
MAX_ITEMS = 4
MAX_STAT_CHARS = 12
MAX_BUTTON_CHARS = 8
MAX_SCENE_IMAGES = 9
MAX_SCENES = 16
# 项目素材 / 上传最多 40 个；AI 生图另计（每张都花了额度，生成后一律收进素材库，不因数量上限丢弃）
MAX_ASSETS = 40
# 中文阅读速度：每秒 8 个字（Netflix 简体中文字幕上限为 9 字 / 秒），每屏另加入场时间。钩子 ≤3 秒，收尾行动 ≥3 秒
READING_CHARS_PER_SECOND = 8.0
SUBLINE_WEIGHT = 0.5
ENTRANCE_SECONDS = 0.6
HOOK_MAX_SECONDS = 3.0
CTA_MIN_SECONDS = 3.0

AssetSource = Literal["main_image", "detail_page", "detail_long", "first_frame", "shot_video", "reference", "upload",
                      "screen", "generated"]
SOURCE_LABELS: dict[str, str] = {
    "main_image": "主图", "detail_page": "详情页", "detail_long": "详情页长图", "first_frame": "视频首帧",
    "shot_video": "分镜视频", "reference": "参考图", "upload": "上传", "screen": "界面截图", "generated": "AI 生图",
}
# 高 / 宽超过这个值算「长图」：放进手机 / 浏览器外框里滚动
LONG_RATIO = 2.2

# 素材方式：项目素材（零成本）/ AI 生图（调用生图接口，为写了画面描述的分镜生成专属画面）
AssetMode = Literal["project", "generate"]
ASSET_MODE_LABELS: dict[str, str] = {"project": "项目素材", "generate": "AI 生图"}
MAX_VISUALS = 8  # 动效导演一次最多给几屏写 AI 画面（控制画图花费）
MAX_VISUAL_REFS = 3  # 与生图接口一致：单次最多 3 张参考图
MAX_VISUAL_CHARS = 200
# 「插入分镜」时素材不够、改由 AI 生图补上的占位描述：生成前必须改成真实描述
VISUAL_PLACEHOLDER = "请填写这一屏的 AI 画面描述"
VISUAL_STATE_LABELS: dict[str, str] = {"none": "", "pending": "⏳ 待生成", "stale": "🔄 描述已改，待重新生成",
                                       "ready": "✅ 已生成"}
# AI 画面的出图比例：按各类版式在不同画幅里的图片框，取最接近的接口比例；钩子是全屏背景，与成片同比例
VISUAL_RATIOS: dict[str, dict[str, str]] = {
    "9:16": {"hero": "3:4", "features": "16:9", "steps": "16:9", "gallery": "2:3", "cta": "4:3"},
    "3:4": {"hero": "1:1", "features": "16:9", "steps": "16:9", "gallery": "2:3", "cta": "3:2"},
    "1:1": {"hero": "3:2", "features": "16:9", "steps": "16:9", "gallery": "2:3", "cta": "16:9"},
    "16:9": {"hero": "1:1", "features": "3:4", "steps": "3:4", "gallery": "1:1", "cta": "3:4"},
}
# 可以写 AI 画面的分镜：数据冲击不放图；长图滚动要的是详情页长图 / 界面截图，生图接口画不了；
# 痛点对比要成对的「之前 / 之后」两张图，一次生成一张对不上
VISUAL_KINDS: tuple[str, ...] = ("hook", "hero", "features", "steps", "gallery", "cta")
# 生图时告诉模型这张图在动效里怎么用（版式事实，不是创意要求）
VISUAL_USAGE: dict[str, str] = {
    "hook": "全屏背景，会被压暗并在画面中部叠加大字标题，主体放在下方或边缘，中部保持简洁",
    "hero": "商品主视觉卡片，主体完整居中，背景干净",
    "features": "卖点清单旁的配图，主体清晰，构图简洁",
    "steps": "步骤流程旁的配图（使用过程或使用场景），主体清晰，构图简洁",
    "gallery": "图集里的一张，主体清晰",
    "cta": "收尾行动的商品卡片，主体完整，背景干净",
}


def visual_ratio(kind: str, aspect_ratio: str) -> str:
    """某类分镜的 AI 画面用什么比例出图（不在表里的成片比例按横竖归到最接近的一档）。"""
    if kind == "hook":
        return aspect_ratio
    table = VISUAL_RATIOS.get(aspect_ratio)
    if table is None:
        w, h = (int(x) for x in aspect_ratio.split(":"))
        table = VISUAL_RATIOS["9:16" if h > w * 1.15 else "16:9" if w > h * 1.15 else "1:1"]
    return table.get(kind, "1:1")


_LATIN_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-_/]*")
_CJK_CHAR = re.compile(r"[⺀-鿿豈-﫿]")


def reading_len(text: str) -> float:
    """阅读长度，折算成汉字：汉字 1，一个外文单词 / 数字 1.5（「VoxCPM2」是一个词，不是 7 个字）。"""
    return len(_CJK_CHAR.findall(text or "")) + 1.5 * len(_LATIN_WORD.findall(text or ""))


def label_code(value: Any, labels: dict[str, str]) -> str | None:
    """接受代码（push）或中文名（动势推移）。"""
    v = str(value or "").strip()
    if v in labels:
        return v
    for code, label in labels.items():
        if v == label:
            return code
    return None


class MotionAsset(BaseModel):
    """一个可入画的素材：图片或视频。key 是分镜引用它的编号（A1、A2…）。"""

    key: str
    kind: Literal["image", "video"] = "image"
    path: Path
    label: str = ""
    source: AssetSource = "upload"
    width: int = 0
    height: int = 0
    duration_seconds: float = 0.0
    # AI 生图素材：为哪一屏生成、按哪段画面描述生成、用的哪个模型
    scene_id: str = ""
    visual: str = ""
    model: str = ""

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def ratio(self) -> float:
        """高 / 宽。"""
        return self.height / self.width if self.width else 1.0

    @property
    def shape(self) -> str:
        r = self.ratio
        return "长图" if r > LONG_RATIO else "竖图" if r > 1.3 else "横图" if r < 0.8 else "方图"

    def payload(self) -> dict[str, Any]:
        """发给动效导演 Skill 的素材说明（不含本机路径）。"""
        shape = self.shape
        data = {"key": self.key, "type": "视频" if self.kind == "video" else "图片",
                "source": SOURCE_LABELS[self.source], "label": self.label, "shape": shape}
        if self.kind == "video":
            data["seconds"] = round(self.duration_seconds, 1)
        if self.source == "generated" and self.visual:
            data["visual"] = self.visual  # 之前生成过的 AI 画面：说明画的是什么，便于 Skill 复用
        return data


def _clean(text: Any) -> str:
    return " ".join(str(text or "").split())


class MotionScene(BaseModel):
    """一个动效分镜。长度按拍计；文字上限与入场时间都在这里校验。"""

    scene_id: str = Field(default_factory=lambda: new_id("mscene"))
    kind: SceneKind
    beats: int = Field(ge=1, le=32)
    headline: str = ""
    subline: str = ""
    # 主标题里要强调的词（必须是主标题的一部分），用主题强调色渐变显示
    accent: str = ""
    # 卖点清单的条目；收尾行动的第 1 条是按钮文字
    items: list[str] = Field(default_factory=list)
    # 数据冲击的数字（如 39.3%），或收尾行动的价格 / 活动胶囊（如 买2发3）
    stat_value: str = ""
    images: list[str] = Field(default_factory=list)
    camera: CameraMove = "push_in"
    transition_out: MotionTransition = "push"
    # AI 生图方式：这一屏的 AI 画面描述与生图参考图编号（保持商品包装一致）。
    # 生成的画面记在 visual_key（由 MotionAd.bind_visual 设置，表格不直接改），渲染时放在该屏第一个素材的位置
    visual: str = ""
    visual_refs: list[str] = Field(default_factory=list)
    visual_key: str = ""

    @field_validator("headline", "subline", "accent", "stat_value", "visual", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> str:
        return _clean(v)

    @field_validator("items", mode="before")
    @classmethod
    def _split_items(cls, v: Any) -> list[str]:
        """条目用「｜」或换行分隔（条目里本身可能有顿号、逗号）。"""
        if isinstance(v, str):
            v = re.split(r"[|｜\n]", v)
        return [s for s in (_clean(x) for x in (v or [])) if s]

    @field_validator("images", "visual_refs", mode="before")
    @classmethod
    def _split_images(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            v = re.split(r"[,，、|｜\s]+", v)
        return list(dict.fromkeys(s.upper() for s in (_clean(x) for x in (v or [])) if s))

    @model_validator(mode="after")
    def _check(self):
        label = SCENE_LABELS[self.kind]
        lo, hi = BEAT_RANGE[self.kind]
        if not lo <= self.beats <= hi:
            raise ValueError(f"{label}的拍数应在 {lo}~{hi} 拍之间（当前 {self.beats}）")
        for name, text, limit in (("主标题", self.headline, MAX_HEADLINE_CHARS),
                                  ("副标题", self.subline, MAX_SUBLINE_CHARS),
                                  ("数字 / 胶囊", self.stat_value, MAX_STAT_CHARS)):
            if visible_len(text) > limit:
                raise ValueError(f"{label}的{name}最多 {limit} 个字（当前 {visible_len(text)} 个）")
        if self.accent and self.accent not in self.headline:
            raise ValueError(f"强调词「{self.accent}」必须是主标题里的一部分")
        if len(self.items) > MAX_ITEMS:
            raise ValueError(f"{label}最多 {MAX_ITEMS} 条（当前 {len(self.items)} 条）")
        limit = MAX_BUTTON_CHARS if self.kind == "cta" else MAX_ITEM_CHARS
        for item in self.items:
            if visible_len(item) > limit:
                raise ValueError(f"{label}的条目「{item}」最多 {limit} 个字")
        if self.kind == "features" and len(self.items) < 2:
            raise ValueError("卖点清单至少 2 条")
        if self.kind == "contrast":
            if len(self.items) != 2:
                raise ValueError("痛点对比需要 2 条：第 1 条写「之前」的状态，第 2 条写「之后」的状态")
            if len(self.images) not in (0, 2):
                raise ValueError("痛点对比的素材填 0 个或 2 个（之前、之后各一张）")
        if self.kind == "steps":
            if len(self.items) < 2:
                raise ValueError("步骤流程至少 2 步")
            if len(self.images) > MAX_STEPS_IMAGES:
                raise ValueError(f"步骤流程最多配 {MAX_STEPS_IMAGES} 张图")
        if self.kind == "stat" and not self.stat_value:
            raise ValueError("数据冲击需要填写数字（如 39.3%）")
        if not self.headline and self.kind in {"hook", "cta"}:
            raise ValueError(f"{label}需要主标题")
        if len(self.images) > MAX_SCENE_IMAGES:
            raise ValueError(f"每个分镜最多 {MAX_SCENE_IMAGES} 个素材")
        if self.visual and self.kind not in VISUAL_KINDS:
            raise ValueError(f"{label}不需要 AI 画面描述（可以写的分镜："
                             f"{' / '.join(SCENE_LABELS[k] for k in VISUAL_KINDS)}）")
        if len(self.visual) > MAX_VISUAL_CHARS:
            raise ValueError(f"AI 画面描述最多 {MAX_VISUAL_CHARS} 个字（当前 {len(self.visual)} 个）")
        if len(self.visual_refs) > MAX_VISUAL_REFS:
            raise ValueError(f"生图参考图最多 {MAX_VISUAL_REFS} 张")
        # 写了 AI 画面描述的分镜，生成的画面会补上第一个素材，所以生成之前可以少 1 个
        need = MIN_IMAGES.get(self.kind, 0) - (1 if self.visual else 0)
        if len(self.images) < need:
            raise ValueError(f"{label}至少需要 {need} 个素材（填素材编号，如 A1）")
        return self

    @property
    def label(self) -> str:
        return SCENE_LABELS[self.kind]

    @property
    def item_id(self) -> str:
        """与主图槽位 / 视频分镜同名的标识，供并发生图引擎使用。"""
        return self.scene_id

    @property
    def text_chars(self) -> float:
        """画面上要读的字数（按 reading_len 折算）。副标题是扫一眼的补充信息，按一半计。"""
        texts = [self.headline, self.stat_value, *self.items]
        return sum(reading_len(t) for t in texts) + reading_len(self.subline) * SUBLINE_WEIGHT

    @property
    def reading_seconds(self) -> float:
        """读完这一屏需要的最短时间：字数 / 阅读速度 + 入场；收尾行动至少 3 秒。"""
        need = self.text_chars / READING_CHARS_PER_SECOND + ENTRANCE_SECONDS
        if self.kind == "features":
            need += 0.2 * len(self.items)  # 逐条错峰入场
        if self.kind == "contrast":
            need += CONTRAST_HOLD_SECONDS  # 「之前」先单独停留，「之后」才落地
        if self.kind == "steps":
            need += STEP_SECONDS * (len(self.items) - 1)  # 逐拍点亮
        if self.kind == "cta":
            need = max(need, CTA_MIN_SECONDS)
        return round(max(need, 1.2), 2)


class MotionRender(BaseModel):
    """一次渲染的成品（每次渲染都新建，不覆盖）。"""

    render_id: str = Field(default_factory=lambda: new_id("mrender"))
    video_path: Path
    contact_sheet_path: Path | None = None
    report_md: str = ""
    quality: Literal["preview", "final"] = "final"
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: int = MOTION_FPS
    seconds: float = 0.0
    warnings: int = 0
    created_at: datetime = Field(default_factory=utc_now)


@dataclass(frozen=True)
class SceneSpan:
    """时间轴上的一个分镜：起止帧（左闭右开）与秒数。"""

    index: int
    scene: MotionScene
    start: int
    end: int
    fps: int

    @property
    def frames(self) -> int:
        return self.end - self.start

    @property
    def start_s(self) -> float:
        return self.start / self.fps

    @property
    def end_s(self) -> float:
        return self.end / self.fps

    @property
    def seconds(self) -> float:
        return self.frames / self.fps


class MotionAd(BaseModel):
    """一条动效广告：分镜、素材、节拍、主题与渲染记录。"""

    motion_id: str = Field(default_factory=lambda: new_id("motion"))
    title: str = ""
    aspect_ratio: AspectRatio = "9:16"
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: int = Field(default=MOTION_FPS, ge=12, le=60)
    bpm: int = Field(default=DEFAULT_BPM, ge=BPM_RANGE[0], le=BPM_RANGE[1])
    theme: str = DEFAULT_THEME
    target_seconds: int = Field(default=20, ge=TARGET_RANGE[0], le=TARGET_RANGE[1])
    scenes: list[MotionScene] = Field(default_factory=list)
    assets: list[MotionAsset] = Field(default_factory=list)
    music: bool = True
    sfx: bool = True
    grain: bool = True
    asset_mode: AssetMode = "project"
    # AI 生图方式：编排时最多几屏写 AI 画面（界面回填用）
    max_visuals: int = Field(default=4, ge=1, le=MAX_VISUALS)
    director_skill_id: str = ""
    planner_mode: str = ""
    notes: str = ""
    user_motion_idea: str | None = None
    renders: list[MotionRender] = Field(default_factory=list)
    edit_log: list[EditRecord] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("theme")
    @classmethod
    def _theme(cls, v: str) -> str:
        if v not in THEME_LABELS:
            raise ValueError(f"未知的画面主题：{v}")
        return v

    @classmethod
    def sized(cls, aspect_ratio: str, **fields: Any) -> MotionAd:
        w, h = OUTPUT_SIZES[aspect_ratio]
        return cls(aspect_ratio=aspect_ratio, width=w, height=h, **fields)

    def _log(self, action: str, target_id: str | None = None, **details: Any) -> None:
        self.edit_log.append(EditRecord(action=action, target_id=target_id, details=details))

    # ---------------------------------------------------------------- 时间轴

    @property
    def beat_seconds(self) -> float:
        return 60.0 / self.bpm

    def timeline(self) -> list[SceneSpan]:
        """分镜起止帧：按累计拍数换算，四舍五入到整数帧，片段首尾相接。"""
        spans, beats = [], 0
        for i, s in enumerate(self.scenes):
            start = round(beats * self.beat_seconds * self.fps)
            beats += s.beats
            end = round(beats * self.beat_seconds * self.fps)
            spans.append(SceneSpan(i, s, start, end, self.fps))
        return spans

    @property
    def total_frames(self) -> int:
        spans = self.timeline()
        return spans[-1].end if spans else 0

    @property
    def total_seconds(self) -> float:
        return round(self.total_frames / self.fps, 2)

    # ---------------------------------------------------------------- 素材

    def asset(self, key: str) -> MotionAsset:
        for a in self.assets:
            if a.key == key:
                return a
        raise KeyError(f"素材 {key} 不存在")

    @property
    def project_asset_count(self) -> int:
        """计入 40 个上限的素材数（AI 生图不计）。"""
        return sum(a.source != "generated" for a in self.assets)

    def next_asset_key(self) -> str:
        used = {int(a.key[1:]) for a in self.assets if a.key[1:].isdigit()}
        return f"A{max(used, default=0) + 1}"

    def add_asset(self, path: Path, *, kind: str = "image", label: str = "", source: str = "upload",
                  width: int = 0, height: int = 0, duration_seconds: float = 0.0, scene_id: str = "",
                  visual: str = "", model: str = "") -> MotionAsset:
        if source != "generated" and self.project_asset_count >= MAX_ASSETS:
            raise ValueError(f"素材最多 {MAX_ASSETS} 个")
        asset = MotionAsset(key=self.next_asset_key(), kind=kind, path=Path(path), label=label, source=source,
                            width=width, height=height, duration_seconds=duration_seconds, scene_id=scene_id,
                            visual=visual, model=model)
        self.assets.append(asset)
        self._log("add_asset", asset.key, label=label, source=source)
        return asset

    def _check_images(self, scene: MotionScene) -> None:
        by_key = {a.key: a for a in self.assets}
        missing = [k for k in [*scene.images, *scene.visual_refs] if k not in by_key]
        if missing:
            raise ValueError(f"素材 {'、'.join(dict.fromkeys(missing))} 不存在"
                             f"（可用：{'、'.join(sorted(by_key)) or '无'}）")
        bad = [k for k in scene.visual_refs if by_key[k].kind != "image" or by_key[k].shape == "长图"]
        if bad:
            raise ValueError(f"生图参考只能用普通图片素材（{'、'.join(bad)} 是视频或长图）")

    # ---------------------------------------------------------------- AI 画面（AI 生图方式）

    def visual_asset(self, scene: MotionScene) -> MotionAsset | None:
        """该屏当前绑定的 AI 画面（文件已不存在时视为没有）。"""
        for a in self.assets:
            if scene.visual_key and a.key == scene.visual_key:
                return a if a.exists else None
        return None

    def visual_state(self, scene: MotionScene) -> str:
        """none 没写 AI 画面 / pending 待生成 / stale 描述改过、当前还是旧画面 / ready 已生成。"""
        if not scene.visual:
            return "none"
        a = self.visual_asset(scene)
        if a is None:
            return "pending"
        return "ready" if a.visual == scene.visual else "stale"

    def visual_label(self, scene: MotionScene) -> str:
        """表格「画面状态」列。"""
        state = self.visual_state(scene)
        if state == "pending" and scene.visual == VISUAL_PLACEHOLDER:
            return "✏️ 请先填写描述"
        if state in {"ready", "stale"}:
            return f"{VISUAL_STATE_LABELS[state]}（{scene.visual_key}）"
        return VISUAL_STATE_LABELS[state]

    def visual_references(self, scene: MotionScene) -> list[MotionAsset]:
        """该屏生图时传给接口的参考图（文件还在的图片素材，按填写顺序）。"""
        by_key = {a.key: a for a in self.assets}
        refs = [by_key[k] for k in scene.visual_refs if k in by_key]
        return [a for a in refs if a.kind == "image" and a.exists][:MAX_VISUAL_REFS]

    def bind_visual(self, scene_id: str, path: Path, *, visual: str, model: str, width: int,
                    height: int) -> MotionAsset:
        """把生成的画面加进素材库并绑定到该屏（替换上一版 AI 画面；旧图留在素材库里，不删除）。

        分镜在生成期间被删掉时，画面仍然留在素材库里，不浪费已经花掉的额度。
        """
        index = next((i for i, s in enumerate(self.scenes) if s.scene_id == scene_id), None)
        where = f"第 {index + 1} 屏" if index is not None else "已删除的分镜"
        asset = self.add_asset(path, kind="image", label=f"{where} AI 画面：{visual[:18]}", source="generated",
                               width=width, height=height, scene_id=scene_id, visual=visual, model=model)
        if index is not None:
            self.scenes[index] = self.scenes[index].model_copy(update={"visual_key": asset.key})
            self._log("bind_visual", scene_id, asset=asset.key)
        else:  # 生成期间重新编排过：新方案里描述相同的分镜直接用上这张
            self.reuse_visuals()
        return asset

    def reuse_visuals(self) -> int:
        """描述与之前某次生成完全相同的分镜直接沿用那张画面（重新编排时不重复花钱）。返回沿用的屏数。"""
        latest = {a.visual: a.key for a in self.assets if a.source == "generated" and a.visual and a.exists}
        n = 0
        for i, s in enumerate(self.scenes):
            if self.visual_state(s) != "ready" and s.visual in latest:
                self.scenes[i] = s.model_copy(update={"visual_key": latest[s.visual]})
                n += 1
        return n

    @property
    def draft_visuals(self) -> list[int]:
        """「AI 画面」还是占位文字的屏号（从 1 开始）：生成前必须改成真实描述。"""
        return [i + 1 for i, s in enumerate(self.scenes) if s.visual == VISUAL_PLACEHOLDER]

    def visual_targets(self, scene_ids: list[str] | None = None) -> list[MotionScene]:
        """要生成 AI 画面的分镜。

        指定 scene_ids 时重新生成这几屏（没写描述或还是占位文字时报错）；
        不指定时取待生成 / 描述改过的分镜，跳过还是占位文字的（见 draft_visuals）。
        """
        if scene_ids is None:
            return [s for s in self.scenes
                    if self.visual_state(s) in {"pending", "stale"} and s.visual != VISUAL_PLACEHOLDER]
        todo = [s for s in self.scenes if s.scene_id in scene_ids]
        bare = [self.scenes.index(s) + 1 for s in todo if not s.visual]
        if bare:
            raise ValueError(f"第 {'、'.join(map(str, bare))} 屏没有填写「AI 画面」描述，先在表格里写好再生成")
        draft = [self.scenes.index(s) + 1 for s in todo if s.visual == VISUAL_PLACEHOLDER]
        if draft:
            raise ValueError(f"第 {'、'.join(map(str, draft))} 屏的「AI 画面」还是占位文字，请改成真实的画面描述再生成")
        return todo

    def render_keys(self, scene: MotionScene) -> list[str]:
        """渲染用的素材编号：写了 AI 画面且已生成时，生成的画面排第一（描述改过还没重新生成时先用旧画面）。"""
        if scene.visual and self.visual_asset(scene) is not None:
            return [scene.visual_key, *[k for k in scene.images if k != scene.visual_key]][:MAX_SCENE_IMAGES]
        return list(scene.images)

    def render_copy(self) -> MotionAd:
        """渲染用的方案快照：每屏的素材换成 render_keys（渲染器只认 images）。"""
        snap = self.model_copy(deep=True)
        snap.scenes = [s.model_copy(update={"images": self.render_keys(s)}) for s in snap.scenes]
        return snap

    # ---------------------------------------------------------------- 编辑

    def get_scene(self, scene_id: str) -> MotionScene:
        for s in self.scenes:
            if s.scene_id == scene_id:
                return s
        raise KeyError(f"分镜不存在：{scene_id}")

    def replace_scenes(self, scenes: list[MotionScene]) -> None:
        if len(scenes) > MAX_SCENES:
            raise ValueError(f"分镜最多 {MAX_SCENES} 个")
        for s in scenes:
            self._check_images(s)
        self.scenes = list(scenes)
        self._log("replace_scenes", None, count=len(scenes))

    def update_scene(self, scene_id: str, /, **changes: Any) -> MotionScene:
        """修改一个分镜：合并后整体重新校验，不合法时原分镜保持不变。"""
        old = self.get_scene(scene_id)
        data = old.model_dump()
        data.update(changes)
        new = MotionScene.model_validate(data)
        self._check_images(new)
        changed = sorted(k for k in changes if getattr(old, k) != getattr(new, k))
        if changed:
            self.scenes[self.scenes.index(old)] = new
            self._log("update_scene", scene_id, fields=changed)
        return self.get_scene(scene_id)

    def insert_scene(self, scene: MotionScene, index: int | None = None) -> MotionScene:
        if len(self.scenes) >= MAX_SCENES:
            raise ValueError(f"分镜最多 {MAX_SCENES} 个")
        self._check_images(scene)
        self.scenes.insert(len(self.scenes) if index is None else index, scene)
        self._log("insert_scene", scene.scene_id, kind=scene.kind)
        return scene

    def remove_scene(self, scene_id: str) -> None:
        if len(self.scenes) <= 1:
            raise ValueError("至少保留一个分镜")
        self.scenes.remove(self.get_scene(scene_id))
        self._log("remove_scene", scene_id)

    def reorder(self, scene_ids: list[str]) -> None:
        if sorted(scene_ids) != sorted(s.scene_id for s in self.scenes):
            raise ValueError("调整顺序时分镜列表不一致")
        by_id = {s.scene_id: s for s in self.scenes}
        self.scenes = [by_id[i] for i in scene_ids]
        self._log("reorder", None, order=scene_ids)

    def set_options(self, *, music: bool | None = None, sfx: bool | None = None, grain: bool | None = None,
                    bpm: int | None = None, theme: str | None = None, target_seconds: int | None = None,
                    aspect_ratio: str | None = None) -> None:
        changes: dict[str, Any] = {}
        if aspect_ratio is not None and aspect_ratio != self.aspect_ratio:
            if aspect_ratio not in OUTPUT_SIZES:
                raise ValueError(f"不支持的画面比例：{aspect_ratio}")
            changes["aspect_ratio"] = aspect_ratio
            changes["width"], changes["height"] = OUTPUT_SIZES[aspect_ratio]
        if target_seconds is not None:
            if not TARGET_RANGE[0] <= int(target_seconds) <= TARGET_RANGE[1]:
                raise ValueError(f"目标时长应在 {TARGET_RANGE[0]}~{TARGET_RANGE[1]} 秒之间")
            changes["target_seconds"] = int(target_seconds)
        if bpm is not None:
            if not BPM_RANGE[0] <= int(bpm) <= BPM_RANGE[1]:
                raise ValueError(f"节拍速度应在 {BPM_RANGE[0]}~{BPM_RANGE[1]} BPM 之间")
            changes["bpm"] = int(bpm)
        if theme is not None:
            changes["theme"] = self._theme(theme)
        for name, value in (("music", music), ("sfx", sfx), ("grain", grain)):
            if value is not None:
                changes[name] = bool(value)
        changes = {k: v for k, v in changes.items() if getattr(self, k) != v}
        for k, v in changes.items():
            setattr(self, k, v)
        if changes:
            self._log("set_options", None, **changes)

    @property
    def visual_scenes(self) -> list[MotionScene]:
        """写了 AI 画面描述的分镜。"""
        return [s for s in self.scenes if s.visual]

    def add_render(self, render: MotionRender) -> None:
        self.renders.append(render)
        self._log("render", render.render_id, quality=render.quality, seconds=render.seconds)

    @property
    def latest_render(self) -> MotionRender | None:
        found = [r for r in self.renders if r.video_path.exists()]
        return found[-1] if found else None

    def require_renderable(self) -> None:
        if not self.scenes:
            raise ValueError("还没有动效分镜，请先生成动效方案")
        pending = [str(i + 1) for i, s in enumerate(self.scenes) if self.visual_state(s) == "pending"]
        if pending:
            raise ValueError(f"第 {'、'.join(pending)} 屏写了「AI 画面」但还没生成：请在 Step 2 点「🎨 生成 AI 画面」；"
                             "不想调用生图接口的话，清空这几屏的「AI 画面」列并填上素材编号，"
                             "或换成「项目素材」方式重新编排")
        for s in self.scenes:
            self._check_images(s)
            for k in self.render_keys(s):
                if not self.asset(k).exists:
                    raise ValueError(f"素材 {k} 的文件已不存在，请重新生成方案或换一个素材")


def beats_for(seconds: float, bpm: int) -> int:
    """至少要多少拍才能放下 seconds 秒。"""
    return max(1, math.ceil(seconds / (60.0 / bpm) - 1e-6))
