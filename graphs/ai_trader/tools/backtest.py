"""Backtest tools for QuantConnect."""

import asyncio
import json
import os
import time

from langgraph.runtime import get_runtime

from ai_trader.context import Context
from ai_trader.qc_api import qc_request


async def create_backtest(compile_id: str, backtest_name: str) -> str:
    """
    Create a backtest on QuantConnect using default parameter values.

    Args:
        compile_id: The QuantConnect compile ID
        backtest_name: Format: "[Symbols] [Strategy Type]" (e.g., "AAPL Momentum Strategy")
    """
    try:
        runtime = get_runtime(Context)
        qc_project_id = runtime.context.qc_project_id
        org_id = os.environ.get("QUANTCONNECT_ORGANIZATION_ID")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request(
            "/backtests/create",
            {
                "projectId": qc_project_id,
                "organizationId": org_id,
                "compileId": compile_id,
                "backtestName": backtest_name or f"Backtest {int(time.time())}",
            },
        )

        # Handle case where API returns a string instead of dict
        if isinstance(result, str):
            return json.dumps({"error": True, "message": f"Unexpected API response: {result}"})

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

        if isinstance(status_result, str):
            return json.dumps({"error": True, "message": f"Unexpected API response: {status_result}"})

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
            {"error": True, "message": f"Failed to create backtest: {e!s}"}
        )


async def read_backtest(backtest_id: str) -> str:
    """
    Read backtest status and key statistics from QuantConnect.

    Args:
        backtest_id: The backtest ID to read
    """
    try:
        runtime = get_runtime(Context)
        qc_project_id = runtime.context.qc_project_id

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request(
            "/backtests/read",
            {"projectId": qc_project_id, "backtestId": backtest_id},
        )

        # Handle case where API returns a string instead of dict
        if isinstance(result, str):
            return json.dumps({"error": True, "message": f"Unexpected API response: {result}"})

        backtest = result.get("backtest", {})
        if isinstance(backtest, list):
            backtest = backtest[0] if backtest else {}

        stats = backtest.get("statistics", {})

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
        return json.dumps({"error": True, "message": f"Failed to read backtest: {e!s}"})


async def read_backtest_chart(
    backtest_id: str, name: str, sample_count: int = 100
) -> str:
    """
    Read chart data from a backtest. Returns metadata for frontend to display.

    Args:
        backtest_id: The backtest ID
        name: Chart name (e.g., "Strategy Equity", "Benchmark", "Drawdown")
        sample_count: Number of data points (default: 100, max: 200)
    """
    try:
        runtime = get_runtime(Context)
        qc_project_id = runtime.context.qc_project_id

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

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

        if isinstance(data, str):
            return json.dumps({"error": True, "message": f"Unexpected API response: {data}"})

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
                "message": f"Failed to read chart: {e!s}",
                "hint": 'Common charts: "Strategy Equity", "Benchmark", "Drawdown".',
            }
        )


async def read_backtest_orders(
    backtest_id: str, page: int = 1, page_size: int = 50
) -> str:
    """
    Read paginated order history from a backtest.

    Args:
        backtest_id: The backtest ID
        page: Page number (default: 1)
        page_size: Orders per page (default: 50, max: 100)
    """
    try:
        runtime = get_runtime(Context)
        qc_project_id = runtime.context.qc_project_id

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

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

        if isinstance(data, str):
            return json.dumps({"error": True, "message": f"Unexpected API response: {data}"})

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
        return json.dumps({"error": True, "message": f"Failed to read orders: {e!s}"})


async def read_backtest_insights(
    backtest_id: str, start: int = 0, end: int = 100
) -> str:
    """
    Read insights from a backtest.

    Args:
        backtest_id: The backtest ID
        start: Start index (default: 0)
        end: End index (default: 100)
    """
    try:
        runtime = get_runtime(Context)
        qc_project_id = runtime.context.qc_project_id

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

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
        return json.dumps({"error": True, "message": f"Failed to read insights: {e!s}"})


async def list_backtests(page: int = 1, page_size: int = 10) -> str:
    """
    List backtests for current project with pagination.

    Args:
        page: Page number (default: 1)
        page_size: Results per page (default: 10, max: 20)
    """
    try:
        runtime = get_runtime(Context)
        qc_project_id = runtime.context.qc_project_id

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request(
            "/backtests/list",
            {"projectId": qc_project_id},
        )

        if isinstance(result, str):
            return json.dumps({"error": True, "message": f"Unexpected API response: {result}"})

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
            {"error": True, "message": f"Failed to list backtests: {e!s}"}
        )


async def update_backtest(backtest_id: str, name: str = None, note: str = None) -> str:
    """
    Update a backtest name and/or note.

    Args:
        backtest_id: The backtest ID
        name: New name (optional)
        note: Note/description (optional)
    """
    try:
        runtime = get_runtime(Context)
        qc_project_id = runtime.context.qc_project_id

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

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
            {"error": True, "message": f"Failed to update backtest: {e!s}"}
        )


async def delete_backtest(backtest_id: str) -> str:
    """
    Delete a backtest. This action cannot be undone.

    Args:
        backtest_id: The backtest ID to delete
    """
    try:
        runtime = get_runtime(Context)
        qc_project_id = runtime.context.qc_project_id

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

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
            {"error": True, "message": f"Failed to delete backtest: {e!s}"}
        )


# Export all tools
TOOLS = [
    create_backtest,
    read_backtest,
    read_backtest_chart,
    read_backtest_orders,
    read_backtest_insights,
    list_backtests,
    update_backtest,
    delete_backtest,
]
