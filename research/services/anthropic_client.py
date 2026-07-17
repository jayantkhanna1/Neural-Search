"""Thin wrapper around the Anthropic SDK.

Centralizes client construction, model selection, retry behavior and
structured (JSON-schema constrained) requests so the rest of the codebase
never touches the SDK directly.
"""

import json
import logging
from functools import lru_cache

import anthropic
from django.conf import settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when a model call fails after retries or returns unusable output."""


@lru_cache(maxsize=1)
def get_client() -> anthropic.Anthropic:
    kwargs = {"max_retries": 4}
    if settings.ANTHROPIC_API_KEY:
        kwargs["api_key"] = settings.ANTHROPIC_API_KEY
    return anthropic.Anthropic(**kwargs)


def complete(
    *,
    system: str,
    user_content: str,
    max_tokens: int = 1024,
    json_schema: dict | None = None,
) -> str:
    """Run a single-turn completion on the configured Haiku model.

    When ``json_schema`` is given, the response is constrained via structured
    outputs and the raw JSON string is returned.
    """
    params = {
        "model": settings.ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
    }
    if json_schema is not None:
        params["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}

    try:
        response = get_client().messages.create(**params)
    except anthropic.RateLimitError as exc:
        logger.warning("Anthropic rate limit hit: %s", exc)
        raise LLMError("Rate limited by the Anthropic API") from exc
    except anthropic.APIStatusError as exc:
        logger.error("Anthropic API error %s: %s", exc.status_code, exc.message)
        raise LLMError(f"Anthropic API error ({exc.status_code})") from exc
    except anthropic.APIConnectionError as exc:
        logger.error("Anthropic connection error: %s", exc)
        raise LLMError("Could not reach the Anthropic API") from exc

    if response.stop_reason == "refusal":
        raise LLMError("The model declined to answer this request")

    text = "".join(block.text for block in response.content if block.type == "text")
    if not text.strip():
        raise LLMError("Empty response from the model")
    return text


def complete_json(
    *,
    system: str,
    user_content: str,
    json_schema: dict,
    max_tokens: int = 1024,
):
    """Structured-output completion, parsed into Python objects."""
    raw = complete(
        system=system,
        user_content=user_content,
        max_tokens=max_tokens,
        json_schema=json_schema,
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Model returned invalid JSON despite schema: %.200s", raw)
        raise LLMError("Model returned invalid JSON") from exc


def chat(
    *,
    system: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int = 1500,
):
    """Multi-turn chat completion; returns the full Message object.

    Used by the RAG chat flow, which needs access to tool_use blocks.
    """
    params = {
        "model": settings.ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if tools:
        params["tools"] = tools
    try:
        return get_client().messages.create(**params)
    except anthropic.RateLimitError as exc:
        raise LLMError("Rate limited by the Anthropic API") from exc
    except anthropic.APIStatusError as exc:
        logger.error("Anthropic API error %s: %s", exc.status_code, exc.message)
        raise LLMError(f"Anthropic API error ({exc.status_code})") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError("Could not reach the Anthropic API") from exc
