"""Code review tool - triggers the reviewer subgraph.

The reviewer (Doubtful Deacon) is integrated as a node in the main graph.
When this tool is called, it uses Command to set request_review=True in state,
which causes the graph to route to the call_reviewer node after tools complete.

This enables shared message history between main agent and reviewer.
"""

from langchain_core.tools import tool
from langgraph.types import Command


@tool(
    "request_code_review",
    description="""Request a code review from Doubtful Deacon.

Use this after completing code changes or when you want a second opinion
on the algorithm implementation. The reviewer will analyze the code
and provide critique on bugs, QuantConnect pitfalls, and potential improvements.

The reviewer shares your conversation history and can see all the work
you've done. Just describe what you want reviewed.""",
)
def request_code_review(review_request: str) -> Command:
    """
    Request a code review from the reviewer subgraph.

    This tool returns a Command that sets request_review=True in the graph state,
    which triggers routing to the reviewer node. The reviewer will see the 
    full conversation and the review_request context.

    Args:
        review_request: Description of what to review. Can include:
                        - Specific concerns to address
                        - Recent changes made
                        - Questions about the implementation

    Returns:
        Command that updates state with request_review=True
    """
    # Return Command to update state
    # ToolNode will process this and update state, triggering reviewer routing
    return Command(
        update={
            "request_review": True,
        }
    )


# Export tools
TOOLS = [request_code_review]
