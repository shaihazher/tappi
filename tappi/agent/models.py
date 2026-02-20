"""Live model discovery — fetch available models from each provider's API.

Falls back to hardcoded defaults if the API call fails (no key yet, network error, etc.).
Results are cached for 10 minutes to avoid hammering endpoints.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError

from tappi.agent.config import get_provider_key, PROVIDERS

# Cache: {provider: (timestamp, [models])}
_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 600  # 10 minutes

# Hardcoded fallbacks (used when API is unreachable or no key)
_FALLBACKS: dict[str, list[dict]] = {
    "openrouter": [
        {"id": "anthropic/claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
        {"id": "anthropic/claude-opus-4-6", "name": "Claude Opus 4.6"},
        {"id": "anthropic/claude-haiku-4-5", "name": "Claude Haiku 4.5"},
        {"id": "anthropic/claude-sonnet-4-20250514", "name": "Claude Sonnet 4"},
        {"id": "anthropic/claude-opus-4-20250514", "name": "Claude Opus 4"},
        {"id": "openai/gpt-4o", "name": "GPT-4o"},
        {"id": "openai/o3-mini", "name": "o3-mini"},
        {"id": "google/gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
        {"id": "deepseek/deepseek-chat", "name": "DeepSeek Chat"},
    ],
    "anthropic": [
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
        {"id": "claude-opus-4-6", "name": "Claude Opus 4.6"},
        {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5"},
        {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4"},
        {"id": "claude-opus-4-20250514", "name": "Claude Opus 4"},
    ],
    "claude_max": [
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
        {"id": "claude-opus-4-6", "name": "Claude Opus 4.6"},
        {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5"},
        {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4"},
        {"id": "claude-opus-4-20250514", "name": "Claude Opus 4"},
    ],
    "openai": [
        {"id": "gpt-4o", "name": "GPT-4o"},
        {"id": "gpt-4o-mini", "name": "GPT-4o Mini"},
        {"id": "o3-mini", "name": "o3-mini"},
        {"id": "gpt-4-turbo", "name": "GPT-4 Turbo"},
    ],
    "bedrock": [
        # Cross-region inference profiles (recommended — auto-routes across regions)
        {"id": "bedrock/us.anthropic.claude-sonnet-4-6-v1:0", "name": "Claude Sonnet 4.6 (Cross-Region)"},
        {"id": "bedrock/us.anthropic.claude-opus-4-6-v1:0", "name": "Claude Opus 4.6 (Cross-Region)"},
        {"id": "bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0", "name": "Claude Sonnet 4 (Cross-Region)"},
        {"id": "bedrock/us.anthropic.claude-opus-4-20250514-v1:0", "name": "Claude Opus 4 (Cross-Region)"},
        {"id": "bedrock/us.anthropic.claude-haiku-4-5-20251212-v1:0", "name": "Claude Haiku 4.5 (Cross-Region)"},
        {"id": "bedrock/us.meta.llama3-1-405b-instruct-v1:0", "name": "Llama 3.1 405B (Cross-Region)"},
        {"id": "bedrock/us.meta.llama3-1-70b-instruct-v1:0", "name": "Llama 3.1 70B (Cross-Region)"},
        {"id": "bedrock/us.amazon.nova-pro-v1:0", "name": "Amazon Nova Pro (Cross-Region)"},
        {"id": "bedrock/us.amazon.nova-lite-v1:0", "name": "Amazon Nova Lite (Cross-Region)"},
        # Direct model IDs (single region — use if cross-region not enabled)
        {"id": "bedrock/anthropic.claude-sonnet-4-6-v1:0", "name": "Claude Sonnet 4.6 (Direct)"},
        {"id": "bedrock/anthropic.claude-opus-4-6-v1:0", "name": "Claude Opus 4.6 (Direct)"},
        {"id": "bedrock/anthropic.claude-sonnet-4-20250514-v1:0", "name": "Claude Sonnet 4 (Direct)"},
        {"id": "bedrock/anthropic.claude-opus-4-20250514-v1:0", "name": "Claude Opus 4 (Direct)"},
        {"id": "bedrock/anthropic.claude-haiku-4-5-20251212-v1:0", "name": "Claude Haiku 4.5 (Direct)"},
        {"id": "bedrock/amazon.nova-pro-v1:0", "name": "Amazon Nova Pro (Direct)"},
        {"id": "bedrock/amazon.nova-lite-v1:0", "name": "Amazon Nova Lite (Direct)"},
    ],
    "azure": [
        {"id": "azure/gpt-4o", "name": "GPT-4o (Azure)"},
        {"id": "azure/gpt-4o-mini", "name": "GPT-4o Mini (Azure)"},
        {"id": "azure/gpt-4-turbo", "name": "GPT-4 Turbo (Azure)"},
        {"id": "azure/o3-mini", "name": "o3-mini (Azure)"},
    ],
    "vertex": [
        {"id": "vertex_ai/gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
        {"id": "vertex_ai/gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
        {"id": "vertex_ai/gemini-2.0-flash", "name": "Gemini 2.0 Flash"},
        {"id": "vertex_ai/claude-sonnet-4-6@20250619", "name": "Claude Sonnet 4.6 (Vertex)"},
        {"id": "vertex_ai/claude-opus-4-6@20250619", "name": "Claude Opus 4.6 (Vertex)"},
        {"id": "vertex_ai/claude-sonnet-4@20250514", "name": "Claude Sonnet 4 (Vertex)"},
        {"id": "vertex_ai/claude-opus-4@20250514", "name": "Claude Opus 4 (Vertex)"},
    ],
}


def _fetch_json(url: str, headers: dict[str, str] | None = None, timeout: float = 10) -> Any:
    """Fetch JSON from a URL with optional headers."""
    req = Request(url)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    resp = urlopen(req, timeout=timeout)
    return json.loads(resp.read())


def _fetch_openrouter(api_key: str | None) -> list[dict]:
    """Fetch ALL models from OpenRouter. Returns the full catalog.

    Includes tool-use capability info from the API's supported_parameters.
    Models that support tool use are marked with supports_tool_use=True.
    """
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    data = _fetch_json("https://openrouter.ai/api/v1/models", headers or None)
    models = data.get("data", [])

    results = []
    for m in models:
        mid = m.get("id", "")
        name = m.get("name", mid)
        ctx = m.get("context_length", 0)
        pricing = m.get("pricing", {})
        prompt_cost = pricing.get("prompt", "0")

        # Check tool-use support from OpenRouter's API metadata
        supported_params = m.get("supported_parameters", [])
        supports_tools = "tools" in supported_params or "tool_choice" in supported_params

        # Meta-routers (openrouter/auto, openrouter/free) don't list
        # supported_parameters — mark them specially
        is_meta = mid.startswith("openrouter/")

        results.append({
            "id": mid,
            "name": name,
            "context": ctx,
            "cost": prompt_cost,
            "supports_tool_use": supports_tools,
            "is_meta_router": is_meta,
        })

    # Sort: anthropic first, then openai, then google, then rest alphabetically
    def sort_key(m):
        mid = m["id"]
        if mid.startswith("anthropic/"):
            return (0, mid)
        if mid.startswith("openai/"):
            return (1, mid)
        if mid.startswith("google/"):
            return (2, mid)
        if mid.startswith("meta-llama/"):
            return (3, mid)
        if mid.startswith("deepseek/"):
            return (4, mid)
        if mid.startswith("mistralai/"):
            return (5, mid)
        return (6, mid)

    results.sort(key=sort_key)
    return results


def _fetch_anthropic(api_key: str) -> list[dict]:
    """Fetch models from Anthropic's API."""
    data = _fetch_json(
        "https://api.anthropic.com/v1/models?limit=50",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    models = data.get("data", [])
    results = []
    for m in models:
        mid = m.get("id", "")
        name = m.get("display_name", mid)
        results.append({"id": mid, "name": name})

    # Sort newest first (longer IDs with dates tend to be newer)
    results.sort(key=lambda m: m["id"], reverse=True)
    return results


def _fetch_openai(api_key: str) -> list[dict]:
    """Fetch models from OpenAI's API."""
    data = _fetch_json(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    models = data.get("data", [])
    results = []
    for m in models:
        mid = m.get("id", "")
        # Filter to chat models only
        if any(mid.startswith(p) for p in [
            "gpt-4", "gpt-3.5", "o1", "o3", "chatgpt",
        ]):
            results.append({"id": mid, "name": mid})

    results.sort(key=lambda m: m["id"])
    return results


def _fetch_bedrock(aws_access_key: str | None, aws_secret_key: str | None, region: str | None) -> list[dict]:
    """Try to list Bedrock foundation models via boto3."""
    try:
        import boto3  # type: ignore
    except ImportError:
        return _FALLBACKS.get("bedrock", [])

    try:
        kwargs: dict[str, Any] = {}
        if region:
            kwargs["region_name"] = region
        if aws_access_key and aws_secret_key:
            kwargs["aws_access_key_id"] = aws_access_key
            kwargs["aws_secret_access_key"] = aws_secret_key

        client = boto3.client("bedrock", **kwargs)
        response = client.list_foundation_models()
        models = response.get("modelSummaries", [])

        results = []
        for m in models:
            mid = m.get("modelId", "")
            name = m.get("modelName", mid)
            provider = m.get("providerName", "")
            # Only include models that support on-demand inference
            inference_types = m.get("inferenceTypesSupported", [])
            if "ON_DEMAND" not in inference_types:
                continue
            # Only include text models (input/output modalities include TEXT)
            input_modalities = m.get("inputModalities", [])
            output_modalities = m.get("outputModalities", [])
            if "TEXT" not in input_modalities or "TEXT" not in output_modalities:
                continue

            results.append({
                "id": f"bedrock/{mid}",
                "name": f"{name} ({provider})" if provider else name,
            })

        # Sort by provider then model
        results.sort(key=lambda m: m["id"])
        return results if results else _FALLBACKS.get("bedrock", [])

    except Exception:
        return _FALLBACKS.get("bedrock", [])


def fetch_models(
    provider: str,
    api_key: str | None = None,
    extra: dict | None = None,
    tool_use_only: bool = False,
) -> list[dict]:
    """Fetch available models for a provider. Uses cache + fallbacks.

    Args:
        provider: Provider key (openrouter, anthropic, etc.)
        api_key: Optional API key override
        extra: Optional extra config (aws_region, aws_secret_key, etc.)
        tool_use_only: If True, filter to models that support tool use (OpenRouter only)

    Returns list of {"id": str, "name": str, ...} dicts.
    """
    extra = extra or {}

    # Check cache
    if provider in _cache:
        ts, cached = _cache[provider]
        if time.monotonic() - ts < _CACHE_TTL:
            return cached

    # Resolve key
    key = api_key or get_provider_key(provider)

    try:
        if provider == "openrouter":
            models = _fetch_openrouter(key)
        elif provider == "claude_max":
            # OAuth tokens (sk-ant-oat01-...) don't work with the models API.
            # Always use the curated fallback list for Claude Max.
            return _FALLBACKS.get("claude_max", [])
        elif provider == "anthropic":
            if not key:
                return _FALLBACKS.get(provider, [])
            models = _fetch_anthropic(key)
        elif provider == "openai":
            if not key:
                return _FALLBACKS.get(provider, [])
            models = _fetch_openai(key)
        elif provider == "bedrock":
            aws_access = extra.get("aws_access_key_id") or os.environ.get("AWS_ACCESS_KEY_ID")
            aws_secret = extra.get("aws_secret_access_key") or os.environ.get("AWS_SECRET_ACCESS_KEY")
            aws_region = extra.get("aws_region") or os.environ.get("AWS_REGION_NAME") or os.environ.get("AWS_DEFAULT_REGION")
            models = _fetch_bedrock(aws_access, aws_secret, aws_region)
        else:
            # Azure, Vertex — no simple list endpoint
            return _FALLBACKS.get(provider, [])

        if models:
            _cache[provider] = (time.monotonic(), models)

            # Filter for tool-use support if requested
            if tool_use_only and provider == "openrouter":
                models = [
                    m for m in models
                    if m.get("supports_tool_use") or m.get("is_meta_router")
                ]

            return models

    except (URLError, OSError, json.JSONDecodeError, KeyError, TypeError):
        pass

    # Fallback
    return _FALLBACKS.get(provider, [])
