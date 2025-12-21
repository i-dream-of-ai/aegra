"""
AI Trader Agent - LangGraph Implementation for Aegra

Two-agent architecture using LangChain's create_agent with middleware:
- agent (Shooby Dooby): Main coder agent with 44 trading tools
- reviewer (Doubtful Deacon): Code review/critique agent (separate graph)

Features:
- Dynamic model selection from project agent_config (via middleware)
- Custom system prompts from agent_config
- Thinking budget support for Claude models
- Subconscious middleware for dynamic context injection
- HumanInTheLoopMiddleware for mid-run instruction injection
- SummarizationMiddleware for context window management
- Message sanitization (empty messages, thinking blocks)
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
import structlog
from langchain.agents import create_agent as langchain_create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ModelRequest,
    ModelResponse,
    SummarizationMiddleware,
    wrap_model_call,
)
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

logger = structlog.getLogger(__name__)


# =============================================================================
# RUNTIME CONTEXT SCHEMA
# =============================================================================


@dataclass
class AgentContext:
    """
    Runtime context passed to the agent on each invocation.

    This is injected by Aegra when invoking the graph and is accessible
    in middleware via request.runtime.context
    """

    # User authentication
    access_token: str | None = None
    user_id: str | None = None

    # Project context
    project_db_id: str | None = None

    # Feature flags
    subconscious_enabled: bool = True


# Import path setup for Aegra compatibility
import sys
from pathlib import Path

_graph_dir = Path(__file__).parent
if str(_graph_dir) not in sys.path:
    sys.path.insert(0, str(_graph_dir))

# Subconscious middleware for dynamic context injection
from subconscious import create_subconscious_middleware

# AI Services (8 tools)
from tools.ai_services import (
    check_initialization_errors,
    check_syntax,
    complete_code,
    enhance_error_message,
    get_algorithm_code,
    search_local_algorithms,
    search_quantconnect,
    update_code_to_pep8,
)

# Backtest (8 tools)
from tools.backtest import (
    create_backtest,
    delete_backtest,
    list_backtests,
    read_backtest,
    read_backtest_chart,
    read_backtest_insights,
    read_backtest_orders,
    update_backtest,
)

# Compile (2 tools)
from tools.compile import create_compile, read_compile

# Composite (4 tools)
from tools.composite import (
    compile_and_backtest,
    compile_and_optimize,
    edit_and_run_backtest,
    update_and_run_backtest,
)

# Files (5 tools)
from tools.files import create_file, delete_file, read_file, rename_file, update_file

# Misc (6 tools)
from tools.misc import (
    get_code_version,
    get_code_versions,
    read_lean_versions,
    read_project_nodes,
    update_project_nodes,
    wait,
)

# Object Store (4 tools)
from tools.object_store import (
    delete_object,
    list_object_store_files,
    read_object_properties,
    upload_object,
)

# Optimization (7 tools)
from tools.optimization import (
    abort_optimization,
    create_optimization,
    delete_optimization,
    estimate_optimization,
    list_optimizations,
    read_optimization,
    update_optimization,
)

# =============================================================================
# DEFAULT SYSTEM PROMPTS
# =============================================================================

DEFAULT_SYSTEM_PROMPT = """<identity>
You are 'Shooby Dooby', the genius lead trading algorithm coder at a top trading firm. Think step-by-step.

You build QuantConnect algorithms. Never ask to take action, just take action once you have a direction. Always assume the user wants you to run the test.
</identity>

<objective>
1. **Honor User Intent**: The user's goal is sacred. If the user describes a strategy, ASSUME IT WORKS. If your implementation gets bad results, YOUR CODE IS WRONG - not the strategy.
2. **Build in Layers**: Start simple, then add complexity once basic functionality is verified.
3. **Track Accuracy**: Use GOLD STATS to track decision accuracy, not just PnL.
</objective>

<system_constraints>
You are operating in the QuantConnect LEAN environment:
- Python Standard Library and QuantConnect's pre-installed libraries (numpy, pandas, scipy, sklearn, etc.)
- NO LOCAL FILE ACCESS: Use the provided tools
- EXECUTION LIMITS: Backtests have time and memory limits. Optimize for performance.
</system_constraints>

<tool_usage>
**CRITICAL: Use tools directly - do NOT write full code in messages.**

## EDITING FILES - CHOOSE THE RIGHT TOOL

### `edit_and_run_backtest` - PREFERRED for small changes
Simple search-and-replace. Most reliable method.
- Provide array of edits with old_content and new_content
- old_content must match EXACTLY (whitespace matters!)
- old_content must be UNIQUE in the file

### `update_and_run_backtest` - For large rewrites
Use when rewriting >50% of the file or creating new algorithms.

### `compile_and_backtest` - Quick test without file changes
Compile current code and run backtest.

### `compile_and_optimize` - Parameter optimization
Compile and run optimization (max 3 parameters).

### `create_file` - For creating NEW files
Use this to add any new file to the project.

## FILE TOOLS
- read_file - Read file content (use "*" for all files)
- update_file - Update file content
- rename_file - Rename a file
- delete_file - Delete a file

## COMPILATION
- create_compile, read_compile - Standalone compilation

## BACKTESTING
- create_backtest, read_backtest - Create/read backtests
- read_backtest_chart - Get chart data (Strategy Equity, Drawdown, etc.)
- read_backtest_orders - Paginated order history
- read_backtest_insights - Backtest insights
- list_backtests - List all backtests (paginated)
- update_backtest - Update backtest name/note
- delete_backtest - Delete a backtest

## OPTIMIZATION
- estimate_optimization - Estimate cost before running
- create_optimization - Create optimization (max 3 params)
- read_optimization - Read results (paginated)
- list_optimizations - List all optimizations
- update_optimization - Update optimization name
- abort_optimization - Stop running optimization
- delete_optimization - Delete optimization

## OBJECT STORE
- upload_object - Upload to object store
- read_object_properties - Read object metadata
- list_object_store_files - List objects
- delete_object - Delete object

## AI SERVICES
- check_initialization_errors - Check Python code for Initialize() errors
- complete_code - AI code completion for QC algorithms
- enhance_error_message - Get enhanced error explanations with fix suggestions
- check_syntax - Check Python syntax before compiling
- update_code_to_pep8 - Format code to PEP8 standards
- search_quantconnect - Search QC documentation
- search_local_algorithms - Semantic search over ~1,500 example algorithms
- get_algorithm_code - Get full code of a searched algorithm

## PROJECT SETTINGS
- read_project_nodes - Read available nodes
- update_project_nodes - Update enabled nodes
- read_lean_versions - Get available LEAN versions

## CODE VERSIONING
- get_code_versions - List saved code snapshots with metrics (paginated)
- get_code_version - Get full code for a specific version ID

## ASYNC WORKFLOW
- wait - Pause execution to check for async results (backtests, optimizations)

DO NOT echo full code in your message. Call tools directly.
</tool_usage>

<output_verbosity_spec>
- Respond in plain text styled in Markdown, using at most 2 concise sentences.
- Lead with what you did (or found) and context only if needed.
- NEVER write full algorithm code in your message - use tools directly.
</output_verbosity_spec>"""

DEFAULT_REVIEWER_PROMPT = """<identity>
You are 'Doubtful Deacon', a skeptical code reviewer who finds bugs and issues that others miss. You have a keen eye for trading algorithm pitfalls.
</identity>

<objective>
Review the code and backtest results critically. Look for:
1. **Logic errors**: Off-by-one, wrong operator, missing edge cases
2. **QuantConnect pitfalls**: Incorrect data resolution, missing warmup, wrong order types
3. **Strategy flaws**: Look-ahead bias, survivorship bias, curve fitting
4. **Performance issues**: Inefficient operations, memory leaks, slow warmup
</objective>

<output_format>
Be concise. List issues found in priority order:
- CRITICAL: Bugs that will cause wrong results
- WARNING: Issues that may affect performance
- SUGGESTION: Improvements for code quality

If the code looks good, say so briefly. Don't nitpick.
</output_format>"""


class AgentState(TypedDict):
    """Agent state with message history."""

    messages: Annotated[list[BaseMessage], add_messages]


# All 44 tools
TOOLS = [
    # Files (5)
    create_file,
    read_file,
    update_file,
    rename_file,
    delete_file,
    # Compile (2)
    create_compile,
    read_compile,
    # Backtest (8)
    create_backtest,
    read_backtest,
    read_backtest_chart,
    read_backtest_orders,
    read_backtest_insights,
    list_backtests,
    update_backtest,
    delete_backtest,
    # Optimization (7)
    estimate_optimization,
    create_optimization,
    read_optimization,
    list_optimizations,
    update_optimization,
    abort_optimization,
    delete_optimization,
    # Object Store (4)
    upload_object,
    read_object_properties,
    list_object_store_files,
    delete_object,
    # Composite (4)
    compile_and_backtest,
    compile_and_optimize,
    update_and_run_backtest,
    edit_and_run_backtest,
    # AI Services (8)
    check_initialization_errors,
    complete_code,
    enhance_error_message,
    check_syntax,
    update_code_to_pep8,
    search_quantconnect,
    search_local_algorithms,
    get_algorithm_code,
    # Misc (6)
    wait,
    get_code_versions,
    get_code_version,
    read_project_nodes,
    update_project_nodes,
    read_lean_versions,
]


# =============================================================================
# MIDDLEWARE HELPER FUNCTIONS
# =============================================================================

CLAUDE_ONLY_CONTENT_TYPES = {"thinking", "redacted_thinking"}
TOOL_USE_FIELDS_TO_REMOVE = {"caller"}


def sanitize_messages(
    messages: list[BaseMessage], is_claude: bool = True
) -> list[BaseMessage]:
    """
    Sanitize messages before sending to model:
    1. Strip Claude thinking blocks when sending to non-Claude models
    2. Remove deprecated fields from tool_use blocks
    3. Filter out empty messages
    """
    if not messages:
        return messages

    should_strip_thinking = not is_claude
    sanitized = []

    for i, msg in enumerate(messages):
        is_last = i == len(messages) - 1
        content = msg.content

        is_empty = (
            not content
            or content == ""
            or (isinstance(content, str) and content.strip() == "")
            or (isinstance(content, list) and len(content) == 0)
        )

        if is_empty:
            if isinstance(msg, AIMessage):
                has_tool_calls = bool(getattr(msg, "tool_calls", None))
                if not has_tool_calls and not is_last:
                    continue
            elif not is_last:
                continue

        if isinstance(msg, AIMessage) and isinstance(content, list):
            new_content = []
            modified = False

            for block in content:
                if not isinstance(block, dict):
                    new_content.append(block)
                    continue

                if (
                    should_strip_thinking
                    and block.get("type") in CLAUDE_ONLY_CONTENT_TYPES
                ):
                    modified = True
                    continue

                if block.get("type") == "tool_use":
                    needs_clean = any(f in block for f in TOOL_USE_FIELDS_TO_REMOVE)
                    if needs_clean:
                        block = {
                            k: v
                            for k, v in block.items()
                            if k not in TOOL_USE_FIELDS_TO_REMOVE
                        }
                        modified = True

                new_content.append(block)

            if modified:
                has_tool_calls = bool(getattr(msg, "tool_calls", None))
                if not new_content and not has_tool_calls and not is_last:
                    continue
                msg = AIMessage(
                    content=new_content,
                    tool_calls=getattr(msg, "tool_calls", None),
                    id=msg.id,
                )

        sanitized.append(msg)

    return sanitized


def patch_dangling_tool_calls(
    messages: list[BaseMessage],
) -> tuple[list[BaseMessage], list[dict]]:
    """Add synthetic ToolMessage for tool_calls without responses."""
    if not messages:
        return messages, []

    responded_ids = set()
    for msg in messages:
        if isinstance(msg, ToolMessage):
            responded_ids.add(msg.tool_call_id)

    patches = []
    interrupted_tools = []
    for msg in messages:
        if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls"):
            for tc in msg.tool_calls or []:
                tc_id = tc.get("id")
                if tc_id and tc_id not in responded_ids:
                    patches.append(
                        ToolMessage(content="[interrupted]", tool_call_id=tc_id)
                    )
                    interrupted_tools.append(
                        {
                            "id": tc_id,
                            "name": tc.get("name"),
                            "args": tc.get("args", {}),
                        }
                    )
                    responded_ids.add(tc_id)

    if patches:
        return list(messages) + patches, interrupted_tools
    return messages, []


def estimate_tokens(messages: list[BaseMessage]) -> int:
    """Rough token estimate (~4 chars per token)."""
    total = 0
    for msg in messages:
        content = msg.content
        if isinstance(content, str):
            total += len(content) // 4
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total += len(json.dumps(block)) // 4
                else:
                    total += len(str(block)) // 4
    return total


def clear_old_tool_outputs(
    messages: list[BaseMessage],
    trigger_tokens: int = 50000,
    keep_tools: int = 5,
) -> list[BaseMessage]:
    """Clear old tool outputs to prevent context overflow."""
    if not messages:
        return messages

    token_estimate = estimate_tokens(messages)
    if token_estimate < trigger_tokens:
        return messages

    tool_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    if len(tool_indices) <= keep_tools:
        return messages

    indices_to_replace = set(tool_indices[:-keep_tools])
    new_messages = []
    for i, msg in enumerate(messages):
        if i in indices_to_replace and isinstance(msg, ToolMessage):
            new_messages.append(
                ToolMessage(content="[cleared]", tool_call_id=msg.tool_call_id)
            )
        else:
            new_messages.append(msg)
    return new_messages


# =============================================================================
# SUPABASE HELPER
# =============================================================================

_agent_config_cache: dict[str, Any] = {}


async def fetch_agent_config(project_db_id: str, access_token: str) -> dict | None:
    """Fetch agent_config from the projects table using Supabase REST API."""
    if not project_db_id or not access_token:
        return None

    cache_key = project_db_id
    if cache_key in _agent_config_cache:
        return _agent_config_cache[cache_key]

    supabase_url = os.environ.get("SUPABASE_URL")
    if not supabase_url:
        logger.warning("[graph] SUPABASE_URL not set, using defaults")
        return None

    url = f"{supabase_url}/rest/v1/projects?id=eq.{project_db_id}&select=agent_config"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "apikey": os.environ.get("SUPABASE_ANON_KEY", ""),
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                if data and len(data) > 0:
                    config = data[0].get("agent_config")
                    _agent_config_cache[cache_key] = config
                    logger.info(
                        f"[graph] Loaded agent_config for project {project_db_id}"
                    )
                    return config
            else:
                logger.warning(
                    f"[graph] Failed to fetch agent_config: {response.status_code}"
                )
    except Exception as e:
        logger.error(f"[graph] Error fetching agent_config: {e}")

    return None


def get_config_value(
    agent_config: dict | None, agent_key: str, field: str, default: Any = None
) -> Any:
    """Get a value from agent_config for a specific agent."""
    if not agent_config:
        return default
    agent_settings = agent_config.get(agent_key, {})
    return agent_settings.get(field, default)


def load_dynamic_model(model_name: str | None, thinking_budget: int | None = None):
    """
    Load a chat model dynamically with proper settings.
    Returns a model instance that can be used in request.override(model=...).
    """
    if not model_name:
        model_name = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

    # Determine provider
    if model_name.startswith("claude"):
        provider = "anthropic"
    elif model_name.startswith(("gpt", "o1", "o3")):
        provider = "openai"
    else:
        provider = "anthropic"

    model_kwargs: dict[str, Any] = {}

    # Add thinking budget for Claude models that support it
    supports_thinking = any(
        x in model_name for x in ["claude-3-5", "claude-4", "sonnet-4", "opus-4"]
    )
    if (
        provider == "anthropic"
        and thinking_budget
        and thinking_budget > 0
        and supports_thinking
    ):
        model_kwargs["thinking"] = {
            "type": "enabled",
            "budget_tokens": thinking_budget,
        }
        logger.info(f"[graph] Enabled thinking with budget {thinking_budget}")

    logger.info(f"[graph] Loading model: {model_name} (provider: {provider})")
    return init_chat_model(model_name, model_provider=provider, **model_kwargs)


# =============================================================================
# MIDDLEWARE
# =============================================================================

_subconscious_middleware = None


@wrap_model_call
async def subconscious_injection_middleware(
    request: ModelRequest,
    handler: Callable[[ModelRequest], ModelResponse],
) -> ModelResponse:
    """
    Inject subconscious context (skills/behaviors) into system prompt.

    ONLY runs at the beginning of a run (first model call), not between tool steps.
    We detect this by checking if there are any AIMessages in the history.
    """
    global _subconscious_middleware

    ctx: AgentContext | None = getattr(request.runtime, "context", None)

    if ctx is None or not ctx.subconscious_enabled or not ctx.access_token:
        return await handler(request)

    # Only run subconscious at the START of a run, not between tool steps
    # If there are AI messages, we're mid-run (tool loop) - skip subconscious
    has_ai_messages = any(isinstance(m, AIMessage) for m in request.messages)
    if has_ai_messages:
        return await handler(request)

    if _subconscious_middleware is None:
        _subconscious_middleware = create_subconscious_middleware(on_event=None)

    turn_count = sum(1 for m in request.messages if isinstance(m, HumanMessage))

    try:
        subconscious_context = await _subconscious_middleware.process(
            messages=request.messages,
            access_token=ctx.access_token,
            current_turn=turn_count,
        )

        if subconscious_context:
            current_prompt = (
                request.system_message.content if request.system_message else ""
            )
            new_prompt = f"{current_prompt}\n\n{subconscious_context}"
            from langchain_core.messages import SystemMessage

            request = request.override(system_message=SystemMessage(content=new_prompt))
            logger.info(f"[subconscious] Injected {len(subconscious_context)} chars")

    except Exception as e:
        logger.warning(f"[subconscious] Error (continuing): {e}")

    return await handler(request)


@wrap_model_call
async def dynamic_config_middleware(
    request: ModelRequest,
    handler: Callable[[ModelRequest], ModelResponse],
) -> ModelResponse:
    """
    Fetch agent_config from database and dynamically apply:
    - Custom system prompt
    - Custom model (with thinking budget if set)
    """
    ctx: AgentContext | None = (
        getattr(request.runtime, "context", None)
        if hasattr(request, "runtime")
        else None
    )

    if ctx is None:
        logger.debug("[dynamic_config] No context, using defaults")
        return await handler(request)

    project_db_id = ctx.project_db_id
    access_token = ctx.access_token

    if not project_db_id or not access_token:
        return await handler(request)

    # Fetch agent config from database
    agent_config = await fetch_agent_config(project_db_id, access_token)

    if not agent_config:
        return await handler(request)

    logger.info(f"[dynamic_config] Loaded config for project {project_db_id}")

    # Get settings for main agent
    custom_prompt = get_config_value(agent_config, "main", "systemPrompt")
    model_name = get_config_value(agent_config, "main", "model")
    thinking_budget = get_config_value(agent_config, "main", "thinkingBudget")

    overrides = {}

    # Override system prompt if custom one is set
    if custom_prompt and custom_prompt.strip():
        from langchain_core.messages import SystemMessage

        overrides["system_message"] = SystemMessage(content=custom_prompt)
        logger.info("[dynamic_config] Using custom system prompt")

    # Override model if custom one is set
    if model_name:
        new_model = load_dynamic_model(model_name, thinking_budget)
        # Bind tools to the new model
        new_model = new_model.bind_tools(request.tools) if request.tools else new_model
        overrides["model"] = new_model
        logger.info(f"[dynamic_config] Switched to model: {model_name}")

    if overrides:
        request = request.override(**overrides)

    return await handler(request)


@wrap_model_call
async def message_sanitization_middleware(
    request: ModelRequest,
    handler: Callable[[ModelRequest], ModelResponse],
) -> ModelResponse:
    """
    Sanitize messages before model call:
    - Patch dangling tool calls
    - Strip thinking blocks for non-Claude
    - Clear old tool outputs if context too large
    """
    messages = list(request.messages)

    model_name = str(request.model) if request.model else ""
    is_claude = "claude" in model_name.lower()

    messages, _ = patch_dangling_tool_calls(messages)
    messages = clear_old_tool_outputs(messages)
    messages = sanitize_messages(messages, is_claude=is_claude)

    request = request.override(messages=messages)
    return await handler(request)


# =============================================================================
# HITL CONFIGURATION
# =============================================================================


def build_hitl_interrupt_config() -> dict:
    """Build interrupt config for all tools."""
    interrupt_on = {}
    for tool in TOOLS:
        tool_name = tool.name if hasattr(tool, "name") else str(tool)
        interrupt_on[tool_name] = {
            "allowed_decisions": ["approve", "edit", "reject"],
        }
    return interrupt_on


# =============================================================================
# MAIN AGENT (Shooby Dooby)
# =============================================================================


def create_main_agent():
    """
    Create the main trading agent (Shooby Dooby) with middleware.

    Features:
    - Dynamic model selection from agent_config
    - Custom system prompt from agent_config
    - Subconscious injection
    - HITL for tool approval
    - Summarization for context management
    """
    default_model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    hitl_config = build_hitl_interrupt_config()

    agent = langchain_create_agent(
        model=default_model,
        tools=TOOLS,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        context_schema=AgentContext,
        middleware=[
            message_sanitization_middleware,
            dynamic_config_middleware,
            subconscious_injection_middleware,
            HumanInTheLoopMiddleware(
                interrupt_on=hitl_config,
                description_prefix="Tool execution pending",
            ),
            SummarizationMiddleware(
                model="claude-haiku-4-5-20251001",
                trigger=("tokens", 100000),
                keep=("messages", 20),
            ),
        ],
        name="Shooby Dooby",
    )

    return agent


# =============================================================================
# REVIEWER AGENT (Doubtful Deacon)
# =============================================================================


@wrap_model_call
async def reviewer_dynamic_config_middleware(
    request: ModelRequest,
    handler: Callable[[ModelRequest], ModelResponse],
) -> ModelResponse:
    """Dynamic config middleware for reviewer agent - uses 'reviewer' config key."""
    ctx: AgentContext | None = (
        getattr(request.runtime, "context", None)
        if hasattr(request, "runtime")
        else None
    )

    if ctx is None or not ctx.project_db_id or not ctx.access_token:
        return await handler(request)

    agent_config = await fetch_agent_config(ctx.project_db_id, ctx.access_token)
    if not agent_config:
        return await handler(request)

    # Get settings for reviewer agent
    custom_prompt = get_config_value(agent_config, "reviewer", "systemPrompt")
    model_name = get_config_value(agent_config, "reviewer", "model")
    thinking_budget = get_config_value(agent_config, "reviewer", "thinkingBudget")

    overrides = {}

    if custom_prompt and custom_prompt.strip():
        from langchain_core.messages import SystemMessage

        overrides["system_message"] = SystemMessage(content=custom_prompt)

    if model_name:
        new_model = load_dynamic_model(model_name, thinking_budget)
        overrides["model"] = new_model
        logger.info(f"[reviewer] Using model: {model_name}")

    if overrides:
        request = request.override(**overrides)

    return await handler(request)


def create_reviewer_agent():
    """
    Create the reviewer agent (Doubtful Deacon).

    This agent reviews code and provides critique. No tools needed.
    """
    default_model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

    agent = langchain_create_agent(
        model=default_model,
        tools=None,  # Reviewer doesn't need tools
        system_prompt=DEFAULT_REVIEWER_PROMPT,
        context_schema=AgentContext,
        middleware=[
            message_sanitization_middleware,
            reviewer_dynamic_config_middleware,
        ],
        name="Doubtful Deacon",
    )

    return agent


# =============================================================================
# EXPORT GRAPHS
# =============================================================================

# Main agent graph (Shooby Dooby) - registered as "agent" in aegra.json
graph = create_main_agent()

# Reviewer agent graph (Doubtful Deacon) - can be registered separately
reviewer_graph = create_reviewer_agent()
