"""
AI Trader Agent - Multi-Agent Handoffs Implementation

This graph uses the LangChain create_agent pattern wrapped in a parent StateGraph
to enable handoffs between agents (main -> reviewer) using Command.PARENT.

Middleware provides:
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
from typing import Any, Literal

from typing_extensions import NotRequired

import structlog
from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelFallbackMiddleware,
    ModelRequest,
    ModelResponse,
    SummarizationMiddleware,
    TodoListMiddleware,
    dynamic_prompt,
    wrap_model_call,
)
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from ai_trader.context import Context
from ai_trader.prompts import DEFAULT_MAIN_PROMPT, DEFAULT_REVIEWER_PROMPT

# Import subconscious components
from ai_trader.subconscious.middleware import (
    SubconsciousMiddleware as SubconsciousProcessor,
)
from ai_trader.subconscious.types import SubconsciousEvent  # noqa: TC001

# Import all tools (except review - we'll define the handoff tool here)
from ai_trader.tools.ai_services import TOOLS as AI_SERVICES_TOOLS
from ai_trader.tools.backtest import TOOLS as BACKTEST_TOOLS
from ai_trader.tools.compile import TOOLS as COMPILE_TOOLS
from ai_trader.tools.composite import TOOLS as COMPOSITE_TOOLS
from ai_trader.tools.files import TOOLS as FILES_TOOLS
from ai_trader.tools.misc import TOOLS as MISC_TOOLS
from ai_trader.tools.object_store import TOOLS as OBJECT_STORE_TOOLS
from ai_trader.tools.optimization import TOOLS as OPTIMIZATION_TOOLS

# Import handoff tools separately
from ai_trader.tools.review import REVIEWER_TOOLS
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
# Custom State (extends AgentState with handoff tracking)
# =============================================================================


class AITraderState(AgentState):
    """Extended state for AI Trader agent with handoff support."""

    # Subconscious injection context (populated by SubconsciousMiddleware)
    subconscious_context: str | None = None

    # Track which agent is active for routing
    active_agent: NotRequired[str]


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


def _get_reviewer_model():
    """Get reviewer model - uses Claude for critique."""
    return ChatAnthropic(
        model="claude-sonnet-4-5-20250929",
        max_tokens=4096,
        api_key=os.environ.get("ANTHROPIC_API_KEY", "placeholder"),
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
        system_prompt += (
            f"\n\n<injected_context>\n{subconscious_context}\n</injected_context>"
        )

    return system_prompt


# =============================================================================
# Middleware: Patch Dangling Tool Calls (wrap_model_call version)
# =============================================================================


@wrap_model_call
async def patch_dangling_tool_calls(request: ModelRequest, handler) -> ModelResponse:
    """
    Patches dangling tool calls in request.messages before each model call.

    Uses wrap_model_call because it intercepts the actual messages being sent
    to the model, including messages loaded from checkpoint.

    The Anthropic API requires every tool_use to have a tool_result.
    """
    messages = list(request.messages)
    logger.info(
        "patch_dangling_tool_calls running",
        message_count=len(messages),
    )

    if not messages:
        return await handler(request)

    patched_messages = []
    made_repairs = False

    for i, msg in enumerate(messages):
        patched_messages.append(msg)

        # Check if this is an AI message with tool_calls
        if getattr(msg, "type", None) == "ai" and getattr(msg, "tool_calls", None):
            for tool_call in msg.tool_calls:
                tool_call_id = tool_call.get("id")
                if not tool_call_id:
                    continue

                # Look for a corresponding ToolMessage in the remaining messages
                has_result = any(
                    getattr(m, "type", None) == "tool"
                    and getattr(m, "tool_call_id", None) == tool_call_id
                    for m in messages[i + 1 :]
                )

                if not has_result:
                    # Create a synthetic tool result
                    tool_name = tool_call.get("name", "unknown")
                    logger.warning(
                        "Patching dangling tool call",
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                    )
                    patched_messages.append(
                        ToolMessage(
                            content=(
                                f"Tool call {tool_name} with id {tool_call_id} was "
                                "cancelled - another message came in before it could be completed."
                            ),
                            name=tool_name,
                            tool_call_id=tool_call_id,
                        )
                    )
                    made_repairs = True

    if made_repairs:
        logger.info(
            "Patched dangling tool calls",
            repair_count=len(patched_messages) - len(messages),
        )
        return await handler(request.override(messages=patched_messages))

    return await handler(request)


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
# Create Agents
# =============================================================================

# Shared middleware stack for the main agent
MAIN_MIDDLEWARE = [
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
    # 5. Patch dangling tool calls - fix interrupted sessions
    patch_dangling_tool_calls,
    # 6. Dynamic model - select model based on context from DB
    dynamic_model_middleware,
    # 7. Dynamic prompt - set system prompt from context + inject subconscious
    dynamic_system_prompt,
]

# Main agent (Shooby Dooby) - has all trading tools + handoff tool
main_agent = create_agent(
    model=_get_default_model(),
    tools=ALL_TOOLS,
    middleware=MAIN_MIDDLEWARE,
    context_schema=Context,
    name="Shooby Dooby",
)

# Reviewer agent (Doubtful Deacon) - has handoff tool to transfer back
reviewer_agent = create_agent(
    model=_get_reviewer_model(),
    tools=REVIEWER_TOOLS,  # Only has transfer_to_main_agent for handoff back
    system_prompt=DEFAULT_REVIEWER_PROMPT,
    name="Doubtful Deacon",
)


# =============================================================================
# Agent Nodes (wrap agents for parent graph)
# =============================================================================


def call_main_agent(state: AITraderState):
    """Node that invokes the main agent."""
    logger.info("Calling main agent (Shooby Dooby)")
    return main_agent.invoke(state)


def call_reviewer_agent(state: AITraderState):
    """Node that invokes the reviewer agent."""
    logger.info("Calling reviewer agent (Doubtful Deacon)")
    return reviewer_agent.invoke(state)


# =============================================================================
# Routing Logic
# =============================================================================


def route_after_agent(
    state: AITraderState,
) -> Literal["main_agent", "reviewer", "__end__"]:
    """Route based on active_agent, or END if agent finished without handoff."""
    messages = state.get("messages", [])

    # Check the last message - if it's an AIMessage without tool calls, we're done
    if messages:
        last_msg = messages[-1]
        if isinstance(last_msg, AIMessage) and not last_msg.tool_calls:
            logger.info("Agent finished without tool calls, ending")
            return "__end__"

    # Route to active agent
    active = state.get("active_agent", "main_agent")
    logger.info(f"Routing to active agent: {active}")
    return active if active in ("main_agent", "reviewer") else "main_agent"


def route_initial(
    state: AITraderState,
) -> Literal["main_agent", "reviewer"]:
    """Route to active agent based on state, default to main agent."""
    return state.get("active_agent") or "main_agent"


# =============================================================================
# Build Parent Graph
# =============================================================================

builder = StateGraph(AITraderState)

# Add agent nodes
builder.add_node("main_agent", call_main_agent)
builder.add_node("reviewer", call_reviewer_agent)

# Start with conditional routing (usually main_agent)
builder.add_conditional_edges(START, route_initial, ["main_agent", "reviewer"])

# After each agent, check if we should end or route to another
builder.add_conditional_edges(
    "main_agent", route_after_agent, ["main_agent", "reviewer", END]
)
builder.add_conditional_edges(
    "reviewer", route_after_agent, ["main_agent", "reviewer", END]
)

# Compile the parent graph
graph = builder.compile(name="AI Trader")
