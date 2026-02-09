"""
AI Cost Tracking Service

Fire-and-forget logging of every paid LLM API call to the ai_cost_log table.
Never blocks or raises — errors are logged and swallowed.
"""

import asyncio
import os
from typing import Any, Optional

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Pricing per million tokens (input, output, thinking)
# Keeping this as a simple dict — update when models change
PRICING: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-opus-4-5-20250929": {"input": 15.0, "output": 75.0, "thinking": 15.0, "cache_read": 1.5, "cache_creation": 18.75, "provider": "anthropic"},
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0, "thinking": 3.0, "cache_read": 0.30, "cache_creation": 3.75, "provider": "anthropic"},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.0, "thinking": 0.80, "cache_read": 0.08, "cache_creation": 1.0, "provider": "anthropic"},
    # OpenAI
    "gpt-5.2": {"input": 2.0, "output": 8.0, "thinking": 0, "provider": "openai"},
    "gpt-5-mini": {"input": 0.40, "output": 1.60, "thinking": 0, "provider": "openai"},
    "gpt-5-nano": {"input": 0.10, "output": 0.40, "thinking": 0, "provider": "openai"},
    "text-embedding-3-small": {"input": 0.02, "output": 0, "thinking": 0, "provider": "openai"},
}

# Fallback aliases (model name prefixes or partial matches)
MODEL_ALIASES: dict[str, str] = {
    "claude-opus": "claude-opus-4-5-20250929",
    "claude-sonnet": "claude-sonnet-4-5-20250929",
    "claude-haiku": "claude-haiku-4-5-20251001",
}


def _resolve_model(model: str) -> tuple[str, dict[str, float]]:
    """Resolve a model name to its pricing entry."""
    if model in PRICING:
        return model, PRICING[model]
    for alias, canonical in MODEL_ALIASES.items():
        if model.startswith(alias):
            return model, PRICING[canonical]
    # Unknown model — log with zero cost rather than fail
    logger.warning("unknown_model_for_pricing", model=model)
    return model, {"input": 0, "output": 0, "thinking": 0, "provider": "unknown"}


def _calculate_cost(
    pricing: dict[str, float],
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Calculate estimated cost in USD from token counts and pricing."""
    cost = (
        input_tokens * pricing.get("input", 0)
        + output_tokens * pricing.get("output", 0)
        + thinking_tokens * pricing.get("thinking", 0)
        + cache_read_tokens * pricing.get("cache_read", 0)
        + cache_creation_tokens * pricing.get("cache_creation", 0)
    ) / 1_000_000
    return round(cost, 6)


def extract_usage_from_response(response: Any) -> dict[str, int]:
    """Extract token usage from a LangChain AIMessage response."""
    usage: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }

    # LangChain AIMessage.usage_metadata
    metadata = getattr(response, "usage_metadata", None)
    if metadata:
        if isinstance(metadata, dict):
            usage["input_tokens"] = metadata.get("input_tokens", 0)
            usage["output_tokens"] = metadata.get("output_tokens", 0)
            # Thinking tokens may be in input_token_details
            details = metadata.get("input_token_details", {})
            if isinstance(details, dict):
                usage["cache_read_tokens"] = details.get("cache_read", 0) or 0
                usage["cache_creation_tokens"] = details.get("cache_creation", 0) or 0
            # Output token details for thinking/reasoning
            out_details = metadata.get("output_token_details", {})
            if isinstance(out_details, dict):
                usage["thinking_tokens"] = out_details.get("reasoning", 0) or 0
        else:
            # Object-style access
            usage["input_tokens"] = getattr(metadata, "input_tokens", 0) or 0
            usage["output_tokens"] = getattr(metadata, "output_tokens", 0) or 0

    return usage


async def _insert_cost_log(
    user_id: str,
    model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    estimated_cost_usd: float,
    call_source: str,
    run_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    project_id: Optional[str] = None,
    session_id: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Insert a row into ai_cost_log via Supabase REST API."""
    supabase_url = os.environ.get("SUPABASE_URL") or os.environ.get(
        "NEXT_PUBLIC_SUPABASE_URL"
    )
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

    if not supabase_url or not supabase_key:
        logger.warning("supabase_not_configured_for_cost_tracking")
        return

    row = {
        "user_id": user_id,
        "model": model,
        "provider": provider,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "estimated_cost_usd": estimated_cost_usd,
        "call_source": call_source,
        "metadata": metadata or {},
    }
    if run_id:
        row["run_id"] = run_id
    if thread_id:
        row["thread_id"] = thread_id
    if project_id:
        row["project_id"] = project_id
    if session_id is not None:
        row["session_id"] = session_id

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{supabase_url}/rest/v1/ai_cost_log",
            json=row,
            headers={
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            timeout=10.0,
        )
        resp.raise_for_status()

    logger.debug(
        "ai_cost_logged",
        model=model,
        call_source=call_source,
        cost=estimated_cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def log_ai_cost(
    user_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    call_source: str,
    *,
    thinking_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    run_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    project_id: Optional[str] = None,
    session_id: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> None:
    """
    Fire-and-forget: schedule cost logging as a background task.
    Never blocks, never raises.
    """
    if not user_id or (input_tokens == 0 and output_tokens == 0):
        return

    resolved_model, pricing = _resolve_model(model)
    provider = pricing.get("provider", "unknown")
    estimated_cost = _calculate_cost(
        pricing, input_tokens, output_tokens, thinking_tokens,
        cache_read_tokens, cache_creation_tokens,
    )

    async def _task() -> None:
        try:
            await _insert_cost_log(
                user_id=user_id,
                model=resolved_model,
                provider=provider,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                thinking_tokens=thinking_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                estimated_cost_usd=estimated_cost,
                call_source=call_source,
                run_id=run_id,
                thread_id=thread_id,
                project_id=project_id,
                session_id=session_id,
                metadata=metadata,
            )
        except Exception:
            logger.exception("failed_to_log_ai_cost", model=model, call_source=call_source)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_task())
    except RuntimeError:
        # No event loop — skip silently
        logger.warning("no_event_loop_for_cost_tracking")
