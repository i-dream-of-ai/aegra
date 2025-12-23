"""Code review tool - reviewer as a subagent with full tool access.

Pattern from official LangChain docs:
https://docs.langchain.com/oss/python/langchain/multi-agent/subagents

The main agent calls the reviewer as a tool, which invokes a separate
agent with its own tools and context. This keeps contexts isolated.
"""

from datetime import UTC, datetime

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain_core.tools import tool

from ai_trader.prompts import DEFAULT_REVIEWER_PROMPT

# Import tools the reviewer needs access to
from ai_trader.tools.ai_services import TOOLS as AI_SERVICES_TOOLS
from ai_trader.tools.backtest import TOOLS as BACKTEST_TOOLS
from ai_trader.tools.compile import TOOLS as COMPILE_TOOLS
from ai_trader.tools.files import TOOLS as FILES_TOOLS

# Reviewer gets read-only tools for analysis (no composite/write tools)
REVIEWER_AGENT_TOOLS = list(
    FILES_TOOLS  # Can read files
    + BACKTEST_TOOLS  # Can read backtest results
    + COMPILE_TOOLS  # Can compile to check for errors
    + AI_SERVICES_TOOLS  # Can search algorithms
)


def _create_reviewer_agent():
    """Create the reviewer agent (Doubtful Deacon) with tools.

    Uses create_agent from langchain.agents following the official
    subagent pattern. The model is tagged for streaming disambiguation.
    """
    system_prompt = DEFAULT_REVIEWER_PROMPT.format(
        system_time=datetime.now(tz=UTC).isoformat()
    )

    # Initialize model with tags for streaming disambiguation
    # when using subgraphs=True in the parent graph
    reviewer_model = init_chat_model(
        "anthropic:claude-sonnet-4-5-20250929",
        tags=["reviewer_subagent"],
        max_tokens=8192,
    )

    return create_agent(
        model=reviewer_model,
        tools=REVIEWER_AGENT_TOOLS,
        system_prompt=system_prompt,
    )


# Create the reviewer agent once at module load
_reviewer_agent = None


def _get_reviewer_agent():
    """Get or create the reviewer agent (lazy initialization)."""
    global _reviewer_agent
    if _reviewer_agent is None:
        _reviewer_agent = _create_reviewer_agent()
    return _reviewer_agent


@tool(
    "request_code_review",
    description="""Request a code review from Doubtful Deacon.

Use this after completing code changes or when you want a second opinion
on the algorithm implementation. The reviewer will analyze the code
and provide critique on bugs, QuantConnect pitfalls, and potential improvements.

The reviewer has access to read files, check backtest results, compile code,
and search the algorithm knowledge base.""",
)
def request_code_review(code_summary: str) -> str:
    """
    Request a code review from the reviewer subagent.

    Args:
        code_summary: A summary of the code changes and what you want reviewed.
                      Include the key parts of the algorithm, recent changes,
                      and any specific concerns you want addressed.

    Returns:
        The reviewer's critique and suggestions.
    """
    agent = _get_reviewer_agent()

    # Invoke the reviewer agent (subagents are stateless per invocation)
    result = agent.invoke({"messages": [{"role": "user", "content": code_summary}]})

    # Get the final message content from the result
    messages = result.get("messages", [])
    if messages:
        last_message = messages[-1]
        content = getattr(last_message, "content", str(last_message))
        return content if isinstance(content, str) else str(content)

    return "No response from reviewer."


# Export tools
TOOLS = [request_code_review]
