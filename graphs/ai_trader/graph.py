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

import os
from typing import Literal

import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime
from langgraph.types import dispatch_custom_event

from ai_trader.context import Context
from ai_trader.state import InputState, State
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


async def fetch_agent_config(
    state: State, *, runtime: Runtime[Context]
) -> dict:
    """
    Fetch agent configuration from the database and update runtime context.

    This runs at the start of each conversation to load custom prompts,
    model settings, and feature flags from the agent_configs table.
    """
    ctx = runtime.context

    # Only fetch if we have a user_id
    if not ctx.user_id:
        logger.info("No user_id in context, using default config")
        return {}

    try:
        client = SupabaseClient(use_service_role=True)

        # Fetch agent config for this user
        configs = await client.select(
            "agent_configs",
            {
                "select": "*",
                "user_id": f"eq.{ctx.user_id}",
                "is_active": "eq.true",
                "limit": "1",
            },
        )

        if configs:
            config = configs[0]
            logger.info(
                "Loaded agent config",
                config_id=config.get("id"),
                user_id=ctx.user_id,
            )

            # Emit event for the frontend
            dispatch_custom_event(
                "agent_config_loaded",
                {
                    "config_id": config.get("id"),
                    "model": config.get("model"),
                    "has_custom_prompt": bool(config.get("system_prompt")),
                },
            )

    except Exception as e:
        logger.warning("Failed to fetch agent config", error=str(e))

    return {}


async def subconscious_node(
    state: State, *, runtime: Runtime[Context]
) -> dict:
    """
    Subconscious injection - retrieves relevant skills and behaviors.

    This node runs before the main agent to inject context from:
    - Agent memories (long-term storage)
    - Algorithm knowledge base (RAG search)
    - User preferences and past interactions
    """
    ctx = runtime.context

    if not ctx.subconscious_enabled:
        logger.debug("Subconscious disabled, skipping")
        return {}

    try:
        # Emit thinking event for UI
        dispatch_custom_event("subconscious_thinking", {"stage": "planning"})

        # Get the last user message for context
        last_message = None
        for msg in reversed(state.messages):
            if hasattr(msg, "type") and msg.type == "human":
                last_message = msg.content if hasattr(msg, "content") else str(msg)
                break

        if not last_message:
            dispatch_custom_event("subconscious_thinking", {"stage": "done"})
            return {}

        # TODO: Implement actual subconscious retrieval
        # For now, this is a placeholder that will be filled with:
        # 1. Memory retrieval from agent_memories table
        # 2. RAG search over algorithm_knowledge_base
        # 3. Skill injection based on conversation context

        dispatch_custom_event("subconscious_thinking", {"stage": "done"})

        # Return any injected context as a system message
        # For now, return empty - the full implementation would return:
        # return {"subconscious_context": "Retrieved context here..."}
        return {}

    except Exception as e:
        logger.warning("Subconscious injection failed", error=str(e))
        dispatch_custom_event("subconscious_thinking", {"stage": "done"})
        return {}


async def call_model(state: State, *, runtime: Runtime[Context]) -> dict:
    """
    Main agent node - invokes the LLM with tools.

    Uses the system prompt from context and all available tools.
    Handles extended thinking if configured.
    """
    ctx = runtime.context

    # Build system message
    system_content = ctx.system_prompt
    if state.subconscious_context:
        system_content += f"\n\n<injected_context>\n{state.subconscious_context}\n</injected_context>"

    # Create the model
    model_name = ctx.model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

    model_kwargs = {
        "model": model_name,
        "api_key": os.environ.get("ANTHROPIC_API_KEY"),
        "max_tokens": 8192,
    }

    # Add extended thinking if configured
    if ctx.thinking_budget > 0:
        model_kwargs["thinking"] = {
            "type": "enabled",
            "budget_tokens": ctx.thinking_budget,
        }

    model = ChatAnthropic(**model_kwargs)

    # Bind tools to model
    model_with_tools = model.bind_tools(ALL_TOOLS)

    # Build messages
    messages = [SystemMessage(content=system_content)] + list(state.messages)

    # Invoke the model
    response = await model_with_tools.ainvoke(messages)

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

# Add nodes
builder.add_node("fetch_config", fetch_agent_config)
builder.add_node("subconscious", subconscious_node)
builder.add_node("call_model", call_model)
builder.add_node("tools", ToolNode(ALL_TOOLS))

# Add edges
builder.add_edge("__start__", "fetch_config")
builder.add_edge("fetch_config", "subconscious")
builder.add_edge("subconscious", "call_model")
builder.add_conditional_edges("call_model", route_after_model)
builder.add_edge("tools", "call_model")

# Compile the graph
graph = builder.compile(name="Shooby Dooby")
