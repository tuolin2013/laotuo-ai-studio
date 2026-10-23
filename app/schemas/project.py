"""项目：一个商品在某个平台上的全部策划、剧本与素材。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import new_id, utc_now
from app.schemas.edit import EditPlan
from app.schemas.plan import ImagePlan
from app.schemas.video import ConceptOutline, ReferenceVideoSource, StoryScript, VideoScript
from app.schemas.voice import VoiceStudio


class Project(BaseModel):
    project_id: str = Field(default_factory=lambda: new_id("proj"))
    name: str
    category_id: str
    platform_id: str
    product_name: str = ""
    # 类目动态表单填写的商品信息（key -> 值）
    product_info: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    # 三个选项卡各自独立的工作区
    main_image_plan: ImagePlan | None = None
    detail_page_plan: ImagePlan | None = None

    # 视频三步式工作流
    video_concept: ConceptOutline | None = None  # Step 1: 创意大纲
    video_story: StoryScript | None = None        # Step 2: 完整剧本
    video_script: VideoScript | None = None       # Step 3: 分镜脚本
    # 克隆模式：最近一次上传并抽帧的参考爆款视频
    video_reference: ReferenceVideoSource | None = None
    # 视频 Step 4：剪辑大师生成的剪辑方案（一键成片 / 导出剪映、达芬奇草稿）
    video_edit: EditPlan | None = None
    # 【配音】选项卡：VoxCPM2 配音文案、音色与逐段配音（独立成品，不写入视频分镜）
    voice_studio: VoiceStudio | None = None

    # 当前会话的 Skill 选择（卡槽 -> skill_id）
    selected_skills: dict[str, str] = Field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = utc_now()
