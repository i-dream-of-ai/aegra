"""
Subconscious Node - Runs ONCE at graph start

This node runs before the main agent loop to:
1. Analyze the user's intent
2. Retrieve relevant skills from the knowledge base
3. Synthesize context to inject into the system prompt
4. Stream progress events to the frontend via push_ui_message

Unlike the middleware approach, this node:
- Runs exactly ONCE per graph invocation (not on every model call)
- Streams UI messages in real-time via push_ui_message (generative UI)
- Persists results in graph state for the agent to use
"""

import time
from typing import TYPE_CHECKING, Any

import structlog
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph.ui import push_ui_message

from graphs.ai_trader.subconscious.middleware import SubconsciousMiddleware as SubconsciousProcessor
from graphs.ai_trader.subconscious.types import SubconsciousEvent, is_confirmation_message

if TYPE_CHECKING:
    from graphs.ai_trader.graph import AITraderState

logger = structlog.getLogger(__name__)


async def subconscious_node(
    state: "AITraderState",
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Subconscious processing node - runs ONCE at graph start.

    Analyzes conversation, retrieves skills, and synthesizes context.
    Streams UI messages to frontend via push_ui_message (generative UI).

    Args:
        state: Current graph state with messages
        context: Runtime context with access_token, etc.

    Returns:
        State update with subconscious_context for the agent and ui messages
    """
    ctx = context or {}
    messages = state.get("messages", [])

    logger.info(
        "Subconscious node started",
        message_count=len(messages),
        context_keys=list(ctx.keys()) if ctx else [],
    )

    # Check if subconscious is enabled
    if not ctx.get("subconscious_enabled", True):
        logger.info("Subconscious disabled via context flag")
        return {}

    # Get access token for DB queries
    access_token = ctx.get("access_token")
    if not access_token:
        logger.warning("Subconscious skipped: no access_token in context")
        return {}

    # Check for confirmation message (skip subconscious for "yes", "ok", etc.)
    last_human = _get_last_human_message(messages)
    if last_human and is_confirmation_message(last_human):
        logger.info("Confirmation message detected, skipping subconscious")
        return {}

    # Start processing with streaming events
    start_time = time.time()

    def emit_event(event: SubconsciousEvent):
        """Emit UI message to stream for frontend consumption via push_ui_message."""
        if event.type == "instinct_injection":
            # Final injection event with all skill data - stage="done"
            data = event.data or {}
            duration_ms = int((time.time() - start_time) * 1000)
            push_ui_message(
                "subconscious-panel",
                {
                    "stage": "done",
                    "userIntent": data.get("userIntent"),
                    "skills": data.get("skills", []),
                    "content": data.get("content"),
                    "tokenCount": data.get("tokenCount", 0),
                    "synthesisMethod": data.get("synthesisMethod", "unknown"),
                    "durationMs": duration_ms,
                    "skillIds": data.get("skillIds", []),
                },
            )
        else:
            # Progress events (planning, retrieving, synthesizing)
            push_ui_message(
                "subconscious-panel",
                {"stage": event.stage},
            )

    try:
        processor = SubconsciousProcessor(on_event=emit_event)

        # Run subconscious processing
        subconscious_context = await processor.process(
            messages=list(messages),
            access_token=access_token,
            current_turn=0,  # Always 0 since this runs once at start
        )

        duration_ms = int((time.time() - start_time) * 1000)
        logger.info(
            "Subconscious processing complete",
            has_result=bool(subconscious_context),
            result_length=len(subconscious_context) if subconscious_context else 0,
            duration_ms=duration_ms,
        )

        if subconscious_context:
            return {"subconscious_context": subconscious_context}

        return {}

    except Exception as e:
        logger.warning("Subconscious processing failed", error=str(e), exc_info=True)
        # Emit done event even on error so UI doesn't get stuck
        push_ui_message("subconscious-panel", {"stage": "done"})
        return {}


def _get_last_human_message(messages: list[BaseMessage]) -> str | None:
    """Extract the last human message content."""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            content = msg.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                text_parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                return " ".join(text_parts)
    return None
