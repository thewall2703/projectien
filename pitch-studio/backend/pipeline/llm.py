from __future__ import annotations

import base64
import json
import re
import time
from typing import Any

import httpx

from backend.config import settings

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMError(RuntimeError):
    pass


def _extract_json(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if not text:
        raise LLMError("Model returned an empty response")
    fenced = FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"Model did not return JSON: {text[:240]}")
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise LLMError("Model JSON was not an object")
    return parsed


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except json.JSONDecodeError:
        return f"{response.status_code} {response.reason_phrase}: {response.text[:500]}"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or error.get("code") or json.dumps(error)
        return f"{response.status_code} {message}"
    if isinstance(error, str):
        return f"{response.status_code} {error}"
    return f"{response.status_code} {response.reason_phrase}: {response.text[:500]}"


def _request_payload(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    reasoning: bool = True,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model or settings.openrouter_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "reasoning": {"enabled": reasoning},
    }
    if reasoning:
        # Claude 4.6 uses adaptive thinking. OpenRouter maps ``verbosity`` to
        # Anthropic's output_config.effort; medium balances script quality,
        # latency, and token cost.
        payload["verbosity"] = settings.openrouter_verbosity
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.openrouter_api_key.strip()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://127.0.0.1:5173",
        "X-Title": "Pitch Studio",
        "User-Agent": "pitch-studio/1.0",
    }


def _message_content(body: dict[str, Any]) -> str:
    message = (body.get("choices") or [{}])[0].get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)


def _post_openrouter(payload: dict[str, Any], timeout: float) -> str:
    if not settings.openrouter_api_key:
        raise LLMError("OPENROUTER_API_KEY is not set")
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(OPENROUTER_URL, headers=_headers(), json=payload)
            if response.status_code >= 500:
                last_error = LLMError(_error_message(response))
                time.sleep(1)
                continue
            if response.status_code >= 400:
                raise LLMError(_error_message(response))
            return _message_content(response.json())
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            time.sleep(1)
            if attempt == 1:
                raise LLMError(str(exc)) from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model did not return JSON: {exc}") from exc
    raise LLMError(str(last_error) if last_error else "OpenRouter request failed")


def _multimodal_payload(text: str, images: list[bytes]) -> dict[str, Any]:
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for data in images:
        encoded = base64.b64encode(data).decode("ascii")
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            }
        )
    return {
        "model": settings.openrouter_model,
        "messages": [{"role": "user", "content": parts}],
        "reasoning": {"enabled": True},
        "verbosity": settings.openrouter_verbosity,
    }


def chat_json(
    messages: list[dict[str, str]],
    timeout: float = 120.0,
    *,
    model: str | None = None,
    reasoning: bool = True,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    payload = _request_payload(
        messages,
        model=model,
        reasoning=reasoning,
        max_tokens=max_tokens,
    )
    return _extract_json(_post_openrouter(payload, timeout))


def chat_text_multimodal(text: str, images: list[bytes], timeout: float = 120.0) -> str:
    content = _post_openrouter(_multimodal_payload(text, images), timeout)
    if not (content or "").strip():
        raise LLMError("Model returned an empty response")
    return content.strip()
