"""类目与平台配置模型，对应 config/categories/*.json 与 config/platforms/*.json。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import AspectRatio


class VisualStyle(BaseModel):
    palette: list[str] = Field(default_factory=list)
    mood: str = ""
    props: list[str] = Field(default_factory=list)


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
