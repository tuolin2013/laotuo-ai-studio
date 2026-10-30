"""LLM 提供商预设，参考 VS Code Cline 的「API Provider」：选提供商 → 填 API Key → 选模型，地址与接口协议自动带出。

- 每个提供商的 Key 分开保存（RightAPI 沿用 RIGHTAPI_LLM_KEY，其他为 LLM_KEY_<ID>），切换提供商不会丢掉另一个的 Key。
- 「使用自定义 Base URL」填在 RIGHTAPI_LLM_BASE_URL，覆盖预设地址；「OpenAI 兼容」没有预设地址，必须自己填。
- 下面的地址 2026-10-30 用无效 Key 实测过：DeepSeek / 智谱 / 百炼 / 方舟 / Moonshot 返回 401，Gemini 返回
  「API key not valid」，OpenRouter 的模型列表公开可读；Anthropic 官方在本机网络下返回 403（地区限制），
  OpenAI 官方连不上，这两个按官方文档填写。
"""

from __future__ import annotations

from dataclasses import dataclass

RIGHTAPI = "rightapi"
CUSTOM = "custom"
# 境外官方接口在国内网络下一般要走代理
_PROXY_NOTE = "国内网络一般要开代理：系统代理或环境变量 HTTPS_PROXY 都会生效。"


@dataclass(frozen=True)
class LLMProvider:
    id: str
    label: str
    # 预设地址：RightAPI 为空（用 RIGHTAPI_BASE_URL，与画图同一个），OpenAI 兼容为空（必须自己填）
    base_url: str
    # 默认接口协议（app/providers/llm.py 的 API_FORMATS 之一），「高级设置」里可以改
    protocol: str
    # 默认模型；为空时要先「获取模型列表」或手动输入
    default_model: str = ""
    hint: str = ""

    @property
    def name(self) -> str:
        """短名（状态栏、诊断里用）：去掉括号里的说明。"""
        return self.label.split("（")[0]

    @property
    def key_env(self) -> str:
        return "RIGHTAPI_LLM_KEY" if self.id == RIGHTAPI else f"LLM_KEY_{self.id.upper()}"


PROVIDERS = (
    LLMProvider(RIGHTAPI, "RightAPI（推荐，与画图同一平台）", "", "auto", "claude-opus-5-5",
                "Key 绑定渠道（Claude / Gemini / DeepSeek / 国模…），只能调用该渠道下的模型。"
                "在 rightapi.ai「令牌管理」换了渠道后，点「🔄 获取可用模型」重新选模型。"),
    LLMProvider(CUSTOM, "OpenAI 兼容（其他中转站）", "", "auto",
                hint="填中转站给的 Base URL（一般以 /v1 结尾，地址里没有版本号时自动补 /v1）和 Key，"
                     "再点「🔄 获取可用模型」。接口协议默认按模型名自动选择。"),
    LLMProvider("anthropic", "Anthropic（Claude 官方）", "https://api.anthropic.com", "anthropic", "claude-opus-5-5",
                "Key 在 console.anthropic.com 创建。" + _PROXY_NOTE),
    LLMProvider("openai", "OpenAI 官方", "https://api.openai.com/v1", "openai",
                hint="Key 在 platform.openai.com 创建。" + _PROXY_NOTE),
    LLMProvider("gemini", "Google Gemini 官方", "https://generativelanguage.googleapis.com", "gemini",
                hint="Key 在 aistudio.google.com 创建，走 Gemini 原生接口。" + _PROXY_NOTE),
    LLMProvider("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "openai",
                hint="模型名带厂商前缀，如 anthropic/claude-…；Key 在 openrouter.ai/keys 创建。"),
    LLMProvider("deepseek", "DeepSeek 官方", "https://api.deepseek.com/v1", "openai",
                hint="Key 在 platform.deepseek.com 创建。"),
    LLMProvider("zhipu", "智谱 GLM（BigModel）", "https://open.bigmodel.cn/api/paas/v4", "openai",
                hint="Key 在 open.bigmodel.cn 创建。"),
    LLMProvider("qwen", "阿里云百炼（通义千问）", "https://dashscope.aliyuncs.com/compatible-mode/v1", "openai",
                hint="Key 在阿里云百炼控制台创建（OpenAI 兼容模式）。"),
    LLMProvider("doubao", "火山方舟（豆包）", "https://ark.cn-beijing.volces.com/api/v3", "openai",
                hint="模型填方舟控制台的模型名或推理接入点 ID（ep-…）。"),
    LLMProvider("moonshot", "Moonshot（Kimi）", "https://api.moonshot.cn/v1", "openai",
                hint="Key 在 platform.moonshot.cn 创建。"),
)
_BY_ID = {p.id: p for p in PROVIDERS}


def get_provider(provider_id: str | None) -> LLMProvider:
    """未知或留空 → RightAPI（旧版 .env 没有 RIGHTAPI_LLM_PROVIDER，行为与原来一样）。"""
    return _BY_ID.get((provider_id or "").strip().lower(), _BY_ID[RIGHTAPI])


def resolve_base_url(provider_id: str, custom_url: str, rightapi_base: str) -> str:
    """LLM 实际使用的地址：自定义 Base URL > 提供商预设 > RightAPI 的 RIGHTAPI_BASE_URL。OpenAI 兼容没填时为空。"""
    p = get_provider(provider_id)
    if custom_url:
        return custom_url
    if p.base_url:
        return p.base_url
    return rightapi_base if p.id == RIGHTAPI else ""
