"""
Doubtful Deacon - Code Review Subgraph

A skeptical reviewer agent that critiques algorithm code and provides feedback.
Runs as a subgraph so its messages stream to the UI with its own namespace.
"""

import os

import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy

from .context import Context
from .prompts import DEFAULT_REVIEWER_PROMPT
from .state import InputState, State

logger = structlog.getLogger(__name__)


async def review_node(state: State, *, runtime: Runtime[Context]) -> dict:
    """
    Reviewer agent node - analyzes the conversation and provides critique.

    The reviewer sees the full message history and responds with its analysis.
    Its response is added to the shared messages state.
    """
    ctx = runtime.context

    # Get reviewer configuration
    reviewer_model = ctx.reviewer_model or os.environ.get(
        "ANTHROPIC_MODEL", "claude-sonnet-4-20250514"
    )
    reviewer_prompt = ctx.reviewer_prompt or DEFAULT_REVIEWER_PROMPT
    reviewer_thinking_budget = ctx.reviewer_thinking_budget or 0

    # Build the model
    is_claude = reviewer_model.startswith("claude")

    if is_claude:
        model_kwargs = {
            "model": reviewer_model,
            "api_key": os.environ.get("ANTHROPIC_API_KEY"),
            "max_tokens": 4096,
        }

        if reviewer_thinking_budget > 0:
            model_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": reviewer_thinking_budget,
            }
            model_kwargs["max_tokens"] = max(4096, reviewer_thinking_budget + 2048)

        model = ChatAnthropic(**model_kwargs)
    else:
        model_kwargs = {
            "model": reviewer_model,
            "api_key": os.environ.get("OPENAI_API_KEY"),
            "max_tokens": 4096,
        }
        model = ChatOpenAI(**model_kwargs)

    # Build messages - reviewer sees the full conversation history
    messages = [SystemMessage(content=reviewer_prompt)] + list(state.messages)

    # Invoke the model
    response = await model.ainvoke(messages)

    return {"messages": [response]}


# Build the reviewer subgraph
reviewer_builder = StateGraph(
    State,
    input_schema=InputState,
    context_schema=Context,
)

# Retry policy for LLM calls - handles transient API errors
llm_retry_policy = RetryPolicy(
    max_attempts=3,
    initial_interval=2.0,
    backoff_factor=2.0,
    max_interval=30.0,
)

reviewer_builder.add_node("review", review_node, retry=llm_retry_policy)
reviewer_builder.add_edge("__start__", "review")
reviewer_builder.add_edge("review", "__end__")

# Compile with name for UI display
reviewer_graph = reviewer_builder.compile(name="Doubtful Deacon")
