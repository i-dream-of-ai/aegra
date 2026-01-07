"""
AI Trader Agent - Using create_agent with Latest Middleware Patterns

Uses the LangChain create_agent API per official docs:
https://docs.langchain.com/oss/python/langchain/agents
https://docs.langchain.com/oss/python/langchain/middleware/built-in
https://docs.langchain.com/oss/python/langchain/middleware/custom

Middleware patterns:
- @dynamic_prompt: Dynamic system prompt with subconscious injection
- @wrap_model_call: Dynamic model selection from context
- SummarizationMiddleware: Built-in context window management
"""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Callable

import structlog
from langchain.agents import create_agent
from langchain.agents.middleware import (
    SummarizationMiddleware,
    dynamic_prompt,
    wrap_model_call,
    before_model,
    ModelRequest,
    ModelResponse,
    AgentState,
)
from langchain.chat_models import init_chat_model
from langchain.messages import SystemMessage
from langchain_core.messages import ToolMessage
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from ai_trader.context import Context
from ai_trader.prompts import DEFAULT_MAIN_PROMPT
from ai_trader.subconscious.middleware import (
    SubconsciousMiddleware as SubconsciousProcessor,
)

if TYPE_CHECKING:
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
from ai_trader.tools.review import TOOLS as REVIEW_TOOLS

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
    + REVIEW_TOOLS
)


# =============================================================================
# Custom State
# =============================================================================


class AITraderState(AgentState):
    """Extended agent state with subconscious context."""
    subconscious_context: str | None = None
    request_review: bool = False


# =============================================================================
# Middleware: Dynamic Model Selection
# =============================================================================


@wrap_model_call
def dynamic_model_selection(
    request: ModelRequest,
    handler: Callable[[ModelRequest], ModelResponse],
) -> ModelResponse:
    """Select model dynamically based on runtime context."""
    ctx = request.runtime.context or {}
    model_name = ctx.get("model", os.environ.get("DEFAULT_MODEL", "gpt-5.2"))
    
    # Initialize model based on name
    model = init_chat_model(model_name)
    
    # Apply Claude thinking budget if set
    if model_name.startswith("claude"):
        thinking_budget = ctx.get("thinking_budget") or 0
        if thinking_budget > 0:
            model = model.bind(
                thinking={"type": "enabled", "budget_tokens": thinking_budget}
            )
    
    # Apply OpenAI reasoning effort if set
    elif model_name.startswith("gpt"):
        reasoning_effort = ctx.get("reasoning_effort")
        if reasoning_effort and reasoning_effort != "none":
            model = model.bind(reasoning_effort=reasoning_effort)
    
    logger.info("Dynamic model selection", model=model_name)
    return handler(request.override(model=model))


# =============================================================================
# Middleware: Dynamic System Prompt with Subconscious Injection
# =============================================================================


@dynamic_prompt
def build_system_prompt(state: AITraderState, runtime: Runtime) -> str:
    """Build system prompt with timestamp and subconscious context."""
    ctx = runtime.context or {}
    
    # Get base prompt from context or default
    base_prompt = ctx.get("system_prompt") or DEFAULT_MAIN_PROMPT
    
    logger.info(
        "System prompt source",
        has_ctx=bool(ctx),
        ctx_has_prompt=bool(ctx.get("system_prompt")),
        using_default=not bool(ctx.get("system_prompt")),
        prompt_preview=base_prompt[:100] if base_prompt else "NONE",
    )
    
    # Format with timestamp
    prompt = base_prompt.format(system_time=datetime.now(tz=UTC).isoformat())
    
    # Add subconscious context if available
    subconscious = state.get("subconscious_context")
    if subconscious:
        prompt += f"\n\n<injected_context>\n{subconscious}\n</injected_context>"
    
    return prompt


# =============================================================================
# Middleware: Subconscious Injection (before model)
# =============================================================================


@before_model
def inject_subconscious(state: AITraderState, runtime: Runtime) -> dict[str, Any] | None:
    """Inject subconscious context before model call."""
    ctx = runtime.context or {}
    
    if not ctx.get("subconscious_enabled", True):
        return None
    
    access_token = ctx.get("access_token")
    if not access_token:
        return None
    
    try:
        writer = get_stream_writer()

        def emit_event(event: "SubconsciousEvent"):
            if event.type == "instinct_injection":
                writer({"type": event.type, "data": event.data or {}})
            else:
                writer({"type": event.type, "stage": event.stage})

        processor = SubconsciousProcessor(on_event=emit_event)

        # Run async subconscious processing
        import asyncio
        loop = asyncio.get_event_loop()
        subconscious = loop.run_until_complete(
            processor.process(
                messages=list(state["messages"]),
                access_token=access_token,
                current_turn=0,
            )
        )
        
        if subconscious:
            return {"subconscious_context": subconscious}
            
    except Exception as e:
        logger.warning("Subconscious injection failed", error=str(e))
        with contextlib.suppress(Exception):
            writer = get_stream_writer()
            writer({"type": "subconscious_thinking", "stage": "done"})
    
    return None


# =============================================================================
# Middleware: Patch Dangling Tool Calls
# =============================================================================


@before_model
def patch_dangling_tool_calls(state: AITraderState, runtime: Runtime) -> dict[str, Any] | None:
    """Patch any dangling tool calls before model invocation."""
    messages = list(state["messages"])
    if not messages:
        return None

    patched = []
    tool_results = {
        m.tool_call_id: m
        for m in messages
        if getattr(m, "type", None) == "tool" and getattr(m, "tool_call_id", None)
    }
    used_results = set()

    i = 0
    while i < len(messages):
        msg = messages[i]

        if getattr(msg, "type", None) == "tool":
            i += 1
            continue

        patched.append(msg)

        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                tool_call_id = tc.get("id")
                if not tool_call_id:
                    continue

                if tool_call_id in tool_results:
                    patched.append(tool_results[tool_call_id])
                    used_results.add(tool_call_id)
                else:
                    tool_name = tc.get("name", "unknown")
                    logger.warning("Patching dangling tool call", tool_call_id=tool_call_id)
                    patched.append(
                        ToolMessage(
                            content=f"Tool {tool_name} was interrupted/cancelled.",
                            name=tool_name,
                            tool_call_id=tool_call_id,
                        )
                    )
        i += 1

    # Only return update if messages changed
    if patched != messages:
        return {"messages": patched}
    return None


# =============================================================================
# Create Agent with Middleware
# =============================================================================

# Default model from environment
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "gpt-5.2")

# Create agent with all middleware
graph = create_agent(
    model=DEFAULT_MODEL,
    tools=ALL_TOOLS,
    state_schema=AITraderState,
    context_schema=Context,
    middleware=[
        # Built-in: Summarization at 100K tokens, keep 20 messages
        SummarizationMiddleware(
            model="gpt-4o-mini",
            trigger=("tokens", 100000),
            keep=("messages", 20),
        ),
        # Custom: Dynamic model selection from context
        dynamic_model_selection,
        # Custom: Dynamic system prompt with subconscious
        build_system_prompt,
        # Custom: Subconscious injection
        inject_subconscious,
        # Custom: Patch dangling tool calls
        patch_dangling_tool_calls,
    ],
    name="AI Trader",
)
