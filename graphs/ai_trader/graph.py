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
- Generative UI via push_ui_message for custom components
"""

# from __future__ import annotations

import contextlib
import os
import time
import uuid
from datetime import UTC, datetime
import typing
from typing import TYPE_CHECKING, Any, Callable, Sequence

import structlog
from deepagents import create_deep_agent, CompiledSubAgent
from langchain.agents.middleware import (
    SummarizationMiddleware,
    ContextEditingMiddleware,
    ClearToolUsesEdit,
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
from langgraph.graph.ui import AnyUIMessage, push_ui_message, ui_message_reducer
from langgraph.runtime import Runtime

from graphs.ai_trader.context import Context
from graphs.ai_trader.prompts import DEFAULT_MAIN_PROMPT
from graphs.ai_trader.subconscious.middleware import (
    SubconsciousMiddleware as SubconsciousProcessor,
)

if TYPE_CHECKING:
    from graphs.ai_trader.subconscious.types import SubconsciousEvent

# Import all tools
from graphs.ai_trader.tools.ai_services import TOOLS as AI_SERVICES_TOOLS
from graphs.ai_trader.tools.backtest import TOOLS as BACKTEST_TOOLS
from graphs.ai_trader.tools.compile import TOOLS as COMPILE_TOOLS
from graphs.ai_trader.tools.composite import TOOLS as COMPOSITE_TOOLS
from graphs.ai_trader.tools.files import TOOLS as FILES_TOOLS
from graphs.ai_trader.tools.misc import TOOLS as MISC_TOOLS
from graphs.ai_trader.tools.object_store import TOOLS as OBJECT_STORE_TOOLS
from graphs.ai_trader.tools.optimization import TOOLS as OPTIMIZATION_TOOLS
# Import reviewer graph for subagent configuration
from graphs.ai_trader.reviewer import reviewer_graph

logger = structlog.getLogger(__name__)

# Combine all tools (excluding REVIEW_TOOLS - reviewer is now a subagent)
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
# Custom State
# =============================================================================


class AITraderState(AgentState):
    """Extended agent state with subconscious context and generative UI."""
    subconscious_context: str | None = None
    request_review: bool = False
    # Generative UI messages - rendered by frontend via ui-registry
    ui: typing.Annotated[Sequence[AnyUIMessage], ui_message_reducer] = []


# =============================================================================
# Middleware: Dynamic Model Selection
# =============================================================================


@wrap_model_call
async def dynamic_model_selection(
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
    elif model_name.startswith("gpt") or model_name.startswith("o1") or model_name.startswith("o3") or model_name.startswith("ft:"):
        reasoning_effort = ctx.get("reasoning_effort")
        if reasoning_effort and reasoning_effort != "none":
            model = model.bind(reasoning_effort=reasoning_effort)
        
        # OpenAI has strict validation on message names - sanitize them
        # Pattern: ^[^\s<|\\/>]+$ (no whitespace, <, |, \, /, >)
        import re
        def sanitize_name(name: str) -> str:
            if not name:
                return name
            return re.sub(r'[\s<|\\/>]', '_', name)
        
        # Sanitize message names in the request
        sanitized_messages = []
        for msg in request.messages:
            if hasattr(msg, 'name') and msg.name:
                msg_copy = msg.model_copy()
                msg_copy.name = sanitize_name(msg.name)
                sanitized_messages.append(msg_copy)
            else:
                sanitized_messages.append(msg)
        
        # Override request with sanitized messages
        request = request.override(messages=sanitized_messages)
    
    logger.info("Dynamic model selection", model=model_name)
    return await handler(request.override(model=model))


# =============================================================================
# Middleware: Dynamic System Prompt with Subconscious Injection
# =============================================================================


@dynamic_prompt
def build_system_prompt(state: AITraderState, *args, **kwargs) -> str:
    """Build system prompt with timestamp and subconscious context."""
    # Defensive logic: Check if first arg is ModelRequest (has runtime) or State (dict)
    ctx = {}
    if args:
        arg0 = args[0]
        # Case 1: ModelRequest (Middleware usage)
        if hasattr(arg0, "runtime") and hasattr(arg0.runtime, "context"):
            ctx = arg0.runtime.context or {}
        # Case 2: Dict/State (Direct usage)
        elif hasattr(arg0, "get"):
            ctx = arg0
        # Case 3: Runtime object directly
        elif hasattr(arg0, "context"):
            ctx = arg0.context or {}
        # Case 4: RunnableConfig
        elif isinstance(arg0, dict) and "configurable" in arg0:
            ctx = arg0.get("configurable", {})

    # Fallback to kwargs if needed
    if not ctx and "config" in kwargs:
         cfg = kwargs["config"]
         ctx = cfg.get("configurable", {}) if isinstance(cfg, dict) else {}

    # Get base prompt
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
    subconscious = ctx.get("subconscious_context")
    if subconscious:
        prompt += f"\n\n<injected_context>\n{subconscious}\n</injected_context>"
    
    return prompt


# =============================================================================
# Middleware: Subconscious Injection (before model)
# =============================================================================


@before_model
async def inject_subconscious(state: AITraderState, runtime: Runtime) -> dict[str, Any] | None:
    """Inject subconscious context before model call using Generative UI."""
    ctx = runtime.context or {}
    
    logger.info("inject_subconscious called", context_keys=list(ctx.keys()) if ctx else [])
    
    if not ctx.get("subconscious_enabled", True):
        logger.info("Subconscious disabled via context flag")
        return None
    
    access_token = ctx.get("access_token")
    if not access_token:
        logger.warning("Subconscious skipped: no access_token in context")
        return None
    
    logger.info("Subconscious processing starting", has_token=bool(access_token))
    
    try:
        start_time = time.time()

        def emit_event(event: "SubconsciousEvent"):
            """Emit events via push_ui_message for SubconsciousPanel."""
            if event.type == "instinct_injection":
                # Final injection - push complete UI message with all data
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
                        # Legacy support
                        "skillIds": data.get("skillIds", []),
                        "instinctSkills": data.get("instinctSkills", []),
                        "contextualSkills": data.get("contextualSkills", []),
                    },
                )
            else:
                # Progress events - push UI message with current stage
                push_ui_message(
                    "subconscious-panel",
                    {"stage": event.stage},
                )

        processor = SubconsciousProcessor(on_event=emit_event)

        # Run async subconscious processing
        subconscious = await processor.process(
            messages=list(state["messages"]),
            access_token=access_token,
            current_turn=0,
        )
        
        logger.info("Subconscious processing complete", 
                   has_result=bool(subconscious),
                   result_length=len(subconscious) if subconscious else 0)
        
        if subconscious:
            return {"subconscious_context": subconscious}
            
    except Exception as e:
        logger.warning("Subconscious injection failed", error=str(e), exc_info=True)
        with contextlib.suppress(Exception):
            # Emit failure state as UI message
            push_ui_message("subconscious-panel", {"stage": "done"})
    
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
# Create Agent with Middleware and Subagents
# =============================================================================

# Default model from environment
DEFAULT_MODEL_NAME = os.environ.get("DEFAULT_MODEL", "gpt-5.2")
DEFAULT_MODEL = init_chat_model(model=DEFAULT_MODEL_NAME)

# Configure the reviewer as a subagent
REVIEWER_SUBAGENT = CompiledSubAgent(
    name="code-reviewer",
    description="""Doubtful Deacon - Chief Quant Strategist & Algorithm Auditor.

Use this subagent when you need:
- Code review of trading algorithms
- Analysis of backtest results
- Critique of strategy logic and edge cases
- Trading recommendations and testable experiments
- Second opinion on algorithm implementation

Deacon is a skeptical expert who will analyze the code, spot potential bugs,
identify QuantConnect/LEAN pitfalls, and suggest concrete improvements.
He operates in isolated context and returns a focused review.""",
    runnable=reviewer_graph,
)

# Create deep agent with all middleware and subagents
# Note: create_deep_agent has built-in summarization and dangling tool repair
graph = create_deep_agent(
    model=DEFAULT_MODEL,
    tools=ALL_TOOLS,
    subagents=[REVIEWER_SUBAGENT],
    middleware=[
        # Custom: Dynamic model selection from context
        dynamic_model_selection,
        # Custom: Dynamic system prompt with subconscious
        build_system_prompt,
        # Custom: Subconscious injection
        inject_subconscious,
    ],
    name="AI_Trader",
)
