"""Shared utility functions for tools."""

import asyncio
import json
from typing import Callable, Awaitable

from langchain_core.runnables import RunnableConfig
from langgraph.graph.ui import push_ui_message


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


async def stream_backtest_progress(
    qc_project_id: int,
    backtest_id: str,
    backtest_name: str,
    qc_request: Callable[..., Awaitable[dict]],
    max_polls: int = 120,
    poll_interval: float = 2.0,
) -> None:
    """
    Stream backtest progress updates as background task.

    Polls QC API and emits backtest-progress UI messages with live equity curve.
    Runs non-blocking - caller should fire and forget.

    Args:
        qc_project_id: QuantConnect project ID
        backtest_id: Backtest ID to monitor
        backtest_name: Display name for the backtest
        qc_request: Async function to make QC API requests
        max_polls: Maximum polling iterations (default 4 minutes)
        poll_interval: Seconds between polls
    """
    equity_curve = []

    # Emit initial progress
    push_ui_message("backtest-progress", {
        "backtestId": backtest_id,
        "backtestName": backtest_name,
        "status": "running",
        "progress": 0,
        "completed": False,
        "equityCurve": [],
    })

    for poll_num in range(max_polls):
        await asyncio.sleep(poll_interval)

        try:
            status_result = await qc_request(
                "/backtests/read",
                {"projectId": qc_project_id, "backtestId": backtest_id},
            )

            status_backtest = status_result.get("backtest", {})
            if isinstance(status_backtest, list):
                status_backtest = status_backtest[0] if status_backtest else {}

            # Check for errors
            if isinstance(status_backtest, dict) and (
                status_backtest.get("error") or status_backtest.get("hasInitializeError")
            ):
                error_msg = status_backtest.get("error", "Initialization error")
                push_ui_message("backtest-progress", {
                    "backtestId": backtest_id,
                    "backtestName": backtest_name,
                    "status": "error",
                    "progress": 0,
                    "completed": False,
                    "error": error_msg,
                    "equityCurve": equity_curve,
                })
                return

            # Extract progress and equity
            progress = status_backtest.get("progress", 0) if isinstance(status_backtest, dict) else 0
            runtime_stats = status_backtest.get("runtimeStatistics", {}) if isinstance(status_backtest, dict) else {}

            # Get current equity value
            equity_str = runtime_stats.get("Equity", "0")
            try:
                equity_value = float(str(equity_str).replace(",", "").replace("$", ""))
            except (ValueError, TypeError):
                equity_value = 0

            # Add equity point to curve
            if equity_value > 0:
                equity_curve.append({"x": poll_num, "y": equity_value})

            # Check if completed
            is_completed = status_backtest.get("completed", False) if isinstance(status_backtest, dict) else False

            # Build statistics for completed backtest
            statistics = None
            if is_completed:
                stats = status_backtest.get("statistics", {}) if isinstance(status_backtest, dict) else {}
                statistics = {
                    "totalReturn": stats.get("Net Profit"),
                    "cagr": stats.get("Compounding Annual Return"),
                    "sharpeRatio": stats.get("Sharpe Ratio"),
                    "maxDrawdown": stats.get("Drawdown"),
                    "winRate": stats.get("Win Rate"),
                    "totalTrades": stats.get("Total Orders"),
                }

            # Emit progress update
            push_ui_message("backtest-progress", {
                "backtestId": backtest_id,
                "backtestName": backtest_name,
                "status": "completed" if is_completed else "running",
                "progress": progress,
                "completed": is_completed,
                "equityCurve": equity_curve,
                "statistics": statistics,
            })

            if is_completed:
                return

        except Exception:
            # Don't fail silently but also don't crash - just stop streaming
            return


def start_backtest_streaming(
    qc_project_id: int,
    backtest_id: str,
    backtest_name: str,
    qc_request: Callable[..., Awaitable[dict]],
) -> asyncio.Task:
    """
    Start non-blocking backtest progress streaming.

    Returns immediately after spawning background task.
    The task will stream UI updates until backtest completes or times out.

    Args:
        qc_project_id: QuantConnect project ID
        backtest_id: Backtest ID to monitor
        backtest_name: Display name for the backtest
        qc_request: Async function to make QC API requests

    Returns:
        asyncio.Task that can be awaited or cancelled if needed
    """
    return asyncio.create_task(
        stream_backtest_progress(
            qc_project_id=qc_project_id,
            backtest_id=backtest_id,
            backtest_name=backtest_name,
            qc_request=qc_request,
        )
    )
