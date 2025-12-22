"""
AI Trader Agent - Standard LangGraph StateGraph Implementation

This graph implements a ReAct-style agent for QuantConnect algorithm development.
It uses the standard StateGraph pattern from LangGraph with:
- Context schema for runtime configuration
- State schema for graph state management
- Subconscious injection for dynamic skill/behavior loading
- HITL interrupts for user confirmation
"""

from __future__ import annotations

import contextlib
import os
from typing import Literal

import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.config import get_stream_writer
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy

from ai_trader.context import Context

# Import the reviewer subgraph
from ai_trader.reviewer import reviewer_graph
from ai_trader.state import InputState, State

# Import subconscious components
from ai_trader.subconscious.middleware import SubconsciousMiddleware
from ai_trader.subconscious.types import SubconsciousEvent  # noqa: TC001
from ai_trader.supabase_client import SupabaseClient

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


def repair_dangling_tool_calls(messages: list) -> list:
    """
    Repair message history when tool calls are interrupted or cancelled before receiving results.

    The problem:
    - Agent requests tool call: "Please run X"
    - Tool call is interrupted (user cancels, network error, crash, etc.)
    - Agent sees tool_call in AIMessage but no corresponding ToolMessage
    - This creates an invalid message sequence that Claude API rejects

    The solution:
    - Detects AIMessages with tool_calls that have no results
    - Creates synthetic ToolMessage responses indicating the call was cancelled
    - Repairs the message history before agent execution

    Why it's useful:
    - Prevents "tool_use without tool_result" API errors
    - Gracefully handles interruptions and errors
    - Maintains conversation coherence
    """
    if not messages:
        return messages

    repaired = []
    i = 0

    while i < len(messages):
        msg = messages[i]
        repaired.append(msg)

        # Check if this is an AIMessage with tool_calls
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            tool_call_ids = {tc.get("id") or tc.get("tool_call_id") for tc in msg.tool_calls if tc}

            # Look ahead to find which tool_calls have results
            j = i + 1
            found_result_ids = set()
            while j < len(messages):
                next_msg = messages[j]
                if isinstance(next_msg, ToolMessage):
                    found_result_ids.add(next_msg.tool_call_id)
                elif hasattr(next_msg, "tool_calls") and next_msg.tool_calls:
                    # Hit another AI message with tool calls, stop looking
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
                            content=f"Tool call was interrupted or cancelled before completion. The tool '{tc_name}' did not return a result.",
                            tool_call_id=tc_id,
                        )
                    )

        i += 1

    return repaired


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


async def fetch_agent_config(state: State, *, runtime: Runtime[Context]) -> dict:
    """
    Fetch agent configuration from the database and update runtime context.

    This runs at the start of each run to load the project's AI settings
    from the projects table, including model, thinking_budget, and agent_config JSONB.
    """
    ctx = runtime.context

    # Must have project_db_id to fetch config
    if not ctx.project_db_id:
        logger.info("No project_db_id in context, using default config")
        return {}

    try:
        client = SupabaseClient(use_service_role=True)

        # Fetch project with its agent_config and AI settings
        projects = await client.select(
            "projects",
            {
                "select": "id,ai_model,thinking_budget,reasoning_effort,text_verbosity,max_output_tokens,agent_config",
                "id": f"eq.{ctx.project_db_id}",
                "limit": "1",
            },
        )

        if not projects:
            logger.warning("Project not found", project_db_id=ctx.project_db_id)
            return {}

        project = projects[0]
        agent_config = project.get("agent_config") or {}

        # Get main agent (Shooby Dooby) settings from JSONB
        main_config = agent_config.get("main", {})

        # Apply main agent settings to context (JSONB overrides project-level settings)
        if main_config.get("model"):
            ctx.model = main_config["model"]
        elif project.get("ai_model"):
            ctx.model = project["ai_model"]

        if main_config.get("thinkingBudget") is not None:
            ctx.thinking_budget = main_config["thinkingBudget"]
        elif project.get("thinking_budget") is not None:
            ctx.thinking_budget = project["thinking_budget"]

        if main_config.get("verbosity"):
            ctx.verbosity = main_config["verbosity"]
        elif project.get("text_verbosity"):
            ctx.verbosity = project["text_verbosity"]

        # Reasoning effort for GPT models
        if main_config.get("reasoningEffort"):
            ctx.reasoning_effort = main_config["reasoningEffort"]
        elif project.get("reasoning_effort"):
            ctx.reasoning_effort = project["reasoning_effort"]

        # Custom system prompt override
        if main_config.get("systemPrompt"):
            ctx.system_prompt = main_config["systemPrompt"]

        # Subconscious toggle (default true, can be disabled in UI)
        if main_config.get("subconsciousEnabled") is False:
            ctx.subconscious_enabled = False

        # Get reviewer agent (Doubtful Deacon) settings
        reviewer_config = agent_config.get("reviewer", {})

        if reviewer_config.get("model"):
            ctx.reviewer_model = reviewer_config["model"]

        if reviewer_config.get("thinkingBudget") is not None:
            ctx.reviewer_thinking_budget = reviewer_config["thinkingBudget"]

        if reviewer_config.get("reasoningEffort"):
            ctx.reviewer_reasoning_effort = reviewer_config["reasoningEffort"]

        if reviewer_config.get("systemPrompt"):
            ctx.reviewer_prompt = reviewer_config["systemPrompt"]

        logger.info(
            "Loaded project config",
            project_id=ctx.project_db_id,
            model=ctx.model,
            thinking_budget=ctx.thinking_budget,
            reviewer_model=ctx.reviewer_model,
        )

    except Exception as e:
        logger.warning("Failed to fetch agent config", error=str(e))

    return {}


async def subconscious_node(state: State, *, runtime: Runtime[Context]) -> dict:
    """
    Subconscious injection - retrieves relevant skills and behaviors.

    This node runs ONCE at the start of each run to inject context from:
    - Agent memories (long-term storage)
    - Algorithm knowledge base (RAG search)
    - User preferences and past interactions

    Emits SSE events for UI progress indicator:
    - subconscious_thinking: {stage: "planning"|"retrieving"|"synthesizing"|"done"}
    - instinct_injection: {skillIds, tokenCount, driftScore, synthesisMethod}
    """
    ctx = runtime.context

    if not ctx.subconscious_enabled:
        logger.debug("Subconscious disabled, skipping")
        return {}

    # Get access token for DB queries
    access_token = ctx.access_token
    if not access_token:
        logger.debug("No access token, skipping subconscious")
        return {}

    try:
        # Get stream writer for custom events
        writer = get_stream_writer()

        # Create middleware with event dispatcher
        def emit_event(event: SubconsciousEvent):
            """Write event to LangGraph stream.

            Frontend type guards expect:
            - subconscious_thinking: {type: 'subconscious_thinking', stage: '...'}
            - instinct_injection: {type: 'instinct_injection', data: {...}}
            """
            if event.type == "instinct_injection":
                writer({"type": event.type, "data": event.data or {}})
            else:
                writer({"type": event.type, "stage": event.stage})

        middleware = SubconsciousMiddleware(on_event=emit_event)

        # Process messages and get context to inject
        # current_turn=0 ensures it always runs (rate limiting is per-run, not per-turn)
        injected_context = await middleware.process(
            messages=list(state.messages),
            access_token=access_token,
            current_turn=0,
        )

        if injected_context:
            logger.info(
                "Subconscious injected context",
                token_count=len(injected_context.split()),
            )
            return {"subconscious_context": injected_context}

        return {}

    except Exception as e:
        logger.warning("Subconscious injection failed", error=str(e))
        # Emit done event even on failure so UI doesn't hang
        with contextlib.suppress(Exception):
            writer = get_stream_writer()
            writer({"type": "subconscious_thinking", "stage": "done"})
        return {}


def create_model_with_tools(
    model_name: str,
    thinking_budget: int = 0,
    reasoning_effort: str | None = None,
) -> tuple:
    """
    Create a model instance with tools bound.

    Returns (model_with_tools, is_claude) tuple.
    """
    is_claude = model_name.startswith("claude")

    if is_claude:
        base_max_tokens = 8192
        model_kwargs = {
            "model": model_name,
            "api_key": os.environ.get("ANTHROPIC_API_KEY"),
        }

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

        if reasoning_effort and reasoning_effort != "none":
            model_kwargs["reasoning_effort"] = reasoning_effort

        model = ChatOpenAI(**model_kwargs)

    return model.bind_tools(ALL_TOOLS), is_claude


# Default fallback chain: if primary fails, try these in order
# Claude Sonnet is fast and capable, GPT-4o is a good cross-provider fallback
DEFAULT_FALLBACK_MODELS = [
    "claude-sonnet-4-20250514",
    "gpt-4o",
]


async def call_model(state: State, *, runtime: Runtime[Context]) -> dict:
    """
    Main agent node - invokes the LLM with tools.

    Uses the system prompt from context and all available tools.
    Handles extended thinking if configured.
    Implements model fallback: if primary model fails, tries fallback models.
    """
    ctx = runtime.context

    # Build system message
    system_content = ctx.system_prompt
    if state.subconscious_context:
        system_content += (
            f"\n\n<injected_context>\n{state.subconscious_context}\n</injected_context>"
        )

    # Build messages - repair any dangling tool calls from interrupted sessions
    repaired_messages = repair_dangling_tool_calls(list(state.messages))
    messages = [SystemMessage(content=system_content)] + repaired_messages

    # Get primary model
    primary_model = ctx.model or os.environ.get(
        "ANTHROPIC_MODEL", "claude-opus-4-5-20251101"
    )

    # Build model chain: primary + fallbacks
    models_to_try = [primary_model] + DEFAULT_FALLBACK_MODELS

    last_error = None
    for i, model_name in enumerate(models_to_try):
        try:
            model_with_tools, is_claude = create_model_with_tools(
                model_name,
                thinking_budget=ctx.thinking_budget if i == 0 else 0,  # Only use thinking on primary
                reasoning_effort=ctx.reasoning_effort if i == 0 else None,
            )

            if i > 0:
                logger.warning(
                    "Falling back to alternative model",
                    primary_model=primary_model,
                    fallback_model=model_name,
                    attempt=i + 1,
                )

            # Invoke the model
            response = await model_with_tools.ainvoke(messages)

            if i > 0:
                logger.info(
                    "Fallback model succeeded",
                    model=model_name,
                )

            break  # Success, exit the loop

        except Exception as e:
            last_error = e
            error_str = str(e).lower()

            # Only fallback on transient/capacity errors, not on invalid requests
            is_transient = any(
                term in error_str
                for term in [
                    "overloaded",
                    "rate limit",
                    "capacity",
                    "timeout",
                    "503",
                    "529",
                    "500",
                    "connection",
                ]
            )

            if not is_transient:
                logger.error(
                    "Model error is not transient, not falling back",
                    model=model_name,
                    error=str(e),
                )
                raise

            logger.warning(
                "Model call failed, will try fallback",
                model=model_name,
                error=str(e),
                attempt=i + 1,
                remaining_fallbacks=len(models_to_try) - i - 1,
            )

            if i == len(models_to_try) - 1:
                # No more fallbacks, raise the last error
                logger.error("All models failed", primary=primary_model)
                raise last_error from e

    # Check for recursion limit
    if state.is_last_step and response.tool_calls:
        logger.warning("Recursion limit reached, forcing end")
        return {
            "messages": [
                AIMessage(
                    content="I've reached the maximum number of steps. Please review the results and let me know if you need me to continue.",
                    id=response.id,
                )
            ]
        }

    return {"messages": [response]}


def route_after_model(
    state: State,
) -> Literal["tools", "__end__"]:
    """
    Route after model call - either to tools or end.

    Returns 'tools' if the model made tool calls, otherwise '__end__'.
    """
    last_message = state.messages[-1]

    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"

    return "__end__"


# Build the graph
builder = StateGraph(
    State,
    input_schema=InputState,
    context_schema=Context,
)

# Retry policy for LLM calls - handles transient API errors (overloaded, rate limits, etc.)
llm_retry_policy = RetryPolicy(
    max_attempts=3,
    initial_interval=2.0,  # Start with 2 second delay
    backoff_factor=2.0,  # Exponential backoff: 2s, 4s, 8s
    max_interval=30.0,  # Cap at 30 seconds
)

# Add nodes
builder.add_node("fetch_config", fetch_agent_config)
builder.add_node("subconscious", subconscious_node)
builder.add_node("call_model", call_model, retry=llm_retry_policy)
builder.add_node("tools", ToolNode(ALL_TOOLS))
builder.add_node("reviewer", reviewer_graph)  # Doubtful Deacon subgraph

# Add edges
builder.add_edge("__start__", "fetch_config")
builder.add_edge("fetch_config", "subconscious")
builder.add_edge("subconscious", "call_model")
builder.add_conditional_edges("call_model", route_after_model)
builder.add_edge("tools", "call_model")
builder.add_edge("reviewer", "call_model")  # After review, main agent responds

# Compile the graph
graph = builder.compile(name="Shooby Dooby")
