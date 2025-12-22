"""State schema for the AI Trader agent graph.

Defines the input and internal state structures for the LangGraph state machine.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated

from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages
from langgraph.managed import IsLastStep


@dataclass
class InputState:
    """Input state for the agent - the narrower interface to the outside world.

    This defines the structure of incoming data when invoking the graph.
    """

    messages: Annotated[Sequence[AnyMessage], add_messages] = field(
        default_factory=list
    )
    """
    Messages tracking the primary execution state of the agent.

    Typically accumulates a pattern of:
    1. HumanMessage - user input
    2. AIMessage with .tool_calls - agent picking tool(s) to use
    3. ToolMessage(s) - the responses from executed tools
    4. AIMessage without .tool_calls - agent responding to user
    5. HumanMessage - user responds with next turn

    The `add_messages` annotation ensures new messages merge with existing ones,
    updating by ID to maintain append-only state.
    """


@dataclass
class State(InputState):
    """Complete internal state of the agent.

    Extends InputState with additional attributes needed during execution.
    """

    is_last_step: IsLastStep = field(default=False)
    """
    Indicates whether the current step is the last one before recursion limit.

    This is a 'managed' variable, controlled by the state machine.
    It is set to 'True' when the step count reaches recursion_limit - 1.
    """

    # Subconscious injection context (populated by subconscious node)
    subconscious_context: str | None = None
    """
    Dynamic context injected by the subconscious layer.
    Contains skills and behaviors retrieved from the knowledge base.
    """

    # Flag to trigger reviewer subgraph
    request_review: bool = False
    """
    When True, routes to the Doubtful Deacon reviewer subgraph.
    Set by the request_code_review tool, cleared after review completes.
    """
