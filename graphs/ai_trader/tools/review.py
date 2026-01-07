"""Code review tool - triggers the reviewer subgraph.

The reviewer (Doubtful Deacon) is integrated as a node in the main graph.
When this tool is called, it uses Command to set request_review=True in state,
which causes the graph to route to the call_reviewer node after tools complete.

This enables shared message history between main agent and reviewer.
"""

from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

# Import using relative path since tools is a subpackage of ai_trader
from ..reviewer import reviewer_graph


@tool(
    "request_code_review",
    description="""Request a code review from Doubtful Deacon.

Use this after completing code changes or when you want a second opinion
on the algorithm implementation. The reviewer will analyze the code
and provide critique on bugs, QuantConnect pitfalls, and potential improvements.

The reviewer shares your conversation history and can see all the work
you've done. Just describe what you want reviewed.""",
)
async def request_code_review(
    review_request: str, 
    state: Annotated[dict, InjectedState]
) -> str:
    """
    Request a code review from the reviewer subgraph.
    """
    # Invoke the reviewer subgraph directly with current state
    # This runs the subagent in its own graph execution
    result = await reviewer_graph.ainvoke(state)
    
    # The result contains the full state of the subgraph
    # We want to extract the reviewer's response message
    reviewer_messages = result.get("messages", [])
    if not reviewer_messages:
        return "Reviewer completed but returned no messages."
        
    last_message = reviewer_messages[-1]
    
    # Return the content of the review
    return last_message.content


# Export tools
TOOLS = [request_code_review]
