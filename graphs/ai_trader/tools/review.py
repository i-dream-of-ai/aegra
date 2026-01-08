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
    
    We filter out tool-related messages from state before invoking the subgraph
    because the reviewer doesn't have matching tool responses for the main agent's
    tool_calls, and OpenAI requires tool_calls to have matching responses.
    """
    import re
    from langchain_core.messages import ToolMessage, AIMessage
    
    # Filter messages for the reviewer
    messages = state.get("messages", [])
    filtered_messages = []
    
    def sanitize_name(name: str) -> str:
        if not name:
            return name
        return re.sub(r'[\s<|\\/>\(\)\[\]\{\}]', '_', name)
    
    for msg in messages:
        # Skip ToolMessage (main agent's tool responses)
        if isinstance(msg, ToolMessage):
            continue
        # Skip AIMessage with tool_calls (main agent's tool calls)
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            continue
        
        # Sanitize message name for OpenAI compatibility
        if hasattr(msg, 'name') and msg.name:
            msg_copy = msg.model_copy()
            msg_copy.name = sanitize_name(msg.name)
            filtered_messages.append(msg_copy)
        else:
            filtered_messages.append(msg)
    
    # Create filtered state for the reviewer
    filtered_state = {**state, "messages": filtered_messages}
    
    # Invoke the reviewer subgraph with filtered state
    result = await reviewer_graph.ainvoke(filtered_state)
    
    # Debug logging to see what the reviewer returns
    import structlog
    logger = structlog.getLogger(__name__)
    logger.info(
        "Reviewer result",
        result_type=type(result).__name__,
        result_keys=list(result.keys()) if isinstance(result, dict) else None,
        messages_count=len(result.get("messages", [])) if isinstance(result, dict) else None,
    )
    
    # The result contains the full state of the subgraph
    # We want to extract the reviewer's response message
    reviewer_messages = result.get("messages", [])
    if not reviewer_messages:
        return "Reviewer completed but returned no messages."
    
    # Find the last AI message with actual content (skip tool calls)
    last_content = None
    for msg in reversed(reviewer_messages):
        # Get content attribute safely
        content = getattr(msg, 'content', None)
        if content and isinstance(content, str) and content.strip():
            last_content = content
            break
        # Also check for dict-style messages
        if isinstance(msg, dict) and msg.get('content'):
            last_content = msg['content']
            break
    
    # Return the content of the review
    if last_content:
        return last_content
    
    # Fallback: try to get any content from last message
    last_message = reviewer_messages[-1]
    if hasattr(last_message, 'content') and last_message.content:
        return str(last_message.content)
    if isinstance(last_message, dict):
        return last_message.get('content', 'Reviewer completed but message had no content.')
    
    return "Reviewer completed but could not extract response content."


# Export tools
TOOLS = [request_code_review]
