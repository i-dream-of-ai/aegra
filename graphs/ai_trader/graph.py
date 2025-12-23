"""
AI Trader Agent - Multi-Agent Handoffs Implementation

Following the aegra react_agent pattern with StateGraph, manual call_model,
and ToolNode. Supports handoffs between main agent and reviewer.

Middleware pattern replaced with:
- Dynamic model/prompt selection in call_model
- Dangling tool call patching in call_model
- Subconscious injection before model call
"""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime
from typing import Literal, cast

import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime

from ai_trader.context import Context
from ai_trader.prompts import DEFAULT_MAIN_PROMPT, DEFAULT_REVIEWER_PROMPT
from ai_trader.state import InputState, State
from ai_trader.subconscious.middleware import (
    SubconsciousMiddleware as SubconsciousProcessor,
)
from ai_trader.subconscious.types import SubconsciousEvent

# Import all tools
from ai_trader.tools.ai_services import TOOLS as AI_SERVICES_TOOLS
from ai_trader.tools.backtest import TOOLS as BACKTEST_TOOLS
from ai_trader.tools.compile import TOOLS as COMPILE_TOOLS
from ai_trader.tools.composite import TOOLS as COMPOSITE_TOOLS
from ai_trader.tools.files import TOOLS as FILES_TOOLS
from ai_trader.tools.misc import TOOLS as MISC_TOOLS
from ai_trader.tools.object_store import TOOLS as OBJECT_STORE_TOOLS
from ai_trader.tools.optimization import TOOLS as OPTIMIZATION_TOOLS

logger = structlog.getLogger(__name__)

# Combine all tools
ALL_TOOLS = (
    FILES_TOOLS
    + BACKTEST_TOOLS
    + COMPILE_TOOLS
    + COMPOSITE_TOOLS
    + OPTIMIZATION_TOOLS
    + OBJECT_STORE_TOOLS
    + AI_SERVICES_TOOLS
    + MISC_TOOLS
)


# =============================================================================
# Helper Functions
# =============================================================================


def _get_model(ctx: dict):
    """Get model instance based on context from DB."""
    model_name = ctx.get("model", "claude-sonnet-4-5-20250929")
    is_claude = model_name.startswith("claude")

    if is_claude:
        base_max_tokens = 16384
        model_kwargs = {
            "model": model_name,
            "api_key": os.environ.get("ANTHROPIC_API_KEY"),
        }

        thinking_budget = ctx.get("thinking_budget") or 0
        if thinking_budget > 0:
            model_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            }
            model_kwargs["max_tokens"] = max(base_max_tokens, thinking_budget + 4096)
        else:
            model_kwargs["max_tokens"] = base_max_tokens

        return ChatAnthropic(**model_kwargs)
    else:
        model_kwargs = {
            "model": model_name,
            "api_key": os.environ.get("OPENAI_API_KEY"),
            "max_tokens": 16384,
        }

        reasoning_effort = ctx.get("reasoning_effort")
        if reasoning_effort and reasoning_effort != "none":
            model_kwargs["reasoning_effort"] = reasoning_effort

        return ChatOpenAI(**model_kwargs)


def _patch_dangling_tool_calls(messages: list) -> list:
    """Patch any dangling tool calls that don't have results."""
    if not messages:
        return messages

    patched = []
    for i, msg in enumerate(messages):
        patched.append(msg)

        if getattr(msg, "type", None) == "ai" and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                tool_call_id = tc.get("id")
                if not tool_call_id:
                    continue

                # Check if there's a result in remaining messages
                has_result = any(
                    getattr(m, "type", None) == "tool"
                    and getattr(m, "tool_call_id", None) == tool_call_id
                    for m in messages[i + 1 :]
                )

                if not has_result:
                    tool_name = tc.get("name", "unknown")
                    logger.warning(
                        "Patching dangling tool call", tool_call_id=tool_call_id
                    )
                    patched.append(
                        ToolMessage(
                            content=f"Tool {tool_name} was cancelled.",
                            name=tool_name,
                            tool_call_id=tool_call_id,
                        )
                    )

    return patched


async def _inject_subconscious(state: State, ctx: dict) -> str | None:
    """Inject subconscious context (RAG + memories)."""
    if not ctx.get("subconscious_enabled", True):
        return None

    access_token = ctx.get("access_token")
    if not access_token:
        return None

    try:
        writer = get_stream_writer()

        def emit_event(event: SubconsciousEvent):
            if event.type == "instinct_injection":
                writer({"type": event.type, "data": event.data or {}})
            else:
                writer({"type": event.type, "stage": event.stage})

        processor = SubconsciousProcessor(on_event=emit_event)

        return await processor.process(
            messages=list(state.messages),
            access_token=access_token,
            current_turn=0,
        )
    except Exception as e:
        logger.warning("Subconscious injection failed", error=str(e))
        with contextlib.suppress(Exception):
            writer = get_stream_writer()
            writer({"type": "subconscious_thinking", "stage": "done"})
        return None


# =============================================================================
# Main Agent: call_model
# =============================================================================


async def call_model(state: State, runtime: Runtime[Context]) -> dict:
    """Call the main LLM with all trading tools."""
    ctx = runtime.context

    # Get model based on context
    model = _get_model(ctx).bind_tools(ALL_TOOLS)

    # Build system prompt
    system_prompt = ctx.get("system_prompt") or DEFAULT_MAIN_PROMPT
    system_prompt = system_prompt.format(system_time=datetime.now(tz=UTC).isoformat())

    # Inject subconscious context
    subconscious = await _inject_subconscious(state, ctx)
    if subconscious:
        system_prompt += f"\n\n<injected_context>\n{subconscious}\n</injected_context>"

    # Patch dangling tool calls
    messages = _patch_dangling_tool_calls(list(state.messages))

    logger.info(
        "Calling main model",
        model=ctx.get("model"),
        message_count=len(messages),
    )

    # Call model
    response = cast(
        AIMessage,
        await model.ainvoke([{"role": "system", "content": system_prompt}, *messages]),
    )

    # Handle last step
    if state.is_last_step and response.tool_calls:
        return {
            "messages": [
                AIMessage(
                    id=response.id,
                    content="I ran out of steps. Please continue the conversation.",
                )
            ]
        }

    return {"messages": [response]}


# =============================================================================
# Reviewer Agent: call_reviewer
# =============================================================================


async def call_reviewer(state: State, runtime: Runtime[Context]) -> dict:  # noqa: ARG001
    """Call the reviewer LLM for code critique."""
    # Reviewer uses Claude without extended thinking
    model = ChatAnthropic(
        model="claude-sonnet-4-5-20250929",
        max_tokens=8192,
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
    )

    system_prompt = DEFAULT_REVIEWER_PROMPT.format(
        system_time=datetime.now(tz=UTC).isoformat()
    )

    messages = _patch_dangling_tool_calls(list(state.messages))

    logger.info("Calling reviewer model", message_count=len(messages))

    response = cast(
        AIMessage,
        await model.ainvoke([{"role": "system", "content": system_prompt}, *messages]),
    )

    return {"messages": [response]}


# =============================================================================
# Routing
# =============================================================================


def route_model_output(state: State) -> Literal["__end__", "tools"]:
    """Route based on whether model wants to use tools."""
    last_message = state.messages[-1]
    if not isinstance(last_message, AIMessage):
        raise ValueError(f"Expected AIMessage, got {type(last_message).__name__}")

    if not last_message.tool_calls:
        return "__end__"
    return "tools"


# =============================================================================
# Build Graph
# =============================================================================

builder = StateGraph(State, input_schema=InputState, context_schema=Context)

# Add nodes
builder.add_node("call_model", call_model)
builder.add_node("tools", ToolNode(ALL_TOOLS))

# Edges
builder.add_edge("__start__", "call_model")
builder.add_conditional_edges("call_model", route_model_output, ["tools", END])
builder.add_edge("tools", "call_model")

# Compile
graph = builder.compile(name="AI Trader")
