"""从 .env / 环境变量读取运行配置。Key 只在这里读取，其他模块不得直接访问 os.environ。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv, set_key

from app.engine.config_loader import PROJECT_ROOT

ENV_FILE = PROJECT_ROOT / ".env"

# 界面「API 配置」面板允许写回 .env 的键
EDITABLE_ENV_KEYS = (
    "RIGHTAPI_LLM_KEY",
    "RIGHTAPI_OPENAI_IMAGE_KEY",
    "RIGHTAPI_GEMINI_IMAGE_KEY",
    "MODAL_H3_ENDPOINT_URL",
    "COMFYUI_URL",
    "RIGHTAPI_LLM_API_FORMAT",
    "DEMO_MODE",
)


def _list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    rightapi_base_url: str = "https://www.rightapi.ai"
    llm_key: str = field(default="", repr=False)
    llm_model: str = "claude-opus-5-5"
    # auto：先试 /v1/messages（Anthropic 原生），404/405 时回退 /v1/chat/completions
    llm_api_format: str = "auto"
    llm_timeout: float = 180
    openai_image_key: str = field(default="", repr=False)
    openai_image_model: str = "gpt-image-2"
    openai_image_models: list[str] = field(
        default_factory=lambda: ["gpt-image-2.5-flare", "gpt-image-2.5-sunburst", "gpt-image-2"]
    )
    gemini_image_key: str = field(default="", repr=False)
    gemini_image_model: str = "gemini-3-pro-image-preview"
    gemini_image_models: list[str] = field(
        default_factory=lambda: ["gemini-3-pro-image-preview", "gemini-nano-banana-2.1"]
    )
    gemini_async: bool = False
    gemini_task_url_template: str = ""
    image_timeout: float = 300
    default_video_provider: str = "modal_minimax_h3"
    video_timeout: float = 600
    modal_h3_url: str = ""
    modal_h3_warmup_url: str = ""
    modal_h3_token: str = field(default="", repr=False)
    comfyui_url: str = ""
    fal_key: str = field(default="", repr=False)
    fal_video_model: str = ""
    # 强制演示模式：即使填了 Key 也不调用真实接口
    demo_mode: bool = False
    workspace_dir: Path = PROJECT_ROOT / "workspace"
    server_name: str = "127.0.0.1"
    server_port: int = 7860

    @property
    def image_models(self) -> list[str]:
        return [*self.openai_image_models, *self.gemini_image_models]

    def channel_live(self, channel: str) -> bool:
        """某个通道是否走真实接口（否则走零 Key 演示兜底）。"""
        if self.demo_mode:
            return False
        return bool(
            {
                "llm": self.llm_key,
                "openai_image": self.openai_image_key,
                "gemini_image": self.gemini_image_key,
                "modal_h3": self.modal_h3_url,
                "fal_ai": self.fal_key,
            }[channel]
        )


def load_settings(env_file: Path | None = None, override: bool = False) -> Settings:
    load_dotenv(env_file or ENV_FILE, override=override)
    e = os.environ.get
    workspace = Path(e("WORKSPACE_DIR", "workspace"))
    defaults = Settings()
    return Settings(
        rightapi_base_url=e("RIGHTAPI_BASE_URL", "https://www.rightapi.ai").rstrip("/"),
        llm_key=e("RIGHTAPI_LLM_KEY", ""),
        llm_model=e("RIGHTAPI_LLM_MODEL", "claude-opus-5-5"),
        llm_api_format=e("RIGHTAPI_LLM_API_FORMAT", "auto").strip().lower() or "auto",
        llm_timeout=float(e("RIGHTAPI_LLM_TIMEOUT_SECONDS", "180")),
        openai_image_key=e("RIGHTAPI_OPENAI_IMAGE_KEY", ""),
        openai_image_model=e("RIGHTAPI_OPENAI_IMAGE_MODEL", "gpt-image-2"),
        openai_image_models=_list(e("RIGHTAPI_OPENAI_IMAGE_MODELS", "")) or defaults.openai_image_models,
        gemini_image_key=e("RIGHTAPI_GEMINI_IMAGE_KEY", ""),
        gemini_image_model=e("RIGHTAPI_GEMINI_IMAGE_MODEL", "gemini-3-pro-image-preview"),
        gemini_image_models=_list(e("RIGHTAPI_GEMINI_IMAGE_MODELS", "")) or defaults.gemini_image_models,
        gemini_async=_bool(e("RIGHTAPI_GEMINI_ASYNC")),
        gemini_task_url_template=e("RIGHTAPI_GEMINI_TASK_URL_TEMPLATE", ""),
        image_timeout=float(e("RIGHTAPI_IMAGE_TIMEOUT_SECONDS", "300")),
        default_video_provider=e("DEFAULT_VIDEO_PROVIDER", "modal_minimax_h3"),
        video_timeout=float(e("VIDEO_TIMEOUT_SECONDS", "600")),
        # 兼容旧变量名 MODAL_MINIMAX_H3_URL
        modal_h3_url=e("MODAL_H3_ENDPOINT_URL", "") or e("MODAL_MINIMAX_H3_URL", ""),
        modal_h3_warmup_url=e("MODAL_H3_WARMUP_URL", "") or e("MODAL_MINIMAX_H3_WARMUP_URL", ""),
        modal_h3_token=e("MODAL_H3_TOKEN", "") or e("MODAL_MINIMAX_H3_TOKEN", ""),
        comfyui_url=e("COMFYUI_URL", ""),
        fal_key=e("FAL_KEY", ""),
        fal_video_model=e("FAL_VIDEO_MODEL", ""),
        demo_mode=_bool(e("DEMO_MODE")),
        workspace_dir=workspace if workspace.is_absolute() else PROJECT_ROOT / workspace,
        server_name=e("GRADIO_SERVER_NAME", "127.0.0.1"),
        server_port=int(e("GRADIO_SERVER_PORT", "7860")),
    )


def save_env(updates: dict[str, str], env_file: Path = ENV_FILE) -> Settings:
    """把界面填写的配置写回 .env，并立即生效。只允许写 EDITABLE_ENV_KEYS。"""
    env_file.touch(exist_ok=True)
    for key, value in updates.items():
        if key not in EDITABLE_ENV_KEYS:
            raise ValueError(f"不允许从界面修改的配置项：{key}")
        # 去掉换行，防止注入额外的环境变量行
        clean = "".join(str(value).split("\n")).replace("\r", "").strip()
        set_key(str(env_file), key, clean, quote_mode="never")
        os.environ[key] = clean
    return load_settings(env_file, override=True)
