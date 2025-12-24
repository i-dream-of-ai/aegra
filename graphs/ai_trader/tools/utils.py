"""Shared utility functions for tools."""

import json
from langchain_core.runnables import RunnableConfig


def get_qc_project_id(config: RunnableConfig) -> int | None:
    """Get QC project ID from LangGraph config."""
    # Handle both direct config dict and object with get method
    if hasattr(config, "get"):
        return config.get("configurable", {}).get("qc_project_id")
    # If it's a raw dict
    return config.get("configurable", {}).get("qc_project_id")


def format_error(message: str, details: dict | None = None) -> str:
    """
    Format an error response for the agent.

    Args:
        message: The main error message
        details: Optional dictionary of additional error details
    """
    response = {
        "error": True,
        "message": message,
    }
    if details:
        response.update(details)
    return json.dumps(response, indent=2)


def format_success(message: str, data: dict | None = None) -> str:
    """
    Format a success response for the agent.

    Args:
        message: The success message
        data: Optional dictionary of result data
    """
    response = {
        "success": True,
        "message": message,
    }
    if data:
        response.update(data)
    return json.dumps(response, indent=2)
