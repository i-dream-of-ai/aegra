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
    # Get reviewer configuration
    # Priority: 1. Context (User override), 2. REVIEWER_MODEL env var, 3. Default fallback (Fine-Tuned)
    default_model = os.environ.get("REVIEWER_MODEL", "ft:gpt-4.1-mini-2025-04-14:chemular-inc:fin:CvDjVD7Q")
    
    reviewer_model = ctx.get("reviewer_model") or default_model
    reviewer_prompt = ctx.get("reviewer_prompt") or DEFAULT_REVIEWER_PROMPT
    reviewer_thinking_budget = ctx.get("reviewer_thinking_budget") or 0

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

    # Import and bind tools - gives Deacon the ability to run backtests
    from .tools import (
        qc_read_file,
        qc_edit_and_run_backtest,
        qc_update_and_run_backtest,
        qc_compile_and_backtest,
        get_code_versions,
        get_code_version,
        read_backtest,
        read_project_nodes,
        read_optimization,
        list_backtests,
    )
    
    reviewer_tools = [
        qc_read_file,
        qc_edit_and_run_backtest,
        qc_update_and_run_backtest,
        qc_compile_and_backtest,
        get_code_versions,
        get_code_version,
        read_backtest,
        read_project_nodes,
        read_optimization,
        list_backtests,
    ]
    
    # Bind tools to model
    model_with_tools = model.bind_tools(reviewer_tools)

    # Build messages - reviewer sees the full conversation history
    # Sanitize message names for OpenAI compatibility (OpenAI rejects spaces and certain chars)
    def sanitize_name(name: str) -> str:
        if not name:
            return name
        # Replace spaces and invalid chars: \s, <, |, \, /, >
        import re
        return re.sub(r'[\s<|\\/>\(\)\[\]\{\}]', '_', name)
    
    # Filter out tool-related messages - OpenAI requires tool_calls to have matching
    # tool responses, but the reviewer is a separate model call that doesn't have them
    from langchain_core.messages import ToolMessage
    
    sanitized_messages = []
    for msg in state.messages:
        # Skip ToolMessage (tool responses)
        if isinstance(msg, ToolMessage):
            continue
        # Skip AIMessage with tool_calls 
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            continue
        
        if hasattr(msg, 'name') and msg.name:
            # Create a copy with sanitized name
            msg_copy = msg.model_copy()
            msg_copy.name = sanitize_name(msg.name)
            sanitized_messages.append(msg_copy)
        else:
            sanitized_messages.append(msg)
    
    messages = [SystemMessage(content=reviewer_prompt)] + sanitized_messages

    # Invoke the model with tools bound
    response = await model_with_tools.ainvoke(messages)

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
reviewer_graph = reviewer_builder.compile(name="Doubtful_Deacon")
