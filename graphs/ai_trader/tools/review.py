"""Code review tools - handoffs between main agent and reviewer subgraph."""

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
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
    # Create the tool result message for this handoff
    # The agents share state so the reviewer sees the full conversation
    transfer_message = ToolMessage(
        content="Transferred to Doubtful Deacon for code review. Please review the conversation and provide feedback.",
        tool_call_id=runtime.tool_call_id,
    )

    return Command(
        goto="reviewer",
        update={
            "active_agent": "reviewer",
            "messages": [transfer_message],
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
    # Create the tool result message for this handoff
    transfer_message = ToolMessage(
        content="Transferred back to Shooby Dooby. Review complete.",
        tool_call_id=runtime.tool_call_id,
    )

    return Command(
        goto="main_agent",
        update={
            "active_agent": "main_agent",
            "messages": [transfer_message],
        },
        graph=Command.PARENT,
    )


# Export tools - main agent gets request_code_review, reviewer gets transfer_to_main_agent
TOOLS = [request_code_review]
REVIEWER_TOOLS = [transfer_to_main_agent]
