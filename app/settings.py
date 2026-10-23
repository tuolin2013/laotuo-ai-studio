"""从 .env / 环境变量读取运行配置。Key 只在这里读取，其他模块不得直接访问 os.environ。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv, set_key

from app.engine.config_loader import PROJECT_ROOT
from app.providers.image_models import ALL_MODELS, CATALOG, get_model

ENV_FILE = PROJECT_ROOT / ".env"
# 官方文档 https://docs.rightapi.ai/docs/rc_draw/ 的基础地址（不带 www 时任务查询接口不可达）
DEFAULT_BASE_URL = "https://www.rightapi.ai"
IMAGE_RESOLUTIONS = ("1K", "2K", "4K")
IMAGE_QUALITIES = ("auto", "low", "medium", "high", "xhigh", "max")

# 界面「API 配置」面板允许写回 .env 的键
EDITABLE_ENV_KEYS = (
    "RIGHTAPI_LLM_KEY",
    "RIGHTAPI_IMAGE_KEY",
    "RIGHTAPI_IMAGE_STD_KEY",
    "RIGHTAPI_IMAGE_STD_BASE_URL",
    "RIGHTAPI_IMAGE_RESOLUTION",
    "RIGHTAPI_IMAGE_QUALITY",
    "RIGHTAPI_BASE_URL",
    "MODAL_H3_APP",
    "MODAL_H3_CLASS",
    "MODAL_VOXCPM_APP",
    "MODAL_VOXCPM_CLASS",
    "RIGHTAPI_LLM_API_FORMAT",
    "DEMO_MODE",
)


def _list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def normalize_base_url(url: str | None) -> str:
    """容错处理：补全 https://、去掉结尾的 / 以及误填的 /v1、/v1beta 后缀；
    裸域名 rightapi.ai 统一改为文档中的 www.rightapi.ai（裸域名下 /v1/tasks 不可达）。"""
    url = (url or "").strip() or DEFAULT_BASE_URL
    if "://" not in url:
        url = "https://" + url
    url = url.rstrip("/")
    for suffix in ("/v1beta", "/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    url = url.rstrip("/")
    scheme, _, host = url.partition("://")
    if host == "rightapi.ai":
        url = f"{scheme}://www.rightapi.ai"
    return url


@dataclass(frozen=True)
class Settings:
    rightapi_base_url: str = DEFAULT_BASE_URL
    llm_key: str = field(default="", repr=False)
    llm_model: str = "claude-opus-5-5"
    # auto：先试 /v1/messages（Anthropic 原生），404/405 时回退 /v1/chat/completions
    llm_api_format: str = "auto"
    llm_timeout: float = 180
    # 画图 Key：RightAPI 的 Key 绑定渠道，两个渠道各用一把；同一渠道内 GPT Image 与 Nano Banana 共用
    # 「image 高质量」渠道（生产默认）
    image_key: str = field(default="", repr=False)
    # 「image 普通」渠道
    image_std_key: str = field(default="", repr=False)
    # 普通渠道的 Base URL；留空 = 与 rightapi_base_url 相同
    image_std_base_url: str = ""
    # 界面可选的画图模型（必须在 app/providers/image_models.py 的目录中，协议与渠道由目录决定）
    image_models: list[str] = field(default_factory=lambda: list(ALL_MODELS))
    # 分辨率档位：GPT Image 换算为合法像素尺寸，Gemini 直接作为 imageSize
    image_resolution: str = "2K"
    # GPT Image 质量档；gpt-image-2 不支持 xhigh/max，会自动降为 high
    image_quality: str = "high"
    # 单个异步任务的最长等待（提交 + 轮询）
    image_timeout: float = 600
    image_poll_interval: float = 4.0
    # 上游繁忙（temporarily busy / 429 / 503）时自动重新提交的次数，0 = 不重试
    image_busy_retries: int = 3
    # 同时进行的画图任务数（高质量渠道不限并发，此处只为保护本机带宽）
    image_concurrency: int = 8
    default_video_provider: str = "modal_minimax_h3"
    video_timeout: float = 600
    # MiniMax H3：通过 Modal SDK 按「应用名 + 类名」调用（使用本机 Modal 登录，无需 URL / Key）
    modal_h3_app: str = "minimax-h3-diffusers-service"
    modal_h3_class: str = "H3BatchRunner"
    modal_h3_steps: int = 30
    modal_h3_timeout: float = 2400
    # VoxCPM2 配音：同样通过 Modal SDK 调用（modal/voxcpm_service.py），单段合成上限秒数含冷启动
    modal_voxcpm_app: str = "voxcpm2-tts-service"
    modal_voxcpm_class: str = "VoxCPM2Service"
    modal_voxcpm_timeout: float = 600
    fal_key: str = field(default="", repr=False)
    fal_video_model: str = ""
    # 剪映草稿文件夹：导出剪映草稿时直接写进这里（重新打开剪映即可在「本地草稿」看到）；
    # 未配置时自动查找本机剪映 / CapCut 的默认位置，都没有时为 None（只打包下载）
    jianying_draft_dir: Path | None = None
    # 强制演示模式：即使填了 Key 也不调用真实接口
    demo_mode: bool = False
    workspace_dir: Path = PROJECT_ROOT / "workspace"
    server_name: str = "127.0.0.1"
    server_port: int = 7860

    def channel_live(self, channel: str) -> bool:
        """某个通道是否走真实接口（否则走零 Key 演示兜底）。"""
        if self.demo_mode:
            return False
        return bool(
            {
                "llm": self.llm_key,
                "image": self.image_key,
                "image_std": self.image_std_key,
                "modal_h3": bool(self.modal_h3_app and self.modal_h3_class) and _modal_logged_in(),
                "modal_voxcpm": bool(self.modal_voxcpm_app and self.modal_voxcpm_class) and _modal_logged_in(),
                "fal_ai": self.fal_key,
            }[channel]
        )


    def image_base_url(self, model: str) -> str:
        """模型所属渠道的 Base URL：普通渠道可单独配置，未配置时与高质量渠道相同。"""
        if get_model(model).channel == "std" and self.image_std_base_url:
            return self.image_std_base_url
        return self.rightapi_base_url


def _modal_logged_in() -> bool:
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return True
    return (Path.home() / ".modal.toml").exists()


def default_jianying_dir() -> Path | None:
    """本机剪映专业版（或 CapCut）的默认草稿文件夹；没有安装时返回 None。"""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    for app in ("JianyingPro", "CapCut"):
        path = Path(base) / app / "User Data" / "Projects" / "com.lveditor.draft"
        if path.is_dir():
            return path
    return None


def _jianying_dir(value: str) -> Path | None:
    value = value.strip().strip('"')
    return Path(value) if value else default_jianying_dir()


def _resolution(value: str) -> str:
    v = (value or "").strip().upper()
    return v if v in IMAGE_RESOLUTIONS else "2K"


def _models(value: str) -> list[str]:
    """只保留目录中登记过协议的模型，未知模型直接忽略，避免调用错接口。"""
    picked = [m for m in _list(value) if m in CATALOG]
    return picked or list(ALL_MODELS)


def load_settings(env_file: Path | None = None, override: bool = False) -> Settings:
    load_dotenv(env_file or ENV_FILE, override=override)
    e = os.environ.get
    workspace = Path(e("WORKSPACE_DIR", "workspace"))
    return Settings(
        rightapi_base_url=normalize_base_url(e("RIGHTAPI_BASE_URL")),
        llm_key=e("RIGHTAPI_LLM_KEY", ""),
        llm_model=e("RIGHTAPI_LLM_MODEL", "claude-opus-5-5"),
        llm_api_format=e("RIGHTAPI_LLM_API_FORMAT", "auto").strip().lower() or "auto",
        llm_timeout=float(e("RIGHTAPI_LLM_TIMEOUT_SECONDS", "180")),
        # 兼容旧版的两个变量名（RIGHTAPI_OPENAI_IMAGE_KEY / RIGHTAPI_GEMINI_IMAGE_KEY）
        image_key=(e("RIGHTAPI_IMAGE_KEY", "") or e("RIGHTAPI_OPENAI_IMAGE_KEY", "")
                   or e("RIGHTAPI_GEMINI_IMAGE_KEY", "")),
        image_std_key=e("RIGHTAPI_IMAGE_STD_KEY", ""),
        image_std_base_url=normalize_base_url(u) if (u := e("RIGHTAPI_IMAGE_STD_BASE_URL", "").strip()) else "",
        image_models=_models(e("RIGHTAPI_IMAGE_MODELS", "")),
        image_resolution=_resolution(e("RIGHTAPI_IMAGE_RESOLUTION", "2K")),
        image_quality=q if (q := e("RIGHTAPI_IMAGE_QUALITY", "high").strip().lower()) in IMAGE_QUALITIES else "high",
        image_timeout=float(e("RIGHTAPI_IMAGE_TIMEOUT_SECONDS", "600")),
        image_poll_interval=float(e("RIGHTAPI_IMAGE_POLL_SECONDS", "4")),
        image_busy_retries=max(0, int(e("RIGHTAPI_IMAGE_BUSY_RETRIES", "3"))),
        image_concurrency=max(1, int(e("RIGHTAPI_IMAGE_CONCURRENCY", "8"))),
        default_video_provider=e("DEFAULT_VIDEO_PROVIDER", "modal_minimax_h3"),
        video_timeout=float(e("VIDEO_TIMEOUT_SECONDS", "600")),
        modal_h3_app=e("MODAL_H3_APP", "minimax-h3-diffusers-service").strip(),
        modal_h3_class=e("MODAL_H3_CLASS", "H3BatchRunner").strip(),
        modal_h3_steps=max(1, int(e("MODAL_H3_STEPS", "30"))),
        modal_h3_timeout=float(e("MODAL_H3_TIMEOUT_SECONDS", "2400")),
        modal_voxcpm_app=e("MODAL_VOXCPM_APP", "voxcpm2-tts-service").strip(),
        modal_voxcpm_class=e("MODAL_VOXCPM_CLASS", "VoxCPM2Service").strip(),
        modal_voxcpm_timeout=float(e("MODAL_VOXCPM_TIMEOUT_SECONDS", "600")),
        fal_key=e("FAL_KEY", ""),
        fal_video_model=e("FAL_VIDEO_MODEL", ""),
        jianying_draft_dir=_jianying_dir(e("JIANYING_DRAFT_DIR", "")),
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
