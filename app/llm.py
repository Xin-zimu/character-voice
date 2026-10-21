"""OpenAI 兼容接口的最小客户端。

只用标准库，不依赖 openai 包。
任何兼容 /chat/completions 的服务都能接：
  - 阿里百炼、硅基流动、DeepSeek、智谱、月之暗面
  - 本地 Ollama / vLLM / llama.cpp server
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


def chat(
    messages: list[dict],
    *,
    model: str | None = None,
    temperature: float = 0.85,
    max_tokens: int = 600,
    timeout: int = 90,
) -> str:
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key:
        raise LLMError(
            "没有找到 API key。请设置环境变量 OPENAI_API_KEY。\n"
            "  Windows: set OPENAI_API_KEY=sk-xxx\n"
            "  或临时: os.environ['OPENAI_API_KEY']='sk-xxx'"
        )

    base = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = model or os.getenv("LLM_MODEL") or "gpt-4o-mini"

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")[:500]
        raise LLMError(f"接口返回 {e.code}：{detail}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"连不上 {base}：{e.reason}") from e

    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"响应格式不认识：{json.dumps(body, ensure_ascii=False)[:400]}") from e
