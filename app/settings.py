"""从 .env / 环境变量读取运行配置。Key 只在这里读取，其他模块不得直接访问 os.environ。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv, set_key

from app.engine.config_loader import PROJECT_ROOT
from app.providers.image_models import ALL_MODELS, CATALOG, get_model
from app.providers.llm import API_FORMATS as LLM_API_FORMATS
from app.providers.llm_providers import PROVIDERS as LLM_PROVIDERS
from app.providers.llm_providers import RIGHTAPI, get_provider, resolve_base_url

ENV_FILE = PROJECT_ROOT / ".env"
# 官方文档 https://docs.rightapi.ai/docs/rc_draw/ 的基础地址（不带 www 时任务查询接口不可达）
DEFAULT_BASE_URL = "https://www.rightapi.ai"
IMAGE_RESOLUTIONS = ("1K", "2K", "4K")
IMAGE_QUALITIES = ("auto", "low", "medium", "high", "xhigh", "max")

# 公共免 Key 在线演示（LAOTUO_PUBLIC_DEMO=1）的限额，见 app/engine/public_demo.py：
# 单条动效广告最长秒数；动效渲染排队上限（含正在渲染的 1 条，满了提示稍后再试）；运行产物保留秒数
PUBLIC_MOTION_MAX_SECONDS = 30
PUBLIC_QUEUE_MAX_SIZE = 5
PUBLIC_OUTPUT_TTL_SECONDS = 3600
# 整个事件队列的安全上限（只拦住堆积的长任务：打开一次页面就会同时排上二十多个短事件，几位访客同时打开
# 也不能被拦住，所以不能用 5）与单个上传文件上限
PUBLIC_EVENT_QUEUE_MAX_SIZE = 120
PUBLIC_MAX_UPLOAD_SIZE = "20mb"
# 公共演示的工作区放在 <WORKSPACE_DIR>/public_demo/：与本机真实项目分开，过期清理不会碰到真实项目
PUBLIC_WORKSPACE_SUBDIR = "public_demo"

# 界面「API 配置」面板允许写回 .env 的键
EDITABLE_ENV_KEYS = (
    "RIGHTAPI_LLM_PROVIDER",
    "RIGHTAPI_LLM_KEY",
    # 其他 LLM 提供商各自的 Key（LLM_KEY_ANTHROPIC、LLM_KEY_DEEPSEEK…），切换提供商不会丢
    *(p.key_env for p in LLM_PROVIDERS if p.id != RIGHTAPI),
    "RIGHTAPI_LLM_MODEL",
    "RIGHTAPI_LLM_BASE_URL",
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


# 误把完整接口地址粘进 LLM 地址时去掉的结尾（/chat/completions 要排在 /completions 前面）
_LLM_ENDPOINT_SUFFIXES = ("/chat/completions", "/completions", "/messages", "/responses", "/models")


def normalize_llm_base_url(url: str | None) -> str:
    """LLM 中转地址容错：补 https://、去掉结尾的 /，误粘的完整接口路径（/chat/completions、/models/…）
    只保留前面部分。版本号（/v1、/api/paas/v4…）保留，由 app/providers/llm.py 的 api_root() 处理。
    空值返回空串 = 沿用 RIGHTAPI_BASE_URL。"""
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    url = url.rstrip("/")
    if "/models/" in url:
        url = url[: url.index("/models/")]
    for suffix in _LLM_ENDPOINT_SUFFIXES:
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/")


@dataclass(frozen=True)
class Settings:
    rightapi_base_url: str = DEFAULT_BASE_URL
    # LLM 提供商（app/providers/llm_providers.py，参考 Cline 的 API Provider）：决定预设地址与默认协议
    llm_provider: str = RIGHTAPI
    # 当前提供商的 Key。RightAPI 的 Key 绑定渠道，模型必须是该渠道下的（界面「获取模型列表」列出）
    llm_key: str = field(default="", repr=False)
    # 所有提供商各自保存的 Key（界面切换提供商时带出）
    llm_keys: dict[str, str] = field(default_factory=dict, repr=False)
    llm_model: str = "claude-opus-5-5"
    # 自定义 Base URL：覆盖提供商的预设地址；「OpenAI 兼容」提供商必填
    llm_base_url: str = ""
    # auto：按模型名选协议，失败再换（见 app/providers/llm.py）；anthropic / openai / responses / gemini 固定协议
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
    # 公共免 Key 在线演示（LAOTUO_PUBLIC_DEMO=1）：锁定演示模式、不显示 API 配置，见 app/engine/public_demo.py
    public_demo: bool = False
    workspace_dir: Path = PROJECT_ROOT / "workspace"
    server_name: str = "127.0.0.1"
    server_port: int = 7860

    def channel_live(self, channel: str) -> bool:
        """某个通道是否走真实接口（否则走零 Key 演示兜底）。"""
        if self.demo_mode or self.public_demo:
            return False
        return bool(
            {
                "llm": self.llm_key and self.llm_model and self.llm_api_base,
                "image": self.image_key,
                "image_std": self.image_std_key,
                "modal_h3": bool(self.modal_h3_app and self.modal_h3_class) and _modal_logged_in(),
                "modal_voxcpm": bool(self.modal_voxcpm_app and self.modal_voxcpm_class) and _modal_logged_in(),
                "fal_ai": self.fal_key,
            }[channel]
        )


    @property
    def llm_api_base(self) -> str:
        """LLM 实际使用的地址：自定义 Base URL > 提供商预设地址 > RIGHTAPI_BASE_URL（RightAPI）。"""
        return resolve_base_url(self.llm_provider, self.llm_base_url, self.rightapi_base_url)

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
    provider = get_provider(e("RIGHTAPI_LLM_PROVIDER"))
    llm_keys = {p.id: e(p.key_env, "").strip() for p in LLM_PROVIDERS}
    settings = Settings(
        rightapi_base_url=normalize_base_url(e("RIGHTAPI_BASE_URL")),
        llm_provider=provider.id,
        llm_key=llm_keys[provider.id],
        llm_keys=llm_keys,
        llm_model=(e("RIGHTAPI_LLM_MODEL") or "").strip() or provider.default_model,
        llm_base_url=normalize_llm_base_url(e("RIGHTAPI_LLM_BASE_URL")),
        llm_api_format=f if (f := e("RIGHTAPI_LLM_API_FORMAT", "auto").strip().lower()) in LLM_API_FORMATS else "auto",
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
        public_demo=_bool(e("LAOTUO_PUBLIC_DEMO")),
        workspace_dir=workspace if workspace.is_absolute() else PROJECT_ROOT / workspace,
        server_name=e("GRADIO_SERVER_NAME", "127.0.0.1"),
        server_port=int(e("GRADIO_SERVER_PORT", "7860")),
    )
    if settings.public_demo:
        # 公共演示一律走演示模式；Key 清空，任何地方都拿不到（包括界面初始值与诊断）；
        # 工作区换到 public_demo/ 子目录，与本机真实项目分开
        settings = replace(settings, demo_mode=True, llm_key="", llm_keys={}, image_key="", image_std_key="",
                           fal_key="",
                           workspace_dir=settings.workspace_dir / PUBLIC_WORKSPACE_SUBDIR)
    return settings


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
