"""从 .env / 环境变量读取运行配置。Key 只在这里读取，其他模块不得直接访问 os.environ。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from app.engine.config_loader import PROJECT_ROOT


def _list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    rightapi_base_url: str = "https://www.rightapi.ai"
    llm_key: str = field(default="", repr=False)
    llm_model: str = "claude-opus-5-5"
    llm_timeout: float = 180
    openai_image_key: str = field(default="", repr=False)
    openai_image_model: str = "gpt-image-2"
    openai_image_models: list[str] = field(default_factory=list)
    gemini_image_key: str = field(default="", repr=False)
    gemini_image_model: str = "gemini-3-pro-image-preview"
    gemini_image_models: list[str] = field(default_factory=list)
    gemini_async: bool = False
    gemini_task_url_template: str = ""
    image_timeout: float = 300
    default_video_provider: str = "manual_comfyui"
    video_timeout: float = 600
    modal_h3_url: str = ""
    modal_h3_warmup_url: str = ""
    modal_h3_token: str = field(default="", repr=False)
    comfyui_url: str = ""
    fal_key: str = field(default="", repr=False)
    fal_video_model: str = ""
    workspace_dir: Path = PROJECT_ROOT / "workspace"
    server_name: str = "127.0.0.1"
    server_port: int = 7860


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    e = os.environ.get
    workspace = Path(e("WORKSPACE_DIR", "workspace"))
    return Settings(
        rightapi_base_url=e("RIGHTAPI_BASE_URL", "https://www.rightapi.ai").rstrip("/"),
        llm_key=e("RIGHTAPI_LLM_KEY", ""),
        llm_model=e("RIGHTAPI_LLM_MODEL", "claude-opus-5-5"),
        llm_timeout=float(e("RIGHTAPI_LLM_TIMEOUT_SECONDS", "180")),
        openai_image_key=e("RIGHTAPI_OPENAI_IMAGE_KEY", ""),
        openai_image_model=e("RIGHTAPI_OPENAI_IMAGE_MODEL", "gpt-image-2"),
        openai_image_models=_list(
            e("RIGHTAPI_OPENAI_IMAGE_MODELS", "gpt-image-2,gpt-image-2.5-flare,gpt-image-2.5-sunburst")
        ),
        gemini_image_key=e("RIGHTAPI_GEMINI_IMAGE_KEY", ""),
        gemini_image_model=e("RIGHTAPI_GEMINI_IMAGE_MODEL", "gemini-3-pro-image-preview"),
        gemini_image_models=_list(
            e("RIGHTAPI_GEMINI_IMAGE_MODELS", "gemini-3-pro-image-preview,gemini-nano-banana-2.1")
        ),
        gemini_async=e("RIGHTAPI_GEMINI_ASYNC", "false").lower() in {"1", "true", "yes"},
        gemini_task_url_template=e("RIGHTAPI_GEMINI_TASK_URL_TEMPLATE", ""),
        image_timeout=float(e("RIGHTAPI_IMAGE_TIMEOUT_SECONDS", "300")),
        default_video_provider=e("DEFAULT_VIDEO_PROVIDER", "manual_comfyui"),
        video_timeout=float(e("VIDEO_TIMEOUT_SECONDS", "600")),
        modal_h3_url=e("MODAL_MINIMAX_H3_URL", ""),
        modal_h3_warmup_url=e("MODAL_MINIMAX_H3_WARMUP_URL", ""),
        modal_h3_token=e("MODAL_MINIMAX_H3_TOKEN", ""),
        comfyui_url=e("COMFYUI_URL", ""),
        fal_key=e("FAL_KEY", ""),
        fal_video_model=e("FAL_VIDEO_MODEL", ""),
        workspace_dir=workspace if workspace.is_absolute() else PROJECT_ROOT / workspace,
        server_name=e("GRADIO_SERVER_NAME", "127.0.0.1"),
        server_port=int(e("GRADIO_SERVER_PORT", "7860")),
    )
