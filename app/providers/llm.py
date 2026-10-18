"""RightAPI LLM 接口（Claude）。

同时兼容两种路径，由 RIGHTAPI_LLM_API_FORMAT 控制：
- anthropic：POST /v1/messages（Anthropic 原生格式）
- openai：   POST /v1/chat/completions（OpenAI 兼容格式）
- auto：     先试 anthropic，遇到 404/405 自动回退 openai，并记住可用的格式
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.providers.image import with_connect_retry

ANTHROPIC_PATH = "/v1/messages"
OPENAI_PATH = "/v1/chat/completions"
ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> dict[str, Any]:
    """从模型回复中提取 JSON 对象，容忍外层 ```json 代码块。"""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]

    # 尝试直接解析
    try:
        data = json.loads(candidate)
        if not isinstance(data, dict):
            raise LLMError("模型输出必须是 JSON 对象")
        return data
    except json.JSONDecodeError:
        pass  # 继续尝试修复

    # 自动修复 1：Claude 经常在 content 字段中忘记转义换行符和引号
    # 策略：找到 "content": " 开始，到下一个字段名或 } 结束的区域，把里面的文本正确转义
    def fix_content_field(match):
        # match.group(1) 是 content 字段的原始值（未转义的多行 Markdown）
        raw_content = match.group(1)
        # 把 Markdown 转义为合法的 JSON 字符串值
        escaped = json.dumps(raw_content, ensure_ascii=False)[1:-1]  # 去掉外层引号
        return f'"content": "{escaped}"'

    # 正则匹配 "content": "..." 直到下一个顶层字段（或对象结束）
    # 非贪婪匹配，遇到 ",\n  " 开头的字段名或 \n} 结束
    pattern = r'"content"\s*:\s*"(.*?)"(?=\s*(?:,\s*"\w+"\s*:|}))'
    fixed = re.sub(pattern, fix_content_field, candidate, flags=re.S)

    # 自动修复 2：数组或对象结尾缺少逗号的常见模式
    # 例如：} \n { → }, \n {  或  ] \n [ → ], \n [
    fixed = re.sub(r'(\}|\])\s*\n\s*(["\{\[])', r'\1,\n\2', fixed)

    # 自动修复 3：多余的尾随逗号（JSON 标准不允许，但 LLM 经常加）
    fixed = re.sub(r',(\s*[\}\]])', r'\1', fixed)

    try:
        data = json.loads(fixed)
    except json.JSONDecodeError as e:
        # 修复后仍然失败，输出前500字符用于调试
        preview = candidate[:500] + ("..." if len(candidate) > 500 else "")
        raise LLMError(f"模型输出不是合法 JSON：{e}\n预览：{preview}") from e
    if not isinstance(data, dict):
        raise LLMError("模型输出必须是 JSON 对象")
    return data


class RightAPILLM:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        api_format: str = "auto",
        timeout: float = 180,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if api_format not in {"auto", "anthropic", "openai"}:
            raise ValueError(f"不支持的 LLM 接口格式：{api_format}")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.api_format = api_format
        self.timeout = timeout
        self._transport = transport
        # auto 模式下探测到的可用格式
        self.resolved_format: str | None = None if api_format == "auto" else api_format

    @staticmethod
    def _anthropic_content(user: str, images: list[str] | None) -> str | list[dict]:
        if not images:
            return user
        blocks: list[dict] = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
                              for b in images]
        return [*blocks, {"type": "text", "text": user}]

    @staticmethod
    def _openai_content(user: str, images: list[str] | None) -> str | list[dict]:
        if not images:
            return user
        blocks: list[dict] = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}}
                              for b in images]
        return [*blocks, {"type": "text", "text": user}]

    def _anthropic_request(self, system: str, user: str, max_tokens: int,
                           images: list[str] | None = None) -> tuple[str, dict, dict]:
        headers = {
            "x-api-key": self._api_key,
            "Authorization": f"Bearer {self._api_key}",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": self._anthropic_content(user, images)}],
        }
        return self.base_url + ANTHROPIC_PATH, headers, body

    def _openai_request(self, system: str, user: str, max_tokens: int,
                        images: list[str] | None = None) -> tuple[str, dict, dict]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": self._openai_content(user, images)}],
        }
        return self.base_url + OPENAI_PATH, headers, body

    @staticmethod
    def _parse(fmt: str, body: dict[str, Any]) -> str:
        try:
            if fmt == "anthropic":
                return "".join(b.get("text", "") for b in body["content"] if b.get("type", "text") == "text")
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"LLM 响应格式无法识别（{fmt}）") from e

    async def _post(self, client: httpx.AsyncClient, fmt: str, system: str, user: str, max_tokens: int,
                    images: list[str] | None = None):
        build = self._anthropic_request if fmt == "anthropic" else self._openai_request
        url, headers, body = build(system, user, max_tokens, images)
        return await client.post(url, json=body, headers=headers)

    async def complete(self, system: str, user: str, max_tokens: int = 8000,
                       images: list[str] | None = None) -> str:
        """images：JPEG 纯 Base64 列表（多模态输入，按顺序放在文本之前）。"""
        if not self._api_key:
            raise LLMError("未配置 RIGHTAPI_LLM_KEY")
        order = [self.resolved_format] if self.resolved_format else ["anthropic", "openai"]
        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
            for i, fmt in enumerate(order):
                try:
                    resp = await with_connect_retry(
                        lambda fmt=fmt: self._post(client, fmt, system, user, max_tokens, images))
                except httpx.HTTPError as e:
                    raise LLMError(f"LLM 请求失败：{type(e).__name__}") from e
                if resp.status_code in (404, 405) and i + 1 < len(order):
                    continue  # auto 模式：该网关不支持此路径，换另一种
                if resp.status_code >= 400:
                    raise LLMError(f"LLM 请求失败：HTTP {resp.status_code}（{fmt} 格式）")
                try:
                    data = resp.json()
                except ValueError as e:
                    raise LLMError("LLM 响应不是 JSON") from e
                self.resolved_format = fmt
                return self._parse(fmt, data)
        raise LLMError("LLM 两种接口格式均不可用")  # pragma: no cover

    async def complete_json(self, system: str, user: str, max_tokens: int = 8000,
                            images: list[str] | None = None) -> dict[str, Any]:
        return extract_json(await self.complete(system, user, max_tokens, images))
