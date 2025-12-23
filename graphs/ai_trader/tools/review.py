"""Code review tool - reviewer subagent with full tool access.

The reviewer (Doubtful Deacon) has access to read files, compile code,
check backtest results, and search the knowledge base. It runs as an
isolated subagent without a checkpointer to ensure complete state
isolation from the parent agent.

Pattern: https://docs.langchain.com/oss/python/langchain/multi-agent/subagents
"""

from datetime import UTC, datetime

from langchain.agents import create_agent
from langchain_core.tools import tool

from ai_trader.prompts import DEFAULT_REVIEWER_PROMPT

# Import tools the reviewer needs access to
from ai_trader.tools.ai_services import TOOLS as AI_SERVICES_TOOLS
from ai_trader.tools.backtest import TOOLS as BACKTEST_TOOLS
from ai_trader.tools.compile import TOOLS as COMPILE_TOOLS
from ai_trader.tools.files import TOOLS as FILES_TOOLS

# Reviewer gets read-only tools for analysis (no composite/write tools)
REVIEWER_TOOLS = list(
    FILES_TOOLS  # Can read files
    + BACKTEST_TOOLS  # Can read backtest results
    + COMPILE_TOOLS  # Can compile to check for errors
    + AI_SERVICES_TOOLS  # Can search algorithms
)


def _create_reviewer_agent():
    """Create a stateless reviewer agent.

    NO checkpointer = stateless = complete isolation from parent.
    Each invocation starts fresh with no shared state.
    """
    system_prompt = DEFAULT_REVIEWER_PROMPT.format(
        system_time=datetime.now(tz=UTC).isoformat()
    )

    return create_agent(
        model="anthropic:claude-sonnet-4-5-20250929",
        tools=REVIEWER_TOOLS,
        system_prompt=system_prompt,
        # NO checkpointer - ensures stateless, isolated execution
    )


@tool(
    "request_code_review",
    description="""Request a code review from Doubtful Deacon.

Use this after completing code changes or when you want a second opinion
on the algorithm implementation. The reviewer will analyze the code
and provide critique on bugs, QuantConnect pitfalls, and potential improvements.

The reviewer has access to read files, check backtest results, compile code,
and search the algorithm knowledge base. You can just describe what you want
reviewed and the reviewer will read the necessary files.""",
)
async def request_code_review(review_request: str) -> str:
    """
    Request a code review from the reviewer subagent.

    Args:
        review_request: Description of what to review. Can include:
                        - File paths to review
                        - Specific concerns to address
                        - Backtest results to analyze
                        - Recent changes made

    Returns:
        The reviewer's critique and suggestions.
    """
    # Create fresh agent each time (stateless, isolated)
    agent = _create_reviewer_agent()

    # Invoke with fresh message list - no shared history
    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": review_request}]
    })

    # Extract only the final message content
    messages = result.get("messages", [])
    if messages:
        last_message = messages[-1]
        content = getattr(last_message, "content", str(last_message))
        # Handle content blocks (text blocks from Claude)
        if isinstance(content, list):
            text_parts = [
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            ]
            return "\n".join(text_parts)
        return content if isinstance(content, str) else str(content)

    return "No response from reviewer."


# Export tools
TOOLS = [request_code_review]
