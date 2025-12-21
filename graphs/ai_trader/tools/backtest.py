"""Backtest tools for QuantConnect."""

import asyncio
import json
import os
import time
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool
from qc_api import qc_request

from thread_context import get_qc_project_id_from_thread


async def no_project_error(config: RunnableConfig) -> str:
    """Return a JSON error with debug info when project context is missing."""
    configurable = config.get("configurable", {}) if config else {}

    # Try to get thread metadata for debugging
    thread_id = configurable.get("thread_id")
    thread_metadata = None
    if thread_id:
        from thread_context import get_thread_metadata
        thread_metadata = await get_thread_metadata(thread_id)

    debug_info = {
        "config_keys": list(config.keys()) if config else [],
        "configurable_keys": list(configurable.keys()),
        "thread_id": thread_id,
        "thread_metadata": thread_metadata,
    }
    return json.dumps({
        "error": True,
        "message": "No project context in thread metadata.",
        "debug": debug_info,
    })


@tool
async def create_backtest(
    compile_id: str,
    backtest_name: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Create a backtest on QuantConnect using default parameter values.

    Args:
        compile_id: The QuantConnect compile ID
        backtest_name: Format: "[Symbols] [Strategy Type]" (e.g., "AAPL Momentum Strategy")
    """
    try:
        qc_project_id = await get_qc_project_id_from_thread(config)
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return await no_project_error(config)

        result = await qc_request(
            "/backtests/create",
            {
                "projectId": qc_project_id,
                "organizationId": org_id,
                "compileId": compile_id,
                "backtestName": backtest_name or f"Backtest {int(time.time())}",
            },
        )

        backtest = result.get("backtest", {})
        if isinstance(backtest, list):
            backtest = backtest[0] if backtest else {}

        backtest_id = backtest.get("backtestId")

        # Wait briefly then check status
        await asyncio.sleep(5)

        status_result = await qc_request(
            "/backtests/read",
            {"projectId": qc_project_id, "backtestId": backtest_id},
        )

        status_backtest = status_result.get("backtest", {})
        if isinstance(status_backtest, list):
            status_backtest = status_backtest[0] if status_backtest else {}

        if status_backtest.get("error") or status_backtest.get("hasInitializeError"):
            error_msg = status_backtest.get("error", "Initialization error")
            return json.dumps(
                {
                    "success": False,
                    "backtest_id": backtest_id,
                    "error": error_msg,
                }
            )

        return json.dumps(
            {
                "success": True,
                "backtest_id": backtest_id,
                "backtest_name": backtest_name,
                "status": status_backtest.get("status", "Running"),
                "message": f"Backtest created! Use read_backtest with ID: {backtest_id}",
            }
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to create backtest: {str(e)}"}
        )


@tool
async def read_backtest(
    backtest_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Read backtest status and key statistics from QuantConnect.

    Args:
        backtest_id: The backtest ID to read
    """
    try:
        qc_project_id = await get_qc_project_id_from_thread(config)
        if not qc_project_id:
            return await no_project_error(config)

        result = await qc_request(
            "/backtests/read",
            {"projectId": qc_project_id, "backtestId": backtest_id},
        )

        # Ensure result is a dict
        if not isinstance(result, dict):
            return json.dumps({"error": True, "message": f"Unexpected API response type: {type(result).__name__}", "raw": str(result)[:200]})

        backtest = result.get("backtest", {})
        if isinstance(backtest, list):
            backtest = backtest[0] if backtest else {}
        if not isinstance(backtest, dict):
            return json.dumps({"error": True, "message": f"Unexpected backtest type: {type(backtest).__name__}", "raw": str(backtest)[:200]})

        stats = backtest.get("statistics", {})
        if not isinstance(stats, dict):
            stats = {}

        return json.dumps(
            {
                "backtest_id": backtest_id,
                "name": backtest.get("name", "Unknown"),
                "status": "Completed" if backtest.get("completed") else "Running",
                "completed": backtest.get("completed", False),
                "statistics": {
                    "net_profit": stats.get("Net Profit", "N/A"),
                    "cagr": stats.get("Compounding Annual Return", "N/A"),
                    "sharpe_ratio": stats.get("Sharpe Ratio", "N/A"),
                    "max_drawdown": stats.get("Drawdown", "N/A"),
                    "win_rate": stats.get("Win Rate", "N/A"),
                    "total_trades": stats.get("Total Trades", "N/A"),
                    "profit_factor": stats.get(
                        "Profit-Loss Ratio", stats.get("Expectancy", "N/A")
                    ),
                    "average_win": stats.get("Average Win", "N/A"),
                    "average_loss": stats.get("Average Loss", "N/A"),
                },
                "error": backtest.get("error") if backtest.get("error") else None,
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to read backtest: {str(e)}"}
        )


@tool
async def read_backtest_chart(
    backtest_id: str,
    name: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
    sample_count: int = 100,
) -> str:
    """
    Read chart data from a backtest. Returns metadata for frontend to display.

    Args:
        backtest_id: The backtest ID
        name: Chart name (e.g., "Strategy Equity", "Benchmark", "Drawdown")
        sample_count: Number of data points (default: 100, max: 200)
    """
    try:
        qc_project_id = await get_qc_project_id_from_thread(config)
        if not qc_project_id:
            return await no_project_error(config)

        effective_count = min(sample_count, 200)

        data = await qc_request(
            "/backtests/chart/read",
            {
                "projectId": qc_project_id,
                "backtestId": backtest_id,
                "name": name,
                "count": 1,
            },
        )

        chart_data = data.get("chart", data)
        series = chart_data.get("series", {})
        series_names = list(series.keys())

        if not series_names:
            return json.dumps(
                {
                    "error": True,
                    "message": f'Chart "{name}" has no series data.',
                    "backtest_id": backtest_id,
                }
            )

        return json.dumps(
            {
                "_chartRequest": True,
                "success": True,
                "project_id": qc_project_id,
                "backtest_id": backtest_id,
                "chart_name": name,
                "sample_count": effective_count,
                "series_names": series_names,
                "message": f'Chart "{name}" ready with {len(series_names)} series.',
            }
        )

    except Exception as e:
        return json.dumps(
            {
                "error": True,
                "message": f"Failed to read chart: {str(e)}",
                "hint": 'Common charts: "Strategy Equity", "Benchmark", "Drawdown".',
            }
        )


@tool
async def read_backtest_orders(
    backtest_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
    page: int = 1,
    page_size: int = 50,
) -> str:
    """
    Read paginated order history from a backtest.

    Args:
        backtest_id: The backtest ID
        page: Page number (default: 1)
        page_size: Orders per page (default: 50, max: 100)
    """
    try:
        qc_project_id = await get_qc_project_id_from_thread(config)
        if not qc_project_id:
            return await no_project_error(config)

        start = (page - 1) * page_size
        end = start + page_size

        data = await qc_request(
            "/backtests/orders/read",
            {
                "projectId": qc_project_id,
                "backtestId": backtest_id,
                "start": start,
                "end": end,
            },
        )

        orders = data.get("orders", [])
        total_orders = data.get("totalOrders", len(orders))
        total_pages = (total_orders + page_size - 1) // page_size

        return json.dumps(
            {
                "pagination": {
                    "current_page": page,
                    "page_size": page_size,
                    "total_results": total_orders,
                    "total_pages": total_pages,
                    "has_more_pages": page < total_pages,
                },
                "backtest_id": backtest_id,
                "orders": orders,
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to read orders: {str(e)}"}
        )


@tool
async def read_backtest_insights(
    backtest_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
    start: int = 0,
    end: int = 100,
) -> str:
    """
    Read insights from a backtest.

    Args:
        backtest_id: The backtest ID
        start: Start index (default: 0)
        end: End index (default: 100)
    """
    try:
        qc_project_id = await get_qc_project_id_from_thread(config)
        if not qc_project_id:
            return await no_project_error(config)

        data = await qc_request(
            "/backtests/insights/read",
            {
                "projectId": qc_project_id,
                "backtestId": backtest_id,
                "start": start,
                "end": end,
            },
        )

        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to read insights: {str(e)}"}
        )


@tool
async def list_backtests(
    config: Annotated[RunnableConfig, InjectedToolArg],
    page: int = 1,
    page_size: int = 10,
) -> str:
    """
    List backtests for current project with pagination.

    Args:
        page: Page number (default: 1)
        page_size: Results per page (default: 10, max: 20)
    """
    try:
        qc_project_id = await get_qc_project_id_from_thread(config)
        if not qc_project_id:
            return await no_project_error(config)

        result = await qc_request(
            "/backtests/list",
            {"projectId": qc_project_id},
        )

        all_backtests = result.get("backtests", [])
        total = len(all_backtests)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1

        start = (page - 1) * page_size
        end = start + page_size
        page_backtests = all_backtests[start:end]

        backtests = []
        for bt in page_backtests:
            stats = bt.get("statistics", {})
            backtests.append(
                {
                    "backtest_id": bt.get("backtestId"),
                    "name": bt.get("name", "Unknown"),
                    "status": "Completed" if bt.get("completed") else "Running",
                    "created": bt.get("created"),
                    "net_profit": stats.get("Net Profit"),
                    "cagr": stats.get("Compounding Annual Return"),
                    "sharpe_ratio": stats.get("Sharpe Ratio"),
                    "max_drawdown": stats.get("Drawdown"),
                }
            )

        return json.dumps(
            {
                "pagination": {
                    "current_page": page,
                    "page_size": page_size,
                    "total_results": total,
                    "total_pages": total_pages,
                    "has_more_pages": page < total_pages,
                },
                "backtests": backtests,
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to list backtests: {str(e)}"}
        )


@tool
async def update_backtest(
    backtest_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
    name: str = None,
    note: str = None,
) -> str:
    """
    Update a backtest name and/or note.

    Args:
        backtest_id: The backtest ID
        name: New name (optional)
        note: Note/description (optional)
    """
    try:
        qc_project_id = await get_qc_project_id_from_thread(config)
        if not qc_project_id:
            return await no_project_error(config)

        payload = {
            "projectId": qc_project_id,
            "backtestId": backtest_id,
        }
        if name:
            payload["name"] = name
        if note:
            payload["note"] = note

        await qc_request("/backtests/update", payload)

        updated = []
        if name:
            updated.append(f'name to "{name}"')
        if note:
            updated.append("note")

        return json.dumps(
            {
                "success": True,
                "message": f"Updated backtest: {', '.join(updated)}",
                "backtest_id": backtest_id,
            }
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to update backtest: {str(e)}"}
        )


@tool
async def delete_backtest(
    backtest_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Delete a backtest. This action cannot be undone.

    Args:
        backtest_id: The backtest ID to delete
    """
    try:
        qc_project_id = await get_qc_project_id_from_thread(config)
        if not qc_project_id:
            return await no_project_error(config)

        await qc_request(
            "/backtests/delete",
            {"projectId": qc_project_id, "backtestId": backtest_id},
        )

        return json.dumps(
            {
                "success": True,
                "message": f"Deleted backtest {backtest_id}.",
                "backtest_id": backtest_id,
            }
        )

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to delete backtest: {str(e)}"}
        )
