from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

from .config import PROJECT_ENV_FILE
from .data import load_dotenv
from .prompt import (
    NormalizedSegments,
    make_user_prompt,
    normalize_segments,
    parse_model_json,
)


RESPONSES_MODELS = {"gpt-5.6-luna"}


def build_openai_client(
    api_key: Optional[str],
    base_url: Optional[str],
    timeout: float,
    env_file: Optional[str] = None,
    use_proxy: bool = True,
):
    # Also load here so direct library users get the same project credentials
    # as the CLI. Explicit arguments still take precedence below.
    load_dotenv(PROJECT_ENV_FILE if env_file is None else Path(env_file), override=True)
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("the openai package is required for the run command") from exc
    # Match the credential precedence used by the existing StreamEM runners.
    key = (
        api_key
        or os.getenv("CUTTING_OPENAI_API_KEY")
        or os.getenv("GRAPH_WEEKLY_OPENAI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
    )
    if not key:
        raise RuntimeError("no API key; pass --api-key or set OPENAI_API_KEY/OPENROUTER_API_KEY")
    resolved_base_url = (
        base_url
        or os.getenv("CUTTING_OPENAI_BASE_URL")
        or os.getenv("GRAPH_WEEKLY_OPENAI_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or "https://openrouter.ai/api/v1"
    )
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("the httpx package is required for the OpenAI client") from exc
    # httpx honors HTTP_PROXY/HTTPS_PROXY/ALL_PROXY when trust_env is enabled.
    # The experiment uses the host proxy by default; --no-proxy opts out.
    http_client = httpx.Client(trust_env=use_proxy, follow_redirects=True)
    kwargs: dict[str, Any] = {
        "api_key": key,
        "timeout": timeout,
        "max_retries": 0,
        "http_client": http_client,
    }
    if resolved_base_url:
        kwargs["base_url"] = resolved_base_url
    return OpenAI(**kwargs)


def call_cutting_model(
    client: Any,
    units: list[dict[str, Any]],
    model: str,
    temperature: float,
    max_tokens: int,
    retries: int = 3,
    system_prompt: str | None = None,
    user_template: str | None = None,
) -> tuple[NormalizedSegments, str]:
    if not system_prompt:
        raise ValueError("system_prompt is required")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": make_user_prompt(units, user_template or "{{units_json}}")},
    ]
    last_error: Optional[Exception] = None
    attempts_used = 0
    response_format_enabled = True
    for attempt in range(1, retries + 1):
        attempts_used = attempt
        try:
            if model in RESPONSES_MODELS:
                # OpenCode Go exposes GPT 5.6 Luna at /responses.
                response = client.responses.create(
                    model=model,
                    input=messages,
                    max_output_tokens=max_tokens,
                )
                text = getattr(response, "output_text", "") or ""
            else:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if response_format_enabled:
                    kwargs["response_format"] = {"type": "json_object"}
                response = client.chat.completions.create(**kwargs)
                text = response.choices[0].message.content or ""
            return normalize_segments(parse_model_json(text), units), text
        except Exception as exc:
            last_error = exc
            if response_format_enabled and "response_format" in str(exc).lower():
                response_format_enabled = False
                continue
            status_code = getattr(exc, "status_code", None)
            message = str(exc).lower()
            # Authentication, quota, and unsupported-model errors are
            # deterministic; retrying them only wastes provider quota.
            if status_code in {400, 401, 403} or any(
                phrase in message
                for phrase in ("model is not supported", "insufficient_user_quota", "invalid api key")
            ):
                break
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(f"cutting call failed after {attempts_used} attempt(s): {last_error}") from last_error
