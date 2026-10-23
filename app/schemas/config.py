"""类目与平台配置模型，对应 config/categories/*.json 与 config/platforms/*.json。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import AspectRatio


class VisualStyle(BaseModel):
    palette: list[str] = Field(default_factory=list)
    mood: str = ""
    props: list[str] = Field(default_factory=list)


class ProductFormField(BaseModel):
    """类目专属的商品信息输入项（界面动态表单）。"""

    key: str
    label: str
    placeholder: str = ""


class ProductLine(BaseModel):
    """类目下的具体单品（如同一品牌的多款产品），数据以包装定稿为准，原样提供给策划 Skill。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    subtitle: str = ""
    # 包装上的功能定位语（合规原句）
    positioning: str = ""
    package_color: str = ""
    package_pet: str = ""
    ingredients: list[str] = Field(default_factory=list)
    # 推荐入画的原料：外观差异最大的 3~4 味，避免同框画成一堆相似的切片
    hero_ingredients: list[str] = Field(default_factory=list)
    additives: list[str] = Field(default_factory=list)
    # 产品成分分析保证值：{成分: 数值}
    guaranteed_analysis: dict[str, str] = Field(default_factory=dict)
    usage_note: str = ""
    dosage: str = ""
    cautions: list[str] = Field(default_factory=list)
    standard: str = ""

    @model_validator(mode="after")
    def _hero_from_ingredients(self):
        extra = [n for n in self.hero_ingredients if n not in self.ingredients]
        if extra:
            raise ValueError(f"{self.name}：hero_ingredients 中的 {'、'.join(extra)} 不在原料组成里")
        return self


class IngredientVisual(BaseModel):
    """一味原料的外观性状（本草性状鉴别）：只写外观、绝不写功效，让图像模型把原料画对。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    # 其他叫法（药典正名、俗名），同样用于识别画面描述中提到的原料
    aliases: list[str] = Field(default_factory=list)
    # 英文名 + 拉丁学名
    botanical: str = ""
    # 入料部位与市售常见切制形态
    part: str = ""
    # 中文性状要点：形状、尺寸、颜色、断面、质地
    appearance: str = ""
    # 可直接写进生图提示词的英文外观描述（含真实尺寸）
    prompt_en: str
    # 外观相近、同框时容易画混的其他原料
    confusable: list[str] = Field(default_factory=list)
    # 性状依据：《中国药典》2020 年版该品种条目的在线链接
    source_url: str = ""

    @property
    def names(self) -> list[str]:
        return [self.name, *self.aliases]


# 判断画面要画哪些原料时扫描的槽位 / 首帧字段。不扫 copy_text / selling_point：
# 配方屏常把十几味原料名整段排成文字，那是上图文案，不代表每味都要画出来
INGREDIENT_TEXT_FIELDS = ("title", "subtitle", "visual_description")
# 一张图最多画几味原料：再多就挤成一堆、互相画混
MAX_HERBS_IN_FRAME = 4
# 原料名后紧跟这些字时是形容词（「姜黄色」「原木香气」），不是要画的原料
DESCRIPTIVE_SUFFIXES = frozenset("色气味")


class CategoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: str
    name: str
    description: str = ""
    product_attributes: dict[str, str] = Field(default_factory=dict)
    core_selling_points: list[str] = Field(default_factory=list)
    pain_points: list[str] = Field(default_factory=list)
    target_audience: list[str] = Field(default_factory=list)
    visual_style: VisualStyle = Field(default_factory=VisualStyle)
    # 参考图角色键 -> 拍摄要求说明
    reference_image_roles: dict[str, str] = Field(default_factory=dict)
    compliance_rules: list[str] = Field(default_factory=list)
    forbidden_words: list[str] = Field(default_factory=list)
    video_scene_suggestions: list[str] = Field(default_factory=list)
    ambient_sound_library: list[str] = Field(default_factory=list)
    product_form_fields: list[ProductFormField] = Field(default_factory=list)
    product_lines: list[ProductLine] = Field(default_factory=list)
    # 检测标准清单（如 T/ZMX 001-2025、GB 2762 等）
    test_standards: list[str] = Field(default_factory=list)
    # 检测报告引用（报告编号、检测项目、结果等结构化数据）
    test_report_references: list[dict[str, str | list[str]]] = Field(default_factory=list)
    # 文化背景与品牌故事（供策划 Skill 使用）
    cultural_context: dict[str, str | list[str]] = Field(default_factory=dict)
    # 原料性状图谱：按画面实际提到的原料单独附给生图 Skill，不整体放进 category_json
    ingredient_visuals: list[IngredientVisual] = Field(default_factory=list)

    def prompt_dict(self) -> dict[str, Any]:
        """发给 Skill 的 category_json 内容（原料性状图谱体积大，按需另附）。"""
        return self.model_dump(mode="json", exclude={"ingredient_visuals"})

    def ingredients_in(self, slot_data: dict[str, Any]) -> list[IngredientVisual]:
        """槽位（或视频首帧）画面信息中提到的原料，按首次出现的顺序。"""
        return self.ingredients_mentioned(*(str(slot_data.get(k) or "") for k in INGREDIENT_TEXT_FIELDS))

    def ingredients_mentioned(self, *texts: str) -> list[IngredientVisual]:
        """找出文本中提到的原料，按首次出现的顺序返回。

        长名优先匹配并占位：「土茯苓」不会再被算作「茯苓」，「枸杞子」不会再被算作「枸杞」。
        紧跟「色 / 气 / 味」的视为形容词（如「姜黄色背景」「原木香气」），不算提到原料。
        """
        work = "\n".join(texts)
        first: dict[str, int] = {}
        pairs = sorted(((n, v) for v in self.ingredient_visuals for n in v.names if n), key=lambda x: -len(x[0]))
        for alias, v in pairs:
            pos = work.find(alias)
            while pos != -1:
                end = pos + len(alias)
                if work[end:end + 1] not in DESCRIPTIVE_SUFFIXES:
                    first[v.name] = min(first.get(v.name, pos), pos)
                work = work[:pos] + "\0" * len(alias) + work[end:]
                pos = work.find(alias, end)
        by_name = {v.name: v for v in self.ingredient_visuals}
        return [by_name[n] for n in sorted(first, key=first.__getitem__)]


class _RatioSpec(BaseModel):
    default_aspect_ratio: AspectRatio
    allowed_aspect_ratios: list[AspectRatio]
    notes: str = ""

    @model_validator(mode="after")
    def _default_in_allowed(self):
        if self.default_aspect_ratio not in self.allowed_aspect_ratios:
            raise ValueError("default_aspect_ratio 必须包含在 allowed_aspect_ratios 中")
        return self


class MainImageSpec(_RatioSpec):
    default_count: int = Field(ge=1)
    min_count: int = Field(ge=1)
    max_count: int = Field(ge=1)
    min_resolution: int = 800


class DetailPageSpec(_RatioSpec):
    default_screens: int = Field(ge=1)
    min_screens: int = Field(ge=1)
    max_screens: int = Field(ge=1)
    width_px: int = 750


class VideoSpec(_RatioSpec):
    recommended_duration_seconds: list[int] = Field(default_factory=lambda: [15, 30])


class PlatformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform_id: str
    name: str
    main_image: MainImageSpec
    detail_page: DetailPageSpec
    video: VideoSpec
    copy_style: str = ""
    forbidden_words: list[str] = Field(default_factory=list)
    # 视觉风格权重（0~1），供策划 Skill 参考
    visual_style_weights: dict[str, float] = Field(default_factory=dict)
