"""引擎运行时上下文：持有配置、Skill 注册表、类目 / 平台配置与各通道客户端，并决定真实 / 演示模式。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.engine.config_loader import load_categories, load_platforms
from app.engine.skill_loader import SkillRegistry
from app.providers.image import RightAPIImageClient
from app.providers.llm import RightAPILLM
from app.providers.video import (
    DemoVideoProvider,
    FalAIVideoProvider,
    ModalMiniMaxH3Provider,
    VideoProvider,
)
from app.schemas.config import CategoryConfig, PlatformConfig
from app.settings import Settings
from app.storage.project_store import ProjectStore


@dataclass
class EngineRuntime:
    settings: Settings
    skills: SkillRegistry = field(default_factory=SkillRegistry)
    categories: dict[str, CategoryConfig] = field(default_factory=load_categories)
    platforms: dict[str, PlatformConfig] = field(default_factory=load_platforms)
    # 界面「本地演示测试模式」开关（优先级高于 .env）
    force_demo: bool = False
    # 演示模式的人为延迟，便于在界面上看到日志滚动；测试中设为 0
    demo_delay: float = 0.35
    store: ProjectStore | None = None
    # 测试注入：替换真实客户端
    llm_override: Any = None
    image_client_override: Any = None

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = ProjectStore(self.settings.workspace_dir)

    def update_settings(self, settings: Settings) -> None:
        self.settings = settings

    # ---------- 模式判定 ----------

    def is_live(self, channel: str) -> bool:
        return not self.force_demo and self.settings.channel_live(channel)

    def mode_summary(self) -> str:
        def tag(ch: str) -> str:
            return "🟢 真实 API" if self.is_live(ch) else "🟡 演示模式"

        return (
            f"LLM：{tag('llm')}　｜　画图（GPT-Image + Nano-Banana）：{tag('image')}　｜　"
            f"Modal H3：{tag('modal_h3')}"
        )

    # ---------- 客户端 ----------

    def llm(self) -> RightAPILLM:
        if self.llm_override is not None:
            return self.llm_override
        s = self.settings
        return RightAPILLM(s.rightapi_base_url, s.llm_key, s.llm_model, s.llm_api_format, s.llm_timeout)

    def image_client(self) -> RightAPIImageClient:
        if self.image_client_override is not None:
            return self.image_client_override
        s = self.settings
        return RightAPIImageClient(base_url=s.rightapi_base_url, timeout=s.image_timeout,
                                   poll_interval=s.image_poll_interval)

    def video_provider(self, provider_id: str = "modal_minimax_h3") -> VideoProvider:
        """返回视频后端（界面固定使用 Modal MiniMax H3）；未登录 Modal 或演示模式时自动换成本地演示后端。"""
        s = self.settings
        if provider_id == "fal_ai":
            if self.is_live("fal_ai"):
                return FalAIVideoProvider(s.fal_key, s.fal_video_model, s.video_timeout)
            return DemoVideoProvider(step_delay=self.demo_delay)
        if provider_id == "modal_minimax_h3" and self.is_live("modal_h3"):
            return ModalMiniMaxH3Provider(s.modal_h3_app, s.modal_h3_class, s.modal_h3_steps, s.modal_h3_timeout)
        return DemoVideoProvider(step_delay=self.demo_delay)

    # ---------- 配置 ----------

    def category(self, category_id: str) -> CategoryConfig:
        return self.categories[category_id]

    def platform(self, platform_id: str) -> PlatformConfig:
        return self.platforms[platform_id]
