"""AI 引擎：异步 HTTP 客户端，支持 OpenAI 和 Anthropic 兼容格式。"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx
from loguru import logger


@dataclass
class TokenUsage:
    """Token 用量统计"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    request_count: int = 0

    def add(self, prompt: int, completion: int) -> None:
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += prompt + completion
        self.request_count += 1

    @property
    def summary(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "request_count": self.request_count,
        }


class AIEngine:
    """AI 引擎：支持 OpenAI 与 Anthropic Messages API。"""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str = "gpt-4",
        max_tokens: int = 500,
        temperature: float = 0.7,
        timeout: float = 30,
        max_retries: int = 3,
        api_style: str = "auto",
    ):
        self.api_style = self._resolve_api_style(api_url, api_style)
        self.api_url = self._normalize_api_url(api_url, self.api_style)
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries
        self.usage = TokenUsage()
        self._clients: dict[int, httpx.AsyncClient] = {}
        self._available = True

    @staticmethod
    def _resolve_api_style(api_url: str, configured: str = "auto") -> str:
        style = str(configured or "auto").strip().lower()
        if style in {"openai", "anthropic"}:
            return style
        return AIEngine._detect_api_style(api_url)

    @staticmethod
    def _normalize_api_url(api_url: str, api_style: str = "openai") -> str:
        """补全常见中转站的完整接口路径。"""
        url = api_url.strip().rstrip("/")
        if not url:
            return ""
        lowered = url.lower()
        if api_style == "anthropic":
            if lowered.endswith("/v1/messages"):
                return url
            if lowered.endswith("/v1"):
                return url + "/messages"
            if lowered.endswith("/messages"):
                return url
            return url + "/v1/messages"

        if lowered.endswith("/chat/completions"):
            return url
        if lowered.endswith("/v1"):
            return url + "/chat/completions"
        if lowered.endswith("/completions"):
            return url
        return url + "/v1/chat/completions"

    @staticmethod
    def _detect_api_style(api_url: str) -> str:
        """Infer OpenAI-compatible by default; Anthropic needs an explicit hint."""
        url = str(api_url or "").lower()
        if (
            "/v1/messages" in url
            or "api.anthropic.com" in url
            or "anthropic" in url
        ):
            return "anthropic"
        return "openai"

    @property
    def api_key_set(self) -> bool:
        return bool(str(self.api_key or "").strip())

    async def reconfigure(
        self,
        api_url: str,
        api_key: str,
        model: str,
        api_style: str = "auto",
    ) -> None:
        """Apply WebUI settings without requiring a process restart."""
        await self.close()
        self.api_style = self._resolve_api_style(api_url, api_style)
        self.api_url = self._normalize_api_url(api_url, self.api_style)
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip() or self.model
        self._available = True
        logger.info(
            "AI 配置已热更新: "
            f"style={self.api_style}, model={self.model}, url={self.api_url}"
        )

    def _get_client_for_current_loop(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        key = id(loop)
        client = self._clients.get(key)
        if client is not None and not client.is_closed:
            return client

        headers = {"Content-Type": "application/json"}
        if self.api_style == "anthropic":
            headers.update({
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            })
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"

        client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers=headers,
            verify=False,
        )
        self._clients[key] = client
        return client

    async def _get_client(self) -> httpx.AsyncClient:
        return self._get_client_for_current_loop()

    def _build_payload(
        self,
        messages: list[dict],
        system_prompt: str,
        temperature: float | None,
        stream: bool = False,
        max_tokens: int | None = None,
    ) -> dict:
        payload = {
            "model": self.model,
            "max_tokens": int(max_tokens or self.max_tokens),
            "temperature": temperature if temperature is not None else self.temperature,
        }

        if self.api_style == "anthropic":
            payload["messages"] = messages
            if system_prompt:
                payload["system"] = system_prompt
        else:
            full_messages = []
            if system_prompt:
                full_messages.append({"role": "system", "content": system_prompt})
            full_messages.extend(messages)
            payload["messages"] = full_messages

        if stream:
            payload["stream"] = True
        return payload

    async def chat(
        self,
        messages: list[dict],
        system_prompt: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """发送聊天请求，返回回复文本"""
        if not self._available:
            logger.warning("AI 引擎不可用")
            return ""

        payload = self._build_payload(
            messages,
            system_prompt,
            temperature,
            max_tokens=max_tokens,
        )

        # 重试循环
        last_error = None
        for attempt in range(self.max_retries):
            try:
                client = await self._get_client()
                logger.debug(f"AI 请求: url={self.api_url}, model={self.model}, attempt={attempt + 1}")
                response = await client.post(self.api_url, json=payload)
                response.raise_for_status()

                data = response.json()
                if self.api_style == "anthropic":
                    content = "\n".join(
                        block.get("text", "")
                        for block in data.get("content", [])
                        if block.get("type") == "text"
                    ).strip()
                    usage = data.get("usage", {})
                    self.usage.add(
                        prompt=usage.get("input_tokens", 0),
                        completion=usage.get("output_tokens", 0),
                    )
                else:
                    content = data["choices"][0]["message"]["content"].strip()
                    usage = data.get("usage", {})
                    self.usage.add(
                        prompt=usage.get("prompt_tokens", 0),
                        completion=usage.get("completion_tokens", 0),
                    )

                logger.debug(f"AI 回复成功 (attempt {attempt + 1})")
                return content

            except httpx.TimeoutException:
                last_error = f"请求超时 ({self.timeout}s)"
                logger.warning(f"AI 请求超时 (attempt {attempt + 1}/{self.max_retries})")
            except httpx.HTTPStatusError as e:
                last_error = f"HTTP {e.response.status_code}"
                logger.warning(f"AI 请求失败: {last_error} (attempt {attempt + 1}/{self.max_retries})")
                if e.response.status_code == 401:
                    logger.error("API Key 无效")
                    return ""
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                logger.warning(f"AI 请求异常: {last_error} (attempt {attempt + 1}/{self.max_retries})")

            # 指数退避
            if attempt < self.max_retries - 1:
                wait = 2 ** attempt
                await asyncio.sleep(wait)

        logger.error(f"AI 请求最终失败: {last_error}")
        return ""

    async def chat_stream(
        self,
        messages: list[dict],
        system_prompt: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """流式聊天请求，逐块返回"""
        payload = self._build_payload(
            messages,
            system_prompt,
            temperature,
            stream=True,
            max_tokens=max_tokens,
        )

        try:
            client = await self._get_client()
            async with client.stream("POST", self.api_url, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        if self.api_style == "anthropic":
                            delta = data.get("delta", {})
                            content = (
                                delta.get("text", "")
                                if delta.get("type") == "text_delta"
                                else ""
                            )
                        else:
                            delta = data["choices"][0].get("delta", {})
                            content = delta.get("content", "")
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
        except Exception as e:
            logger.error(f"流式请求失败: {e}")

    async def test_connection(self) -> tuple[bool, str]:
        """测试 API 连接"""
        try:
            response = await self.chat(
                messages=[{"role": "user", "content": "Hi, respond with 'OK' only."}],
            )
            if response:
                return True, f"连接成功: {response[:50]}"
            return False, "未收到回复"
        except Exception as e:
            return False, f"连接失败: {e}"

    async def close(self) -> None:
        clients = list(self._clients.items())
        self._clients.clear()
        current_loop_id = id(asyncio.get_running_loop())
        for loop_id, client in clients:
            if loop_id != current_loop_id:
                continue
            if not client.is_closed:
                await client.aclose()

    @property
    def is_available(self) -> bool:
        return self._available

    @is_available.setter
    def is_available(self, value: bool) -> None:
        self._available = value
        logger.info(f"AI 引擎 {'可用' if value else '不可用'}")
