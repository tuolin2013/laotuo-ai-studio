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

ANTHROPIC_PATH = "/v1/messages"
OPENAI_PATH = "/v1/chat/completions"
ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> dict[str, Any]:
    """从模型回复中提取 JSON 对象，容忍外层 ```json 代码块。"""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise LLMError(f"模型输出不是合法 JSON：{e}") from e
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

    def _anthropic_request(self, system: str, user: str, max_tokens: int) -> tuple[str, dict, dict]:
        headers = {
            "x-api-key": self._api_key,
            "Authorization": f"Bearer {self._api_key}",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        return self.base_url + ANTHROPIC_PATH, headers, body

    def _openai_request(self, system: str, user: str, max_tokens: int) -> tuple[str, dict, dict]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
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

    async def _post(self, client: httpx.AsyncClient, fmt: str, system: str, user: str, max_tokens: int):
        build = self._anthropic_request if fmt == "anthropic" else self._openai_request
        url, headers, body = build(system, user, max_tokens)
        return await client.post(url, json=body, headers=headers)

    async def complete(self, system: str, user: str, max_tokens: int = 8000) -> str:
        if not self._api_key:
            raise LLMError("未配置 RIGHTAPI_LLM_KEY")
        order = [self.resolved_format] if self.resolved_format else ["anthropic", "openai"]
        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
            for i, fmt in enumerate(order):
                try:
                    resp = await self._post(client, fmt, system, user, max_tokens)
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

    async def complete_json(self, system: str, user: str, max_tokens: int = 8000) -> dict[str, Any]:
        return extract_json(await self.complete(system, user, max_tokens))
