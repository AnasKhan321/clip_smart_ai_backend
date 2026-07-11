"""Small provider switch for text-generation LLM calls.

Set LLM_PROVIDER=openrouter or LLM_PROVIDER=gemini.
OpenRouter remains the default so existing deployments keep working.
"""
from __future__ import annotations

import json
import os
from typing import Literal

import google.auth
import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from openai import OpenAI

Purpose = Literal["json", "generate", "stream", "synthesize"]


def provider_name() -> str:
    explicit = os.getenv("LLM_PROVIDER") or os.getenv("AI_PROVIDER")
    if explicit:
        return explicit.strip().lower()
    if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
        return "gemini"
    return "openrouter"


def model_for(purpose: Purpose, openrouter_default: str, gemini_default: str) -> str:
    provider = provider_name()
    if provider == "gemini":
        if purpose == "generate":
            return os.getenv("GEMINI_HOOK_GEN_MODEL", os.getenv("GEMINI_MODEL", gemini_default))
        return os.getenv("GEMINI_MODEL", gemini_default)
    return os.getenv("ANALYZER_MODEL" if purpose == "json" else "HOOK_GEN_MODEL", openrouter_default)


def generate_text(
    prompt: str,
    *,
    purpose: Purpose,
    openrouter_model: str,
    gemini_model: str,
    max_tokens: int,
    temperature: float | None = None,
    json_response: bool = False,
) -> str:
    provider = provider_name()
    model = model_for(purpose, openrouter_model, gemini_model)
    if provider == "gemini":
        if _gemini_auth_mode() == "vertex":
            return _generate_vertex_gemini(
                prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                json_response=json_response,
            )
        return _generate_gemini(
            prompt,
            model=model,
            purpose=purpose,
            max_tokens=max_tokens,
            temperature=temperature,
            json_response=json_response,
        )
    if provider in ("openrouter", "openoter"):
        return _generate_openrouter(
            prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    raise RuntimeError(f"Unsupported LLM_PROVIDER={provider!r}; use openrouter or gemini")


def _gemini_auth_mode() -> str:
    mode = os.getenv("GEMINI_AUTH_MODE", "").strip().lower()
    if mode:
        return mode
    if os.getenv("GOOGLE_CLOUD_PROJECT"):
        return "vertex"
    return "api_key"


def _generate_openrouter(
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    temperature: float | None,
) -> str:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not configured")

    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    response = client.chat.completions.create(**kwargs)
    return (response.choices[0].message.content or "").strip()


def _generate_gemini(
    prompt: str,
    *,
    model: str,
    purpose: Purpose,
    max_tokens: int,
    temperature: float | None,
    json_response: bool,
) -> str:
    api_key = (
        (os.getenv("GEMINI_HOOK_GEN_API_KEY") if purpose == "generate" else None)
        or os.getenv("GOOGLE_API_KEY")
        or os.getenv("GEMINI_API_KEY")
    )
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY or GEMINI_API_KEY not configured")

    generation_config: dict = {"maxOutputTokens": max_tokens}
    if provider_name() == "gemini":
        generation_config["thinkingConfig"] = {"thinkingBudget": 0}
    if temperature is not None:
        generation_config["temperature"] = temperature
    if json_response:
        generation_config["responseMimeType"] = "application/json"

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": generation_config,
    }

    with httpx.Client(timeout=180) as client:
        response = client.post(url, params={"key": api_key}, json=payload)
    if response.status_code >= 400:
        raise RuntimeError(f"Gemini API error {response.status_code}: {response.text[:500]}")

    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {data}")
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(str(part.get("text", "")) for part in parts).strip()
    if not text:
        finish_reason = candidates[0].get("finishReason")
        raise RuntimeError(f"Gemini returned empty text (finishReason={finish_reason}): {data}")
    return text


def _generate_vertex_gemini(
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    temperature: float | None,
    json_response: bool,
) -> str:
    project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "global").strip()
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required for GEMINI_AUTH_MODE=vertex")

    generation_config: dict = {"maxOutputTokens": max_tokens}
    generation_config["thinkingConfig"] = {"thinkingBudget": 0}
    if temperature is not None:
        generation_config["temperature"] = temperature
    if json_response:
        generation_config["responseMimeType"] = "application/json"

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": generation_config,
    }

    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    url = (
        f"https://{host}/v1/projects/{project}/locations/{location}"
        f"/publishers/google/models/{model}:generateContent"
    )

    api_key = (
        (os.getenv("GEMINI_HOOK_GEN_API_KEY") if model == os.getenv("GEMINI_HOOK_GEN_MODEL") else None)
        or os.getenv("GOOGLE_API_KEY")
        or os.getenv("GEMINI_API_KEY")
    )

    with httpx.Client(timeout=180) as client:
        if api_key:
            response = client.post(url, params={"key": api_key}, json=payload)
        else:
            credentials = _vertex_credentials()
            credentials.refresh(Request())
            headers = {"Authorization": f"Bearer {credentials.token}"}
            response = client.post(url, headers=headers, json=payload)
    if response.status_code >= 400:
        raise RuntimeError(f"Vertex Gemini API error {response.status_code}: {response.text[:500]}")

    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Vertex Gemini returned no candidates: {data}")
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(str(part.get("text", "")) for part in parts).strip()
    if not text:
        finish_reason = candidates[0].get("finishReason")
        raise RuntimeError(f"Vertex Gemini returned empty text (finishReason={finish_reason}): {data}")
    return text


def _vertex_credentials():
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        info = json.loads(raw)
        return service_account.Credentials.from_service_account_info(info, scopes=scopes)
    credentials, _project = google.auth.default(scopes=scopes)
    return credentials
