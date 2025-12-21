"""Code review tool - invokes the Doubtful Deacon reviewer agent."""

import json
import os

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import get_runtime

from ai_trader.context import Context, DEFAULT_REVIEWER_PROMPT


async def request_code_review(code: str, backtest_results: str | None = None) -> str:
    """
    Request a code review from Doubtful Deacon.

    Use this after completing code changes or when you want a second opinion
    on the algorithm implementation. The reviewer will analyze the code for
    bugs, QuantConnect pitfalls, and potential improvements.

    Args:
        code: The algorithm code to review
        backtest_results: Optional backtest results to analyze alongside the code
    """
    try:
        runtime = get_runtime(Context)
        ctx = runtime.context

        # Get reviewer configuration from context
        reviewer_model = ctx.reviewer_model or os.environ.get(
            "ANTHROPIC_MODEL", "claude-sonnet-4-20250514"
        )
        reviewer_prompt = ctx.reviewer_prompt or DEFAULT_REVIEWER_PROMPT

        # Build the review request
        review_request = f"Review this algorithm code:\n\n```python\n{code}\n```"

        if backtest_results:
            review_request += f"\n\nBacktest Results:\n{backtest_results}"

        # Create the reviewer model
        model = ChatAnthropic(
            model=reviewer_model,
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            max_tokens=4096,
        )

        # Invoke the reviewer
        messages = [
            SystemMessage(content=reviewer_prompt),
            HumanMessage(content=review_request),
        ]

        response = await model.ainvoke(messages)
        review_content = response.content

        # Handle content blocks if needed
        if isinstance(review_content, list):
            review_content = "\n".join(
                block.get("text", str(block)) if isinstance(block, dict) else str(block)
                for block in review_content
            )

        return json.dumps(
            {
                "success": True,
                "reviewer": "Doubtful Deacon",
                "review": review_content,
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps(
            {
                "error": True,
                "message": f"Failed to get code review: {e!s}",
            }
        )


# Export all tools
TOOLS = [request_code_review]
