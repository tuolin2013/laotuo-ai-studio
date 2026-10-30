"""LLM 接口：RightAPI 的任一对话渠道，或任意兼容的中转站 / 官方接口。

RightAPI 的 Key 绑定渠道（Claude 官渠 / AwsQ、Gemini、Codex 外接、DeepSeek、国模…），一把 Key 只能调用所绑渠道的模型，
后台换了渠道，模型也要跟着换。list_models() 用 GET {根}/models 列出这把 Key 能调用的模型，供界面选择。

接口协议由 RIGHTAPI_LLM_API_FORMAT 控制：
- anthropic：POST {根}/messages（Anthropic 原生，Claude 渠道）
- openai：   POST {根}/chat/completions（OpenAI 兼容：绝大多数中转、国产模型、Codex 外接）
- responses：POST {根}/responses（OpenAI Responses；RightAPI 的 Grok 渠道只支持这一种）
- gemini：   POST {根}/models/{model}:streamGenerateContent?alt=sse（Gemini 原生；RightAPI 的 Gemini 渠道只支持这一种）
- auto：     按模型名排出尝试顺序（protocol_order），遇到 4xx 换下一种，并记住可用的协议
{根} 由 api_root() 决定：地址里已有版本号（/v1、/api/paas/v4、/v1beta/openai…）原样使用，
否则补 /v1（Gemini 补 /v1beta）。
地址由 Settings.llm_api_base 给出：RIGHTAPI_LLM_BASE_URL 留空时与 RIGHTAPI_BASE_URL 相同。
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.providers.image import with_connect_retry

ANTHROPIC_VERSION = "2023-06-01"
PROTOCOLS = ("anthropic", "openai", "responses", "gemini")
API_FORMATS = ("auto", *PROTOCOLS)
PROTOCOL_LABELS = {"anthropic": "Anthropic /messages", "openai": "OpenAI /chat/completions",
                   "responses": "OpenAI /responses", "gemini": "Gemini 原生 streamGenerateContent"}
# 地址里的版本号段：/v1、/v1beta、/v3、/v4…
_VERSION_SEG = re.compile(r"/v\d+(?:beta|alpha)?\d*(?=/|$)")
# 推理模型（DeepSeek-R1、部分国产模型）放在回答开头的思考过程
_THINK = re.compile(r"^\s*<think>.*?</think>\s*", re.S)
# 「模型不在这把 Key 的渠道里」的常见报错（RightAPI：model not found；new-api：无可用渠道；OpenAI：does not exist）
_MODEL_MISSING = ("model not found", "model_not_found", "does not exist", "no available channel", "无可用渠道",
                  "模型不存在")


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


def api_root(base_url: str, protocol: str) -> str:
    """接口根地址：地址里已有版本号时原样使用，否则补 /v1。
    Gemini 原生接口在 /v1beta 下（结尾填了 /v1 也改成 /v1beta）。"""
    base = base_url.rstrip("/")
    if protocol == "gemini":
        if base.endswith("/v1"):
            return base[:-3] + "/v1beta"
        return base if _VERSION_SEG.search(base) else base + "/v1beta"
    return base if _VERSION_SEG.search(base) else base + "/v1"


def _is_anthropic_official(base_url: str) -> bool:
    return httpx.URL(base_url).host == "api.anthropic.com"


def protocol_order(model: str) -> list[str]:
    """auto 模式的尝试顺序：先试最可能的协议，4xx 时再试下一种。"""
    name = model.lower()
    if "/" in name:  # OpenRouter 一类带厂商前缀的模型名（anthropic/claude-…）：OpenAI 兼容的聚合平台
        return ["openai", "anthropic"]
    if name.startswith("claude"):
        return ["anthropic", "openai"]
    if name.startswith("gemini"):
        return ["gemini", "openai"]
    if name.startswith("grok"):
        return ["responses", "openai"]
    if name.startswith(("gpt", "codex")):
        return ["openai", "responses"]
    return ["openai", "anthropic"]


def _error_detail(resp: httpx.Response) -> str:
    """错误应答里的说明文字；网关的 HTML 错误页不写进日志。"""
    try:
        body = resp.json()
    except ValueError:
        text = resp.text.strip()
        return "网关返回了 HTML 错误页" if text.startswith("<") else text[:200]
    if isinstance(body, list) and body:  # Gemini 流式接口出错时可能用数组包一层
        body = body[0]
    if not isinstance(body, dict):
        return str(body)[:200]
    err = body.get("error")
    if isinstance(err, dict):
        err = err.get("message") or err.get("code")
    return str(err or body.get("message") or "")[:300]


def _gemini_chunks(text: str) -> list[Any]:
    """streamGenerateContent?alt=sse 的应答：逐行 data: {...}；不走 SSE 的网关直接回 JSON（对象或数组）。"""
    text = text.strip()
    try:
        if text.startswith(("[", "{")):
            data = json.loads(text)
            return data if isinstance(data, list) else [data]
        return [json.loads(p) for line in text.splitlines()
                if line.startswith("data:") and (p := line[5:].strip()) and p != "[DONE]"]
    except ValueError as e:
        raise LLMError("LLM 响应格式无法识别（gemini）") from e


def _parse_gemini(text: str) -> str:
    chunks = [c for c in _gemini_chunks(text) if isinstance(c, dict)]
    if not chunks:
        raise LLMError("LLM 响应格式无法识别（gemini）")
    out: list[str] = []
    for c in chunks:
        if c.get("error"):
            err = c["error"]
            raise LLMError(f"Gemini 返回错误：{err.get('message') if isinstance(err, dict) else err}")
        if block := (c.get("promptFeedback") or {}).get("blockReason"):
            raise LLMError(f"内容被 Gemini 拦截（{block}）")
        cand = (c.get("candidates") or [{}])[0]
        for part in (cand.get("content") or {}).get("parts") or []:
            # thought 为真的是思考过程，不是回答
            if isinstance(part, dict) and not part.get("thought") and isinstance(part.get("text"), str):
                out.append(part["text"])
    return "".join(out)


def _parse(fmt: str, resp: httpx.Response) -> str:
    if fmt == "gemini":
        return _parse_gemini(resp.text)
    try:
        body = resp.json()
    except ValueError as e:
        raise LLMError(f"LLM 响应不是 JSON（{fmt}）") from e
    try:
        if fmt == "anthropic":
            return "".join(b.get("text", "") for b in body["content"] if b.get("type", "text") == "text")
        if fmt == "responses":
            if isinstance(body.get("output_text"), str):
                return body["output_text"]
            return "".join(c.get("text", "") for item in body["output"] if item.get("type") == "message"
                           for c in item.get("content") or [] if c.get("type") == "output_text")
        content = body["choices"][0]["message"].get("content")
        if isinstance(content, list):  # 少数网关把回答拆成多段
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return _THINK.sub("", content or "", count=1)
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise LLMError(f"LLM 响应格式无法识别（{fmt}）") from e


def _model_ids(resp: httpx.Response) -> list[str] | None:
    """模型列表应答 → 模型名。OpenAI / Anthropic：data[].id；Gemini：models[].name（去掉 models/ 前缀）。"""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    if isinstance(body.get("data"), list):
        ids = [m.get("id") for m in body["data"] if isinstance(m, dict)]
    elif isinstance(body.get("models"), list):
        # Google 官方会列出向量 / 语音等模型，只留能对话的（网关不给 supportedGenerationMethods 时全留）
        ids = [str(m.get("name", "")).removeprefix("models/") for m in body["models"]
               if isinstance(m, dict) and "generateContent" in m.get("supportedGenerationMethods", ["generateContent"])]
    else:
        return None
    return list(dict.fromkeys(i for i in ids if isinstance(i, str) and i))


async def list_models(base_url: str, api_key: str, api_format: str = "auto",
                      transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30) -> list[str]:
    """这把 Key 能调用的模型（GET {根}/models，不消耗额度）。RightAPI 只返回 Key 绑定渠道下的模型。"""
    if not api_key:
        raise LLMError("请先填写 LLM 密钥")
    styles = {"gemini": ["gemini"], "auto": ["openai", "gemini"]}.get(api_format, ["openai"])
    failures: list[str] = []
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for style in styles:
            url = api_root(base_url, style) + "/models"
            if style == "gemini":
                headers = {"x-goog-api-key": api_key}
            elif _is_anthropic_official(base_url):
                headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
            else:
                headers = {"Authorization": f"Bearer {api_key}", "x-api-key": api_key,
                           "anthropic-version": ANTHROPIC_VERSION}
            try:
                resp = await with_connect_retry(lambda url=url, headers=headers: client.get(url, headers=headers))
            except httpx.HTTPError as e:
                raise LLMError(f"无法连接 {url}（{type(e).__name__}）") from e
            if resp.status_code >= 400:
                failures.append(f"{url} HTTP {resp.status_code} {_error_detail(resp)}".strip())
                continue
            models = _model_ids(resp)
            if models is not None:
                return models
            failures.append(f"{url} 返回的格式无法识别")
    raise LLMError("获取模型列表失败：" + "；".join(failures))


class RightAPILLM:
    """LLM 客户端（类名沿用 RightAPI，base_url 可以是任意兼容的中转）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        api_format: str = "auto",
        timeout: float = 180,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if api_format not in API_FORMATS:
            raise ValueError(f"不支持的 LLM 接口格式：{api_format}")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model = model
        self.api_format = api_format
        self.timeout = timeout
        self._transport = transport
        # auto 模式下探测到的可用协议
        self.resolved_format: str | None = None if api_format == "auto" else api_format
        # OpenAI 新版推理模型不认 max_tokens、只认 max_completion_tokens：遇到一次后一直用它
        self._completion_tokens = False

    @staticmethod
    def _image_blocks(fmt: str, images: list[str]) -> list[dict]:
        if fmt == "anthropic":
            return [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
                    for b in images]
        if fmt == "openai":
            return [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in images]
        if fmt == "responses":
            return [{"type": "input_image", "image_url": f"data:image/jpeg;base64,{b}"} for b in images]
        return [{"inline_data": {"mime_type": "image/jpeg", "data": b}} for b in images]

    def _request(self, fmt: str, system: str, user: str, max_tokens: int,
                 images: list[str] | None = None) -> tuple[str, dict, dict]:
        root = api_root(self.base_url, fmt)
        blocks = self._image_blocks(fmt, images or [])
        if fmt == "anthropic":
            headers = {"x-api-key": self._api_key, "anthropic-version": ANTHROPIC_VERSION}
            if not _is_anthropic_official(self.base_url):
                # 中转站多数只认 Bearer；Anthropic 官方只认 x-api-key（Bearer 留给 OAuth 令牌）
                headers["Authorization"] = f"Bearer {self._api_key}"
            content = [*blocks, {"type": "text", "text": user}] if blocks else user
            body = {
                "model": self.model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": content}],
            }
            return root + "/messages", headers, body
        if fmt == "openai":
            content = [*blocks, {"type": "text", "text": user}] if blocks else user
            body = {
                "model": self.model,
                ("max_completion_tokens" if self._completion_tokens else "max_tokens"): max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            }
            return root + "/chat/completions", {"Authorization": f"Bearer {self._api_key}"}, body
        if fmt == "responses":
            body = {
                "model": self.model,
                "instructions": system,
                "max_output_tokens": max_tokens,
                "input": [{"role": "user", "content": [*blocks, {"type": "input_text", "text": user}]}],
                "store": False,
            }
            return root + "/responses", {"Authorization": f"Bearer {self._api_key}"}, body
        # Gemini 的思考过程也计入 maxOutputTokens，限得紧了只剩思考、没有回答，所以不设上限（用模型默认值）
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [*blocks, {"text": user}]}],
        }
        model = self.model.removeprefix("models/")
        return f"{root}/models/{model}:streamGenerateContent?alt=sse", {"x-goog-api-key": self._api_key}, body

    async def _send(self, client: httpx.AsyncClient, fmt: str, system: str, user: str, max_tokens: int,
                    images: list[str] | None = None) -> httpx.Response:
        url, headers, body = self._request(fmt, system, user, max_tokens, images)
        resp = await with_connect_retry(lambda: client.post(url, json=body, headers=headers))
        if (fmt == "openai" and resp.status_code == 400 and not self._completion_tokens
                and "max_completion_tokens" in resp.text):
            self._completion_tokens = True
            url, headers, body = self._request(fmt, system, user, max_tokens, images)
            resp = await with_connect_retry(lambda: client.post(url, json=body, headers=headers))
        return resp

    def _failure_message(self, failures: list[tuple[str, int, str]]) -> str:
        parts = [" ".join(x for x in (PROTOCOL_LABELS[f], f"HTTP {code}" if code >= 400 else "", msg) if x)
                 for f, code, msg in failures]
        text = f"LLM 请求失败（模型 {self.model}）：" + "；".join(parts)
        details = " ".join(msg.lower() for _, _, msg in failures)
        if any(k in details for k in _MODEL_MISSING):
            text += ("。这个模型不在当前 Key 绑定的渠道里：在「⚙️ API 密钥与接口配置」点「🔄 获取可用模型」，"
                     "重新选择后保存")
        elif any(code in (401, 403) for _, code, _ in failures):
            text += "。请检查 Key 是否有效、未过期，以及绑定的渠道里有没有这个模型"
        return text

    async def complete(self, system: str, user: str, max_tokens: int = 8000,
                       images: list[str] | None = None) -> str:
        """images：JPEG 纯 Base64 列表（多模态输入，按顺序放在文本之前）。"""
        if not self._api_key:
            raise LLMError("未配置 LLM 的 API Key")
        order = [self.resolved_format] if self.resolved_format else protocol_order(self.model)
        failures: list[tuple[str, int, str]] = []
        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
            for fmt in order:
                try:
                    resp = await self._send(client, fmt, system, user, max_tokens, images)
                except httpx.HTTPError as e:
                    raise LLMError(f"LLM 请求失败：{type(e).__name__}") from e
                if resp.status_code < 400:
                    try:
                        text = _parse(fmt, resp)
                    except LLMError as e:
                        # 有的网关对不支持的路径回 200 + 网页：auto 模式下换下一种协议
                        failures.append((fmt, resp.status_code, str(e)))
                        continue
                    self.resolved_format = fmt
                    return text
                failures.append((fmt, resp.status_code, _error_detail(resp)))
                # 5xx / 429：上游可能已经执行（已计费）或是额度 / 限流问题，换协议重发没有意义
                if resp.status_code >= 500 or resp.status_code == 429:
                    break
        raise LLMError(self._failure_message(failures))

    async def complete_json(self, system: str, user: str, max_tokens: int = 8000,
                            images: list[str] | None = None) -> dict[str, Any]:
        return extract_json(await self.complete(system, user, max_tokens, images))

