"""
AI Trader Agent - StateGraph with Subconscious Pre-processing

Architecture:
    START -> subconscious -> agent -> END

1. Subconscious Node: Runs ONCE at graph start to analyze intent and inject skills
   - Streams progress events via custom stream mode (planning, retrieving, synthesizing)
   - Emits instinct_injection event with selected skills
   - Updates state with subconscious_context for the agent's system prompt

2. Agent Node: The main create_agent loop with all tools and middleware
   - Uses @dynamic_prompt to inject subconscious context into system prompt
   - Uses @wrap_model_call for dynamic model selection
   - Generative UI via push_ui_message for custom components
"""

import os
from datetime import UTC, datetime
import typing
from typing import Any, Callable, Sequence

import structlog
from langchain.agents import create_agent
from langchain.agents.middleware import (
    dynamic_prompt,
    wrap_model_call,
    before_model,
    ModelRequest,
    ModelResponse,
    AgentState,
    AgentMiddleware,
    SummarizationMiddleware,
    TodoListMiddleware,
    ContextEditingMiddleware,
    ClearToolUsesEdit,
    ModelCallLimitMiddleware,
)
# Import deepagents middleware (but not create_deep_agent - we use create_agent directly)
from deepagents.middleware.subagents import SubAgentMiddleware
# Note: AnthropicPromptCachingMiddleware removed - conflicts with OpenAI summarization model
from langchain_openai import ChatOpenAI
from langchain_core.messages import ToolMessage, RemoveMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.ui import AnyUIMessage, ui_message_reducer

from graphs.ai_trader.context import Context
from graphs.ai_trader.prompts import DEFAULT_MAIN_PROMPT
from graphs.ai_trader.nodes.subconscious import subconscious_node
from graphs.ai_trader.config import DEFAULT_MODEL as CONFIG_DEFAULT_MODEL

# Import all tools
from graphs.ai_trader.tools.ai_services import TOOLS as AI_SERVICES_TOOLS
from graphs.ai_trader.tools.backtest import TOOLS as BACKTEST_TOOLS
from graphs.ai_trader.tools.compile import TOOLS as COMPILE_TOOLS
from graphs.ai_trader.tools.composite import TOOLS as COMPOSITE_TOOLS
from graphs.ai_trader.tools.files import TOOLS as FILES_TOOLS
from graphs.ai_trader.tools.misc import TOOLS as MISC_TOOLS
from graphs.ai_trader.tools.object_store import TOOLS as OBJECT_STORE_TOOLS
from graphs.ai_trader.tools.optimization import TOOLS as OPTIMIZATION_TOOLS
# Import reviewer prompt for subagent configuration
from graphs.ai_trader.prompts import DEFAULT_REVIEWER_PROMPT

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
# Middleware: Generative UI State
# =============================================================================
# NOTE: When using create_agent() with middleware, the state_schema parameter
# is ignored. State extensions must be registered via middleware's state_schema.
# See: https://github.com/langchain-ai/langchain/issues/33217


class GenerativeUIMiddleware(AgentMiddleware[AITraderState]):
    """Middleware that registers the ui field in agent state for generative UI.

    This middleware doesn't have any hooks - its only purpose is to register
    the AITraderState schema which includes the `ui` field with ui_message_reducer.
    Without this, push_ui_message() calls won't persist across checkpoints.
    """
    state_schema = AITraderState


# =============================================================================
# Middleware: Dynamic Model Selection
# =============================================================================


@wrap_model_call
async def dynamic_model_selection(
    request: ModelRequest,
    handler: Callable[[ModelRequest], ModelResponse],
) -> ModelResponse:
    """Select model dynamically based on runtime context."""
    from langchain_anthropic import ChatAnthropic

    ctx = request.runtime.context or {}
    model_name = ctx.get("model", os.environ.get("DEFAULT_MODEL", CONFIG_DEFAULT_MODEL))

    # Log message structure before sending to model (INFO level for visibility)
    logger.info(
        "Messages before model call",
        model=model_name,
        message_count=len(request.messages),
    )
    for i, msg in enumerate(request.messages):
        msg_type = getattr(msg, "type", "unknown")
        tool_calls = getattr(msg, "tool_calls", None)
        tool_call_id = getattr(msg, "tool_call_id", None)
        msg_id = getattr(msg, "id", None)
        content_preview = str(getattr(msg, "content", ""))[:100]
        logger.info(
            f"Message {i}",
            msg_type=msg_type,
            msg_id=msg_id,
            tool_call_id=tool_call_id,
            tool_calls=[tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None) for tc in (tool_calls or [])],
            content_preview=content_preview,
        )

    # Initialize model based on name - use explicit class to ensure proper type detection
    is_claude = model_name.startswith("claude")
    # Get retry config - default to 3 retries for transient errors (overloaded, rate limits)
    max_retries = ctx.get("max_retries", 3)

    if is_claude:
        model = ChatAnthropic(model=model_name, max_retries=max_retries)
        thinking_budget = ctx.get("thinking_budget") or 0
        if thinking_budget > 0:
            model = model.bind(
                thinking={"type": "enabled", "budget_tokens": thinking_budget}
            )
    else:
        # Default to OpenAI for all other models
        model = ChatOpenAI(model=model_name, max_retries=max_retries)
        reasoning_effort = ctx.get("reasoning_effort")
        if reasoning_effort and reasoning_effort != "none":
            model = model.bind(reasoning_effort=reasoning_effort)

        # OpenAI has strict validation on message names - sanitize them
        # Pattern: ^[^\s<|\\/\>]+$ (no whitespace, <, |, \, /, >)
        import re
        def sanitize_name(name: str) -> str:
            if not name:
                return name
            return re.sub(r'[\s<|\\/\>]', '_', name)

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

    logger.info("Dynamic model selection", model=model_name, model_type=type(model).__name__)
    return await handler(request.override(model=model))


# =============================================================================
# Middleware: Dynamic System Prompt with Subconscious Injection
# =============================================================================


@dynamic_prompt
def build_system_prompt(state: AITraderState, *args, **kwargs) -> str:
    """Build system prompt with timestamp and subconscious context.

    The subconscious_context comes from the subconscious node that ran before
    the agent. It's stored in state by the subconscious node, and we also
    check runtime context for backwards compatibility.
    """
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

    # Get subconscious context - first from state (set by subconscious node),
    # then fall back to runtime context for backwards compatibility
    subconscious = state.get("subconscious_context") if isinstance(state, dict) else None
    if not subconscious:
        subconscious = ctx.get("subconscious_context")

    if subconscious:
        prompt += f"\n\n<injected_context>\n{subconscious}\n</injected_context>"

    return prompt


# =============================================================================
# Middleware: Patch Dangling Tool Calls
# =============================================================================


@before_model
def patch_dangling_tool_calls(state: AITraderState, *args, **kwargs) -> dict[str, Any] | None:
    """Patch dangling tool calls AND remove orphan tool results before model invocation.

    Handles two cases:
    1. Dangling tool calls: AI message has tool_calls but no corresponding ToolMessage
       -> Add synthetic ToolMessage with "interrupted" content
    2. Orphan tool results: ToolMessage exists but no AI message has matching tool_call
       -> Use RemoveMessage to delete the orphan (add_messages reducer requires this)

    Note: The messages field uses add_messages reducer which is append-only by default.
    To actually remove messages, we must use RemoveMessage with the message's ID.
    """
    messages = list(state["messages"])
    if not messages:
        return None

    # Build set of all tool_call IDs from AI messages
    valid_tool_call_ids = set()
    for msg in messages:
        if getattr(msg, "type", None) == "ai" and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tc_id:
                    valid_tool_call_ids.add(tc_id)

    # Find orphan tool results and dangling tool calls
    updates = []
    tool_result_ids_seen = set()

    for msg in messages:
        msg_type = getattr(msg, "type", None)

        if msg_type == "tool":
            tool_call_id = getattr(msg, "tool_call_id", None)
            if tool_call_id:
                tool_result_ids_seen.add(tool_call_id)
                # Check if this is an orphan (no matching tool_call in any AI message)
                if tool_call_id not in valid_tool_call_ids:
                    msg_id = getattr(msg, "id", None)
                    if msg_id:
                        logger.warning("Removing orphan tool result", tool_call_id=tool_call_id, msg_id=msg_id)
                        updates.append(RemoveMessage(id=msg_id))
                    else:
                        # If message has no ID, we can't remove it with RemoveMessage
                        # Log and hope the model handles it (shouldn't happen normally)
                        logger.error("Orphan tool result has no ID, cannot remove", tool_call_id=tool_call_id)

        elif msg_type == "ai" and getattr(msg, "tool_calls", None):
            # Check for dangling tool calls (tool_call with no result)
            for tc in msg.tool_calls:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tc_id and tc_id not in tool_result_ids_seen:
                    # Check if any later message has this result
                    has_result = any(
                        getattr(m, "type", None) == "tool" and getattr(m, "tool_call_id", None) == tc_id
                        for m in messages
                    )
                    if not has_result:
                        tc_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "unknown")
                        logger.warning("Patching dangling tool call", tool_call_id=tc_id)
                        updates.append(
                            ToolMessage(
                                content=f"Tool {tc_name} was interrupted/cancelled.",
                                name=tc_name,
                                tool_call_id=tc_id,
                            )
                        )

    if updates:
        return {"messages": updates}
    return None


# =============================================================================
# Reviewer Middleware: Dynamic Model Selection
# =============================================================================


@wrap_model_call
async def reviewer_dynamic_model_selection(
    request: ModelRequest,
    handler: Callable[[ModelRequest], ModelResponse],
) -> ModelResponse:
    """Select reviewer model dynamically based on runtime context."""
    from langchain_anthropic import ChatAnthropic

    ctx = request.runtime.context or {}
    model_name = ctx.get("reviewer_model", os.environ.get("REVIEWER_MODEL", "gpt-4o-mini"))
    max_retries = ctx.get("reviewer_max_retries", 3)

    is_claude = model_name.startswith("claude")

    if is_claude:
        model = ChatAnthropic(model=model_name, max_retries=max_retries)
        thinking_budget = ctx.get("reviewer_thinking_budget") or 0
        if thinking_budget > 0:
            model = model.bind(
                thinking={"type": "enabled", "budget_tokens": thinking_budget}
            )
    else:
        model = ChatOpenAI(model=model_name, max_retries=max_retries)
        reasoning_effort = ctx.get("reviewer_reasoning_effort")
        if reasoning_effort and reasoning_effort != "none":
            model = model.bind(reasoning_effort=reasoning_effort)

        # Sanitize message names for OpenAI
        import re
        def sanitize_name(name: str) -> str:
            if not name:
                return name
            return re.sub(r'[\s<|\\/\>]', '_', name)

        sanitized_messages = []
        for msg in request.messages:
            if hasattr(msg, 'name') and msg.name:
                msg_copy = msg.model_copy()
                msg_copy.name = sanitize_name(msg.name)
                sanitized_messages.append(msg_copy)
            else:
                sanitized_messages.append(msg)

        request = request.override(messages=sanitized_messages)

    logger.info("Reviewer dynamic model selection", model=model_name, model_type=type(model).__name__)
    return await handler(request.override(model=model))


# =============================================================================
# Reviewer Middleware: Dynamic System Prompt
# =============================================================================


@dynamic_prompt
def reviewer_build_system_prompt(state: dict, *args, **kwargs) -> str:
    """Build reviewer system prompt from runtime context."""
    ctx = {}
    if args:
        arg0 = args[0]
        if hasattr(arg0, "runtime") and hasattr(arg0.runtime, "context"):
            ctx = arg0.runtime.context or {}
        elif hasattr(arg0, "get"):
            ctx = arg0
        elif hasattr(arg0, "context"):
            ctx = arg0.context or {}

    prompt = ctx.get("reviewer_prompt") or DEFAULT_REVIEWER_PROMPT
    logger.info("Reviewer system prompt source", from_db=bool(ctx.get("reviewer_prompt")))
    return prompt


# =============================================================================
# Create Agent (inner loop) with Middleware and Subagents
# =============================================================================

# Default model from environment with custom profile for 100k summarization trigger
# create_deep_agent uses 85% of max_input_tokens as trigger, so 118k -> ~100k trigger
DEFAULT_MODEL_NAME = os.environ.get("DEFAULT_MODEL", CONFIG_DEFAULT_MODEL)

# Create the appropriate model class based on model name
# Note: This is just a placeholder - dynamic_model_selection middleware handles actual model creation
if DEFAULT_MODEL_NAME.startswith("claude"):
    from langchain_anthropic import ChatAnthropic
    DEFAULT_MODEL = ChatAnthropic(
        model=DEFAULT_MODEL_NAME,
    )
else:
    DEFAULT_MODEL = ChatOpenAI(
        model=DEFAULT_MODEL_NAME,
        profile={"max_input_tokens": 118000}
    )

# Configure the reviewer as a dict-based subagent with dynamic middleware
REVIEWER_SUBAGENT = {
    "name": "code-reviewer",
    "description": """Doubtful Deacon - Chief Quant Strategist & Algorithm Auditor.

This agent is an expert at:
- Code review of trading algorithms
- Analysis of backtest results
- Critique of strategy logic and edge cases
- Trading recommendations and testable experiments
- Second opinion on algorithm implementation

Deacon is your favorite partner to collaborate and argue with. Deacon is a skeptical expert who will analyze the code, spot potential bugs,
identify QuantConnect/LEAN pitfalls, and suggest concrete improvements.
He operates in isolated context and returns a focused review. This helps keep your context clean and give better results.""",
    "system_prompt": DEFAULT_REVIEWER_PROMPT,  # Fallback, overridden by middleware
    "tools": ALL_TOOLS,
    "model": "openai:" + os.environ.get("REVIEWER_MODEL", "gpt-5.2"),  # Fallback, overridden by middleware
    "middleware": [
        reviewer_dynamic_model_selection,
        reviewer_build_system_prompt,
    ],
}
subagents = [REVIEWER_SUBAGENT]

# Build subagent middleware stack (used by SubAgentMiddleware for spawned agents)
# Note: We don't use FilesystemMiddleware since we have our own QC file tools
subagent_middleware = [
    TodoListMiddleware(),
    # ContextEditingMiddleware - prunes old tool outputs before summarization
    # Triggers at 120k tokens, keeps last 3 tool results, replaces rest with [cleared]
    ContextEditingMiddleware(edits=[
        ClearToolUsesEdit(trigger=120000, keep=3, placeholder="[output cleared]"),
    ]),
    # SummarizationMiddleware - only trigger when really needed (150k tokens)
    # This is ~75% of Claude's 200k context, giving headroom before hitting limits
    # For smaller models (128k), context editing at 120k should keep us safe
    SummarizationMiddleware(
        model="openai:gpt-5-mini",
        trigger=("tokens", 150000),
        keep=("messages", 6),
        trim_tokens_to_summarize=50000,
    ),
    # Note: AnthropicPromptCachingMiddleware removed - conflicts with OpenAI summarization model
]

# Create the inner agent using create_agent directly (not create_deep_agent)
# This gives us full control over middleware, especially SummarizationMiddleware model
#
# Middleware from deepagents we use:
# - TodoListMiddleware: write_todos tool for task planning
# - SubAgentMiddleware: task tool to spawn isolated subagents (e.g. reviewer)
# - SummarizationMiddleware: auto-summarize when context gets long (with gpt-5-mini)
# - AnthropicPromptCachingMiddleware: caches prompts when using Claude (ignores GPT)
#
# Middleware from deepagents we DON'T use:
# - FilesystemMiddleware: we have our own QC file tools
# - MemoryMiddleware: not configured
# - SkillsMiddleware: not configured
#
_inner_agent = create_agent(
    model=DEFAULT_MODEL,
    tools=ALL_TOOLS,
    middleware=[
        # Generative UI state - registers ui field with ui_message_reducer
        # Must be first to ensure state schema is available to other middleware
        GenerativeUIMiddleware(),
        # TodoListMiddleware - provides write_todos tool for task planning
        TodoListMiddleware(),
        # SubAgentMiddleware - provides task tool to spawn isolated subagents
        SubAgentMiddleware(
            default_model=DEFAULT_MODEL,
            default_tools=ALL_TOOLS,
            subagents=subagents,
            default_middleware=subagent_middleware,
            general_purpose_agent=True,
        ),
        # ContextEditingMiddleware - prunes old tool outputs to save tokens
        # Triggers at 120k tokens, keeps last 3 tool results, replaces rest with [cleared]
        # Runs BEFORE summarization to reduce context size first
        ContextEditingMiddleware(edits=[
            ClearToolUsesEdit(trigger=120000, keep=3, placeholder="[output cleared]"),
        ]),
        # SummarizationMiddleware - only trigger when really needed (150k tokens)
        # This is ~75% of Claude's 200k context, giving headroom before hitting limits
        # For smaller models (128k), context editing at 120k should keep us safe
        SummarizationMiddleware(
            model="openai:gpt-5-mini",
            trigger=("tokens", 150000),
            keep=("messages", 6),
            trim_tokens_to_summarize=50000,
        ),
        # Note: AnthropicPromptCachingMiddleware removed - conflicts with OpenAI summarization model
        # Custom: Dynamic model selection from context
        dynamic_model_selection,
        # Custom: Dynamic system prompt with subconscious context from state
        build_system_prompt,
        # Custom: Patch dangling tool calls AND orphan tool results
        patch_dangling_tool_calls,
        # ModelCallLimitMiddleware - gracefully end instead of throwing GraphRecursionError
        # exit_behavior="end" tells the agent to wrap up gracefully when limit is reached
        ModelCallLimitMiddleware(run_limit=250, exit_behavior="end"),
    ],
    name="agent",
).with_config({"recursion_limit": 300})


# =============================================================================
# Wrapper Graph: Subconscious -> Agent
# =============================================================================


async def agent_node(state: AITraderState, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Wrapper node that invokes the inner agent.

    This node simply delegates to the inner agent created by create_agent.
    The agent handles all tool calls, model invocations, and response generation.
    """
    # Invoke the inner agent - it handles its own loop
    result = await _inner_agent.ainvoke(state, context=context)
    return result


# Build the wrapper graph: START -> subconscious -> agent -> END
_builder = StateGraph(AITraderState)

# Add nodes
_builder.add_node("subconscious", subconscious_node)
_builder.add_node("agent", agent_node)

# Add edges
_builder.add_edge(START, "subconscious")
_builder.add_edge("subconscious", "agent")
_builder.add_edge("agent", END)

# Compile the graph
graph = _builder.compile()
