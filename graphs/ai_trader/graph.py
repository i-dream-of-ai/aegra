"""
AI Trader Agent - LangChain create_agent Implementation

This graph uses the LangChain create_agent pattern with middleware for:
- Model fallback (automatic failover on errors)
- Todo list (task planning and tracking)
- Dangling tool call repair (fix interrupted sessions)
- Subconscious injection (dynamic skill/behavior loading)
- Config fetching (load project settings from DB)
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
    TodoListMiddleware,
)
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from ai_trader.context import Context

# Import subconscious components
from ai_trader.subconscious.middleware import SubconsciousMiddleware as SubconsciousProcessor
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
        self, state: AITraderState, _runtime: Runtime
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
# Middleware: Config Fetcher (loads project settings from DB)
# =============================================================================


class ConfigFetcherMiddleware(AgentMiddleware[AITraderState]):
    """
    Fetches agent configuration from the database at the start of each run.

    Loads project's AI settings from the projects table, including:
    - model, thinking_budget, reasoning_effort
    - agent_config JSONB (per-agent overrides)
    """

    state_schema = AITraderState

    async def abefore_agent(
        self, _state: AITraderState, runtime: Runtime[Context]
    ) -> dict[str, Any] | None:
        """Fetch config from DB and update runtime context."""
        ctx = runtime.context

        if not ctx.project_db_id:
            logger.info("No project_db_id in context, using default config")
            return None

        try:
            client = SupabaseClient(use_service_role=True)

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
                return None

            project = projects[0]
            agent_config = project.get("agent_config") or {}
            main_config = agent_config.get("main", {})

            # Apply main agent settings (JSONB overrides project-level)
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

            if main_config.get("reasoningEffort"):
                ctx.reasoning_effort = main_config["reasoningEffort"]
            elif project.get("reasoning_effort"):
                ctx.reasoning_effort = project["reasoning_effort"]

            if main_config.get("systemPrompt"):
                ctx.system_prompt = main_config["systemPrompt"]

            if main_config.get("subconsciousEnabled") is False:
                ctx.subconscious_enabled = False

            # Reviewer config
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
            )

        except Exception as e:
            logger.warning("Failed to fetch agent config", error=str(e))

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

        if not ctx.subconscious_enabled:
            logger.debug("Subconscious disabled, skipping")
            return None

        access_token = ctx.access_token
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
# Middleware: Dynamic System Prompt (injects subconscious context)
# =============================================================================


class DynamicPromptMiddleware(AgentMiddleware[AITraderState]):
    """
    Injects subconscious context into the system prompt before model calls.

    Uses wrap_model_call to access state and modify the system message with
    injected context from the SubconsciousMiddleware.
    """

    state_schema = AITraderState

    def wrap_model_call(
        self,
        request,  # ModelRequest
        handler,  # Callable[[ModelRequest], ModelResponse]
    ):
        """Wrap model call to inject subconscious context into system message."""
        # Access subconscious_context from state (set by SubconsciousMiddleware)
        subconscious_context = request.state.get("subconscious_context")

        if subconscious_context and request.system_message:
            logger.debug(
                "Injecting subconscious context into system prompt",
                context_length=len(subconscious_context),
            )
            # Build the context addendum
            context_addendum = (
                f"\n\n<injected_context>\n{subconscious_context}\n</injected_context>"
            )

            # Append to system message content blocks
            from langchain_core.messages import SystemMessage

            new_content = list(request.system_message.content_blocks) + [
                {"type": "text", "text": context_addendum}
            ]
            new_system_message = SystemMessage(content=new_content)
            request = request.override(system_message=new_system_message)

        return handler(request)


# =============================================================================
# Dynamic Model Selection (with extended thinking support)
# =============================================================================


def create_dynamic_model(_state: AITraderState, runtime: Runtime[Context]):
    """
    Dynamically select and configure the model based on runtime context.

    Supports:
    - Claude models with extended thinking
    - OpenAI models with reasoning effort
    """
    ctx = runtime.context

    model_name = ctx.model or os.environ.get(
        "ANTHROPIC_MODEL", "claude-opus-4-5-20251101"
    )

    is_claude = model_name.startswith("claude")

    if is_claude:
        base_max_tokens = 8192
        model_kwargs = {
            "model": model_name,
            "api_key": os.environ.get("ANTHROPIC_API_KEY"),
        }

        if ctx.thinking_budget > 0:
            model_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": ctx.thinking_budget,
            }
            model_kwargs["max_tokens"] = max(base_max_tokens, ctx.thinking_budget + 4096)
        else:
            model_kwargs["max_tokens"] = base_max_tokens

        model = ChatAnthropic(**model_kwargs)
    else:
        model_kwargs = {
            "model": model_name,
            "api_key": os.environ.get("OPENAI_API_KEY"),
            "max_tokens": 8192,
        }

        if ctx.reasoning_effort and ctx.reasoning_effort != "none":
            model_kwargs["reasoning_effort"] = ctx.reasoning_effort

        model = ChatOpenAI(**model_kwargs)

    # Bind tools to the model
    return model.bind_tools(ALL_TOOLS)


# =============================================================================
# Create the Agent
# =============================================================================

# Get default system prompt from context
from ai_trader.context import DEFAULT_SYSTEM_PROMPT

graph = create_agent(
    # Dynamic model selection based on runtime context
    model=create_dynamic_model,
    # All available tools
    tools=ALL_TOOLS,
    # Default system prompt (can be overridden via context)
    system_prompt=DEFAULT_SYSTEM_PROMPT,
    # Context schema for runtime configuration
    context_schema=Context,
    # Middleware stack (executed in order)
    middleware=[
        # 1. Model fallback - automatic failover on errors
        ModelFallbackMiddleware(
            "claude-sonnet-4-20250514",  # First fallback
            "gpt-4o",  # Cross-provider fallback
        ),
        # 2. Todo list - task planning and tracking
        TodoListMiddleware(),
        # 3. Config fetcher - load project settings from DB
        ConfigFetcherMiddleware(),
        # 4. Subconscious - RAG + memory injection
        SubconsciousMiddleware(),
        # 5. Dangling tool repair - fix interrupted sessions
        DanglingToolRepairMiddleware(),
        # 6. Dynamic prompt - inject subconscious context
        DynamicPromptMiddleware(),
    ],
    # Graph name for debugging
    name="Shooby Dooby",
)
