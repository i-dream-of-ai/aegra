"""Code review tool - handoff to the Doubtful Deacon reviewer subgraph."""

from langchain_core.messages import AIMessage, ToolMessage
from langchain.tools import tool, ToolRuntime
from langgraph.types import Command


@tool
def request_code_review(runtime: ToolRuntime) -> Command:
    """
    Request a code review from Doubtful Deacon.

    Use this after completing code changes or when you want a second opinion
    on the algorithm implementation. The reviewer will analyze the conversation
    and provide critique on bugs, QuantConnect pitfalls, and potential improvements.

    This hands off the conversation to the reviewer agent.
    """
    # Get the last AI message to include in handoff
    last_ai_message = next(
        (msg for msg in reversed(runtime.state["messages"]) if isinstance(msg, AIMessage)),
        None
    )

    # Create a tool message for the handoff
    transfer_message = ToolMessage(
        content="Handing off to Doubtful Deacon for code review...",
        tool_call_id=runtime.tool_call_id,
    )

    # Build messages to pass - include context
    messages_to_pass = []
    if last_ai_message:
        messages_to_pass.append(last_ai_message)
    messages_to_pass.append(transfer_message)

    return Command(
        goto="reviewer",
        update={"messages": messages_to_pass},
        graph=Command.PARENT,
    )


# Export all tools
TOOLS = [request_code_review]
