"""Code review tools - handoffs between main agent and reviewer subgraph."""

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, ToolMessage
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
    # Get the last AI message to include in handoff (per docs pattern)
    last_ai_message = next(
        (
            msg
            for msg in reversed(runtime.state["messages"])
            if isinstance(msg, AIMessage)
        ),
        None,
    )

    # Create a tool message for the handoff
    transfer_message = ToolMessage(
        content="Transferred to Doubtful Deacon for code review",
        tool_call_id=runtime.tool_call_id,
    )

    # Build messages to pass - include last AI message and transfer message
    messages_to_pass = (
        [last_ai_message, transfer_message] if last_ai_message else [transfer_message]
    )

    return Command(
        goto="reviewer",
        update={
            "active_agent": "reviewer",
            "messages": messages_to_pass,
        },
        graph=Command.PARENT,
    )


@tool
def transfer_to_main_agent(runtime: ToolRuntime) -> Command:
    """
    Transfer back to the main agent (Shooby Dooby).

    Use this after completing your code review to hand control back
    to the main agent for further implementation or conversation.
    """
    # Get the last AI message to include in handoff (per docs pattern)
    last_ai_message = next(
        (
            msg
            for msg in reversed(runtime.state["messages"])
            if isinstance(msg, AIMessage)
        ),
        None,
    )

    # Create a tool message for the handoff
    transfer_message = ToolMessage(
        content="Transferred back to Shooby Dooby",
        tool_call_id=runtime.tool_call_id,
    )

    # Build messages to pass - include last AI message and transfer message
    messages_to_pass = (
        [last_ai_message, transfer_message] if last_ai_message else [transfer_message]
    )

    return Command(
        goto="main_agent",
        update={
            "active_agent": "main_agent",
            "messages": messages_to_pass,
        },
        graph=Command.PARENT,
    )


# Export tools - main agent gets request_code_review, reviewer gets transfer_to_main_agent
TOOLS = [request_code_review]
REVIEWER_TOOLS = [transfer_to_main_agent]
