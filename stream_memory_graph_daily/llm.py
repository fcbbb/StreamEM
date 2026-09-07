from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Protocol


class LLMUnavailable(RuntimeError):
    pass


class JsonLLM(Protocol):
    def complete(self, system_prompt: str, user_prompt: str) -> dict[str, Any]: ...


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = str(text).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("LLM output must be a JSON object")
    return value


def load_env(path: str | Path | None) -> None:
    if not path:
        return
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'\"")


class OpenAIJsonLLM:
    """Small OpenAI-compatible JSON client shared by all four LLM stages."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
        max_tokens: int = 4096,
        retries: int = 3,
        use_proxy: bool = True,
        env_file: str | Path | None = None,
    ) -> None:
        load_env(env_file)
        api_key = (
            api_key
            or os.getenv("DAILY_GRAPH_OPENAI_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("OPENROUTER_API_KEY")
        )
        if not api_key:
            raise LLMUnavailable(
                "no API key; set DAILY_GRAPH_OPENAI_API_KEY/OPENAI_API_KEY or pass --api-key"
            )
        try:
            import httpx
            from openai import OpenAI
        except ImportError as exc:
            raise LLMUnavailable("openai and httpx are required for LLM calls") from exc
        resolved_url = (
            base_url
            or os.getenv("DAILY_GRAPH_OPENAI_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://openrouter.ai/api/v1"
        ).rstrip("/")
        if resolved_url.endswith("/chat/completions"):
            resolved_url = resolved_url[: -len("/chat/completions")]
        http_client = httpx.Client(trust_env=use_proxy, follow_redirects=True)
        self.client = OpenAI(
            api_key=api_key,
            base_url=resolved_url,
            timeout=timeout,
            max_retries=0,
            http_client=http_client,
        )
        self.model = model
        self.max_tokens = max_tokens
        self.retries = retries

    def complete(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        last_error: Exception | None = None
        json_mode = True
        for attempt in range(1, self.retries + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.0,
                    "max_tokens": self.max_tokens,
                }
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                response = self.client.chat.completions.create(**kwargs)
                return parse_json_object(response.choices[0].message.content or "")
            except Exception as exc:
                last_error = exc
                if json_mode and "response_format" in str(exc).casefold():
                    json_mode = False
                    continue
                status_code = getattr(exc, "status_code", None)
                if status_code in {400, 401, 403}:
                    break
                if attempt < self.retries:
                    time.sleep(min(2 ** (attempt - 1), 8))
        raise RuntimeError(f"LLM JSON call failed: {last_error}") from last_error

