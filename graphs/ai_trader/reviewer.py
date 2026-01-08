"""
Doubtful Deacon - Chief Quant Strategist & Algorithm Auditor

A full ReAct agent that can run backtests, analyze results, and iterate on improvements.
Uses create_agent for proper tool execution loop - not just suggestions.
"""

import os
from typing import Callable

import structlog
from langchain.agents import create_agent
from langchain.agents.middleware import (
    dynamic_prompt,
    wrap_model_call,
    ModelRequest,
    ModelResponse,
    AgentState,
)
from langchain.chat_models import init_chat_model
from langchain.messages import SystemMessage

from .context import Context
from .prompts import DEFAULT_REVIEWER_PROMPT

# Import tools for the reviewer
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
    list_optimizations,
    read_backtest_orders,
)

logger = structlog.getLogger(__name__)

# Reviewer tools - core subset for analysis and testing
REVIEWER_TOOLS = [
    qc_read_file,
    qc_edit_and_run_backtest,
    qc_update_and_run_backtest,
    qc_compile_and_backtest,
    get_code_versions,
    get_code_version,
    read_backtest,
    read_backtest_orders,
    read_project_nodes,
    read_optimization,
    list_optimizations,
    list_backtests,
]


# =============================================================================
# Middleware: Dynamic Model Selection for Reviewer
# =============================================================================

@wrap_model_call
async def reviewer_model_selection(
    request: ModelRequest,
    handler: Callable[[ModelRequest], ModelResponse],
) -> ModelResponse:
    """Select reviewer model dynamically based on context."""
    ctx = request.runtime.context or {}
    
    # Priority: context override > env var > default fine-tuned model
    default_model = os.environ.get(
        "REVIEWER_MODEL", 
        "ft:gpt-4.1-mini-2025-04-14:chemular-inc:fin:CvDjVD7Q"
    )
    model_name = ctx.get("reviewer_model") or default_model
    
    # Determine model provider - fine-tuned models need explicit provider
    if model_name.startswith("ft:") or model_name.startswith("gpt") or model_name.startswith("o1") or model_name.startswith("o3"):
        model_provider = "openai"
    elif model_name.startswith("claude"):
        model_provider = "anthropic"
    else:
        model_provider = None  # Let init_chat_model infer
    
    model = init_chat_model(model_name, model_provider=model_provider)
    
    # Apply Claude thinking budget if set
    if model_name.startswith("claude"):
        thinking_budget = ctx.get("reviewer_thinking_budget") or 0
        if thinking_budget > 0:
            model = model.bind(
                thinking={"type": "enabled", "budget_tokens": thinking_budget}
            )
    
    # Bind tools to model
    model = model.bind_tools(REVIEWER_TOOLS)
    
    # Update request with new model
    request.model = model
    return await handler(request)


# =============================================================================
# Middleware: Dynamic System Prompt for Reviewer
# =============================================================================

@dynamic_prompt
async def reviewer_system_prompt(
    state: AgentState,
    runtime,
) -> SystemMessage:
    """Build the reviewer system prompt."""
    ctx = runtime.context or {}
    prompt = ctx.get("reviewer_prompt") or DEFAULT_REVIEWER_PROMPT
    return SystemMessage(content=prompt)


# =============================================================================
# Create Reviewer Agent with Full ReAct Loop
# =============================================================================

# Default reviewer model
DEFAULT_REVIEWER_MODEL = os.environ.get(
    "REVIEWER_MODEL",
    "ft:gpt-4.1-mini-2025-04-14:chemular-inc:fin:CvDjVD7Q"
)

# Determine model provider for default model
if DEFAULT_REVIEWER_MODEL.startswith("ft:") or DEFAULT_REVIEWER_MODEL.startswith("gpt"):
    _MODEL_PROVIDER = "openai"
else:
    _MODEL_PROVIDER = None

# Create reviewer agent with tools and ReAct loop
reviewer_graph = create_agent(
    model=DEFAULT_REVIEWER_MODEL,
    model_provider=_MODEL_PROVIDER,
    tools=REVIEWER_TOOLS,
    state_schema=AgentState,
    context_schema=Context,
    middleware=[
        reviewer_model_selection,
        reviewer_system_prompt,
    ],
    name="Doubtful_Deacon",
)

