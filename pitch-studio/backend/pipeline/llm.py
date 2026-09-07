from __future__ import annotations

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


def chat_json(messages: list[dict[str, str]], timeout: float = 120.0) -> dict[str, Any]:
    if not settings.openrouter_api_key:
        raise LLMError("OPENROUTER_API_KEY is not set")
    payload = {
        "model": settings.openrouter_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key.strip()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://127.0.0.1:5173",
        "X-Title": "Pitch Studio",
        "User-Agent": "pitch-studio/1.0",
    }
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(OPENROUTER_URL, headers=headers, json=payload)
            if response.status_code >= 500:
                last_error = LLMError(_error_message(response))
                time.sleep(1)
                continue
            if response.status_code >= 400:
                raise LLMError(_error_message(response))
            body = response.json()
            message = (body.get("choices") or [{}])[0].get("message") or {}
            content = message.get("content") or ""
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            return _extract_json(content)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            time.sleep(1)
            if attempt == 1:
                raise LLMError(str(exc)) from exc
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model did not return JSON: {exc}") from exc
    raise LLMError(str(last_error) if last_error else "OpenRouter request failed")
