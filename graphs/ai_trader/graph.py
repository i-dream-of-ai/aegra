"""
AI Trader Agent - LangChain create_agent Implementation

This graph uses the LangChain create_agent pattern with middleware for:
- Dynamic model selection (based on runtime context from DB)
- Dynamic system prompt (based on runtime context from DB)
- Model fallback (automatic failover on errors)
- Todo list (task planning and tracking)
- Dangling tool call repair (fix interrupted sessions)
- Subconscious injection (dynamic skill/behavior loading)
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

import structlog
from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    ModelFallbackMiddleware,
    ModelRequest,
    ModelResponse,
    SummarizationMiddleware,
    TodoListMiddleware,
    dynamic_prompt,
    wrap_model_call,
)
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from ai_trader.context import Context
from ai_trader.prompts import DEFAULT_MAIN_PROMPT

# Import subconscious components
from ai_trader.subconscious.middleware import SubconsciousMiddleware as SubconsciousProcessor
from ai_trader.subconscious.types import SubconsciousEvent  # noqa: TC001

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
# Custom State (extends AgentState with our fields)
# =============================================================================


class AITraderState(AgentState):
    """Extended state for AI Trader agent."""

    # Subconscious injection context (populated by SubconsciousMiddleware)
    subconscious_context: str | None = None


# =============================================================================
# Default Model (required by create_agent, overridden by middleware)
# =============================================================================


def _get_default_model():
    """Get default model lazily to avoid import-time API key requirement.

    This is the fallback model if context doesn't specify one.
    In practice, context should always have model from DB.
    """
    return ChatOpenAI(
        model="gpt-5",
        max_tokens=8192,
        api_key=os.environ.get("OPENAI_API_KEY", "placeholder"),
    )


# =============================================================================
# Middleware: Dynamic Model Selection
# =============================================================================


@wrap_model_call
async def dynamic_model_middleware(request: ModelRequest, handler) -> ModelResponse:
    """
    Select and configure model based on runtime context.

    Context is passed at invocation time from runs.py, which fetches
    config from the DB before invoking the agent.

    Supports:
    - Claude models with extended thinking
    - OpenAI models with reasoning effort
    """
    ctx = request.runtime.context

    # Get model from context (loaded from DB by runs.py)
    model_name = ctx.get("model")
    if not model_name:
        # Use default if not specified (shouldn't happen in production)
        logger.warning("No model in context, using default")
        return await handler(request)

    is_claude = model_name.startswith("claude")

    if is_claude:
        base_max_tokens = 8192
        model_kwargs = {
            "model": model_name,
            "api_key": os.environ.get("ANTHROPIC_API_KEY"),
        }

        # thinking_budget from context (loaded from DB)
        thinking_budget = ctx.get("thinking_budget") or 0
        if thinking_budget > 0:
            model_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            }
            model_kwargs["max_tokens"] = max(base_max_tokens, thinking_budget + 4096)
        else:
            model_kwargs["max_tokens"] = base_max_tokens

        model = ChatAnthropic(**model_kwargs)
    else:
        model_kwargs = {
            "model": model_name,
            "api_key": os.environ.get("OPENAI_API_KEY"),
            "max_tokens": 8192,
        }

        # reasoning_effort from context (loaded from DB)
        reasoning_effort = ctx.get("reasoning_effort")
        if reasoning_effort and reasoning_effort != "none":
            model_kwargs["reasoning_effort"] = reasoning_effort

        model = ChatOpenAI(**model_kwargs)

    logger.info(
        "Dynamic model selected",
        model=model_name,
        thinking_budget=ctx.get("thinking_budget"),
        reasoning_effort=ctx.get("reasoning_effort"),
    )

    return await handler(request.override(model=model))


# =============================================================================
# Middleware: Dynamic System Prompt
# =============================================================================


@dynamic_prompt
def dynamic_system_prompt(request: ModelRequest) -> str:
    """
    Generate system prompt based on runtime context.

    Context is passed at invocation time from runs.py, which fetches
    config from the DB before invoking the agent.
    """
    ctx = request.runtime.context

    # Get system prompt from context (loaded from DB by runs.py)
    system_prompt = ctx.get("system_prompt")
    if not system_prompt:
        # Use default from local prompt files
        logger.debug("No system prompt in context, using default")
        system_prompt = DEFAULT_MAIN_PROMPT

    # Append subconscious context if available
    subconscious_context = request.state.get("subconscious_context")
    if subconscious_context:
        system_prompt += f"\n\n<injected_context>\n{subconscious_context}\n</injected_context>"

    return system_prompt


# =============================================================================
# Middleware: Dangling Tool Call Repair
# =============================================================================


class DanglingToolRepairMiddleware(AgentMiddleware[AITraderState]):
    """
    Repairs message history when tool calls are interrupted.

    The problem:
    - Agent requests tool call
    - Tool call is interrupted (user cancels, network error, crash)
    - Agent sees tool_call in AIMessage but no corresponding ToolMessage
    - This creates an invalid message sequence that Claude API rejects

    The solution:
    - Detects AIMessages with tool_calls that have no results
    - Creates synthetic ToolMessage responses indicating cancellation
    """

    state_schema = AITraderState

    def before_model(
        self, state: AITraderState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Repair dangling tool calls before sending to model."""
        messages = list(state.get("messages", []))
        if not messages:
            return None

        repaired = []
        i = 0
        made_repairs = False

        while i < len(messages):
            msg = messages[i]
            repaired.append(msg)

            # Check if this is an AIMessage with tool_calls
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                tool_call_ids = {
                    tc.get("id") or tc.get("tool_call_id")
                    for tc in msg.tool_calls
                    if tc
                }

                # Look ahead to find which tool_calls have results
                j = i + 1
                found_result_ids = set()
                while j < len(messages):
                    next_msg = messages[j]
                    if isinstance(next_msg, ToolMessage):
                        found_result_ids.add(next_msg.tool_call_id)
                    elif hasattr(next_msg, "tool_calls") and next_msg.tool_calls:
                        break
                    j += 1

                # Create synthetic results for missing tool_calls
                missing_ids = tool_call_ids - found_result_ids
                for tool_call in msg.tool_calls:
                    tc_id = tool_call.get("id") or tool_call.get("tool_call_id")
                    if tc_id in missing_ids:
                        tc_name = tool_call.get("name", "unknown")
                        logger.warning(
                            "Repairing dangling tool call",
                            tool_call_id=tc_id,
                            tool_name=tc_name,
                        )
                        repaired.append(
                            ToolMessage(
                                content=f"Tool call was interrupted or cancelled. The tool '{tc_name}' did not return a result.",
                                tool_call_id=tc_id,
                            )
                        )
                        made_repairs = True

            i += 1

        if made_repairs:
            return {"messages": repaired}
        return None


# =============================================================================
# Middleware: Subconscious Injection (RAG + Memory)
# =============================================================================


class SubconsciousMiddleware(AgentMiddleware[AITraderState]):
    """
    Injects context from skills, memories, and knowledge base.

    Runs ONCE at the start of each run to inject:
    - Agent memories (long-term storage)
    - Algorithm knowledge base (RAG search)
    - User preferences and past interactions

    Emits SSE events for UI progress indicator.
    """

    state_schema = AITraderState

    async def abefore_agent(
        self, state: AITraderState, runtime: Runtime[Context]
    ) -> dict[str, Any] | None:
        """Inject subconscious context before agent runs."""
        ctx = runtime.context

        if not ctx.get("subconscious_enabled", True):
            logger.debug("Subconscious disabled, skipping")
            return None

        access_token = ctx.get("access_token")
        if not access_token:
            logger.debug("No access token, skipping subconscious")
            return None

        try:
            writer = get_stream_writer()

            def emit_event(event: SubconsciousEvent):
                if event.type == "instinct_injection":
                    writer({"type": event.type, "data": event.data or {}})
                else:
                    writer({"type": event.type, "stage": event.stage})

            processor = SubconsciousProcessor(on_event=emit_event)

            injected_context = await processor.process(
                messages=list(state.get("messages", [])),
                access_token=access_token,
                current_turn=0,
            )

            if injected_context:
                logger.info(
                    "Subconscious injected context",
                    token_count=len(injected_context.split()),
                )
                return {"subconscious_context": injected_context}

        except Exception as e:
            logger.warning("Subconscious injection failed", error=str(e))
            with contextlib.suppress(Exception):
                writer = get_stream_writer()
                writer({"type": "subconscious_thinking", "stage": "done"})

        return None


# =============================================================================
# Create the Agent
# =============================================================================

graph = create_agent(
    # Default model (overridden by dynamic_model_middleware based on context)
    model=_get_default_model(),
    # All available tools
    tools=ALL_TOOLS,
    # Middleware stack (executed in order)
    middleware=[
        # 1. Model fallback - automatic failover on errors
        ModelFallbackMiddleware(
            "claude-sonnet-4-5-20250929",  # First fallback
            "gpt-5",  # Cross-provider fallback
        ),
        # 2. Summarization - compress long conversations to fit context
        SummarizationMiddleware(
            model="gpt-5-mini",  # Fast/cheap model for summarization
            trigger=("tokens", 100000),  # Trigger when messages exceed 100k tokens
            keep=("messages", 20),  # Keep last 20 messages intact
        ),
        # 3. Todo list - task planning and tracking
        TodoListMiddleware(),
        # 4. Subconscious - RAG + memory injection (runs before model selection)
        SubconsciousMiddleware(),
        # 5. Dangling tool repair - fix interrupted sessions
        DanglingToolRepairMiddleware(),
        # 6. Dynamic model - select model based on context from DB
        dynamic_model_middleware,
        # 7. Dynamic prompt - set system prompt from context + inject subconscious
        dynamic_system_prompt,
    ],
    # Context schema for runtime configuration (passed at invocation)
    context_schema=Context,
    # Graph name for debugging
    name="Shooby Dooby",
)
