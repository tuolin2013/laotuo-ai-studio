"""项目：一个商品在某个平台上的全部策划、剧本与素材。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import new_id, utc_now
from app.schemas.plan import ImagePlan
from app.schemas.video import VideoScript


class Project(BaseModel):
    project_id: str = Field(default_factory=lambda: new_id("proj"))
    name: str
    category_id: str
    platform_id: str
    product_name: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    # 三个选项卡各自独立的工作区
    main_image_plan: ImagePlan | None = None
    detail_page_plan: ImagePlan | None = None
    video_script: VideoScript | None = None

    # 当前会话的 Skill 选择（卡槽 -> skill_id）
    selected_skills: dict[str, str] = Field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = utc_now()
