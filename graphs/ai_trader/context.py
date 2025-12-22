"""Runtime context schema for the AI Trader agent.

This context is passed to the agent at invocation time and is accessible
in middleware via `request.runtime.context`.

Context is a TypedDict (not a dataclass) per LangChain v1.0 requirements.
"""

from __future__ import annotations

from typing import TypedDict


class Context(TypedDict, total=False):
    """Runtime context for the AI Trader agent.

    Passed at invocation time by runs.py, which fetches config from DB.
    Accessible in middleware via `request.runtime.context`.

    All config values (model, prompts, budgets) come from the DB.
    runs.py calls fetch_project_config() to load these before invoking.
    """

    # User authentication
    access_token: str | None
    user_id: str | None

    # Project context
    project_db_id: str | None
    qc_project_id: int | None

    # Main agent config - loaded from DB by runs.py
    model: str | None
    thinking_budget: int | None
    reasoning_effort: str | None
    verbosity: str | None
    system_prompt: str | None  # Custom prompt from DB (optional)

    # Reviewer agent config
    reviewer_model: str | None
    reviewer_thinking_budget: int | None
    reviewer_reasoning_effort: str | None
    reviewer_prompt: str | None

    # Feature flags
    subconscious_enabled: bool
