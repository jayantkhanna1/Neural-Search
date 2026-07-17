"""Thin wrapper around the Anthropic SDK.

Centralizes client construction, model selection, retry behavior, structured
(JSON-schema constrained) requests, prompt-cache-aware message calls, per-task
token budgeting, and Message Batches — so the rest of the codebase never
touches the SDK directly.
"""

import contextlib
import contextvars
import json
import logging
import time
from functools import lru_cache

import anthropic
from django.conf import settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when a model call fails after retries or returns unusable output."""


class BudgetExceededError(LLMError):
    """Raised when a research task's LLM token budget is exhausted."""


# ------------------------------------------------------------- token budget
class UsageTracker:
    """Accumulates token usage across every LLM call made in a context."""

    def __init__(self, limit: int | None = None):
        self.limit = limit
        self.total_tokens = 0

    def add(self, usage) -> None:
        if usage is None:
            return
        self.total_tokens += (
            (getattr(usage, "input_tokens", 0) or 0)
            + (getattr(usage, "output_tokens", 0) or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        )

    @property
    def exhausted(self) -> bool:
        return self.limit is not None and self.total_tokens >= self.limit


_usage_ctx: contextvars.ContextVar[UsageTracker | None] = contextvars.ContextVar(
    "llm_usage_tracker", default=None
)


@contextlib.contextmanager
def track_usage(limit: int | None = None):
    """Track (and optionally cap) token usage for all LLM calls in this context."""
    tracker = UsageTracker(limit)
    token = _usage_ctx.set(tracker)
    try:
        yield tracker
    finally:
        _usage_ctx.reset(token)


def _check_budget():
    tracker = _usage_ctx.get()
    if tracker is not None and tracker.exhausted:
        raise BudgetExceededError(
            f"LLM token budget exhausted ({tracker.total_tokens}/{tracker.limit} tokens)"
        )


def _record_usage(response):
    tracker = _usage_ctx.get()
    if tracker is not None:
        tracker.add(getattr(response, "usage", None))


# ------------------------------------------------------------------- client
@lru_cache(maxsize=1)
def get_client() -> anthropic.Anthropic:
    kwargs = {"max_retries": 4}
    if settings.ANTHROPIC_API_KEY:
        kwargs["api_key"] = settings.ANTHROPIC_API_KEY
    return anthropic.Anthropic(**kwargs)


def _create(params: dict):
    _check_budget()
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
    _record_usage(response)
    return response


def complete_messages(
    *,
    system,
    messages: list[dict],
    max_tokens: int = 1024,
    json_schema: dict | None = None,
) -> str:
    """Completion over explicit messages (supports cache_control blocks).

    ``system`` may be a string or a list of system content blocks.
    """
    params = {
        "model": settings.ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if json_schema is not None:
        params["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}

    response = _create(params)
    if response.stop_reason == "refusal":
        raise LLMError("The model declined to answer this request")
    text = "".join(block.text for block in response.content if block.type == "text")
    if not text.strip():
        raise LLMError("Empty response from the model")
    return text


def complete(
    *,
    system: str,
    user_content: str,
    max_tokens: int = 1024,
    json_schema: dict | None = None,
) -> str:
    """Single-turn completion on the configured Haiku model."""
    return complete_messages(
        system=system,
        messages=[{"role": "user", "content": user_content}],
        max_tokens=max_tokens,
        json_schema=json_schema,
    )


def complete_json(
    *,
    system: str,
    user_content: str | list,
    json_schema: dict,
    max_tokens: int = 1024,
):
    """Structured-output completion, parsed into Python objects."""
    messages = [{"role": "user", "content": user_content}]
    raw = complete_messages(
        system=system,
        messages=messages,
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

    Used by the RAG chat flow, which needs access to tool_use and citation blocks.
    """
    params = {
        "model": settings.ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if tools:
        params["tools"] = tools
    return _create(params)


# ------------------------------------------------------------ message batches
def run_batch(requests_params: list[tuple[str, dict]], poll_seconds: int = 15,
              timeout_seconds: int = 3000) -> dict[str, str]:
    """Run a Message Batch (50% cost) and return {custom_id: text}.

    ``requests_params`` is a list of (custom_id, messages.create params).
    Failed or refused entries are simply absent from the result so callers
    can fall back per-item.
    """
    client = get_client()
    batch = client.messages.batches.create(
        requests=[{"custom_id": cid, "params": params} for cid, params in requests_params]
    )
    logger.info("Submitted message batch %s with %d requests", batch.id, len(requests_params))

    deadline = time.monotonic() + timeout_seconds
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        if time.monotonic() > deadline:
            logger.warning("Batch %s timed out; falling back to sync calls", batch.id)
            try:
                client.messages.batches.cancel(batch.id)
            except anthropic.APIError:
                pass
            return {}
        time.sleep(poll_seconds)

    results: dict[str, str] = {}
    tracker = _usage_ctx.get()
    for result in client.messages.batches.results(batch.id):
        if result.result.type != "succeeded":
            continue
        message = result.result.message
        if tracker is not None:
            tracker.add(getattr(message, "usage", None))
        text = "".join(b.text for b in message.content if b.type == "text").strip()
        if text:
            results[result.custom_id] = text
    return results
