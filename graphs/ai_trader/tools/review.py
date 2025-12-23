"""Code review tools - handoffs between main agent and reviewer subgraph.

Pattern from official LangChain docs:
https://docs.langchain.com/oss/python/langchain/multi-agent/handoffs

Key quote: "The example below assumes only the handoff tool was called
(no parallel tool calls)"
"""

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
    # Per docs: Get the AI message that triggered this handoff (the last message)
    last_ai_message = runtime.state["messages"][-1]

    # Per docs: Create an artificial tool response to complete the pair
    transfer_message = ToolMessage(
        content="Transferred to Doubtful Deacon for code review",
        tool_call_id=runtime.tool_call_id,
    )
    return Command(
        goto="reviewer",
        update={
            "active_agent": "reviewer",
            # Pass only these two messages, not the full subagent history
            "messages": [last_ai_message, transfer_message],
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
    # Per docs: Get the AI message that triggered this handoff (the last message)
    last_ai_message = runtime.state["messages"][-1]

    # Per docs: Create an artificial tool response to complete the pair
    transfer_message = ToolMessage(
        content="Transferred back to Shooby Dooby",
        tool_call_id=runtime.tool_call_id,
    )
    return Command(
        goto="main_agent",
        update={
            "active_agent": "main_agent",
            # Pass only these two messages, not the full subagent history
            "messages": [last_ai_message, transfer_message],
        },
        graph=Command.PARENT,
    )


# Export tools - main agent gets request_code_review, reviewer gets transfer_to_main_agent
TOOLS = [request_code_review]
REVIEWER_TOOLS = [transfer_to_main_agent]
