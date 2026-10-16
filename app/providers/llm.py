"""RightAPI LLM 接口（Claude）。

假定 RightAPI 提供 OpenAI 兼容的 /v1/chat/completions；如实际网关路径不同，只需修改 CHAT_PATH。
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

CHAT_PATH = "/v1/chat/completions"


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
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 180):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.timeout = timeout

    async def complete(self, system: str, user: str, max_tokens: int = 8000) -> str:
        if not self._api_key:
            raise LLMError("未配置 RIGHTAPI_LLM_KEY")
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    self.base_url + CHAT_PATH,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise LLMError(f"LLM 请求失败：HTTP {e.response.status_code}") from e
            except httpx.HTTPError as e:
                raise LLMError(f"LLM 请求失败：{type(e).__name__}") from e
        try:
            return resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError("LLM 响应格式无法识别") from e

    async def complete_json(self, system: str, user: str, max_tokens: int = 8000) -> dict[str, Any]:
        return extract_json(await self.complete(system, user, max_tokens))
