"""Code review tool - invokes the Doubtful Deacon reviewer agent."""

import json
import os

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from ai_trader.context import DEFAULT_REVIEWER_PROMPT


def _get_config():
    """Get LangGraph config."""
    from langgraph.config import get_config

    return get_config()


@tool
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
        config = _get_config()
        configurable = config.get("configurable", {})

        # Get reviewer configuration from config
        reviewer_model = configurable.get("reviewer_model") or os.environ.get(
            "ANTHROPIC_MODEL", "claude-opus-4-5-20251101"
        )
        reviewer_prompt = configurable.get("reviewer_prompt") or DEFAULT_REVIEWER_PROMPT
        reviewer_thinking_budget = configurable.get("reviewer_thinking_budget", 0)
        reviewer_reasoning_effort = configurable.get("reviewer_reasoning_effort", "none")

        # Build the review request
        review_request = f"Review this algorithm code:\n\n```python\n{code}\n```"

        if backtest_results:
            review_request += f"\n\nBacktest Results:\n{backtest_results}"

        # Determine model provider (Claude vs GPT)
        is_claude = reviewer_model.startswith("claude")

        if is_claude:
            model_kwargs = {
                "model": reviewer_model,
                "api_key": os.environ.get("ANTHROPIC_API_KEY"),
                "max_tokens": 4096,
            }

            # Add extended thinking if configured for Claude reviewer
            if reviewer_thinking_budget and reviewer_thinking_budget > 0:
                model_kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": reviewer_thinking_budget,
                }

            model = ChatAnthropic(**model_kwargs)
        else:
            # OpenAI / GPT models
            model_kwargs = {
                "model": reviewer_model,
                "api_key": os.environ.get("OPENAI_API_KEY"),
                "max_tokens": 4096,
            }

            # Add reasoning effort for GPT models
            if reviewer_reasoning_effort and reviewer_reasoning_effort != "none":
                model_kwargs["reasoning_effort"] = reviewer_reasoning_effort

            model = ChatOpenAI(**model_kwargs)

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
