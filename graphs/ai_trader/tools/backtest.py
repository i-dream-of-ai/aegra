"""Backtest tools for QuantConnect."""

import asyncio
import json
import os
import time

from langchain.tools import tool, ToolRuntime
from langgraph.graph.ui import push_ui_message

from ..context import Context
from ..qc_api import qc_request
from .utils import start_backtest_streaming



@tool
async def create_backtest(
    compile_id: str,
    backtest_name: str,
    runtime: ToolRuntime[Context],
) -> str:
    """
    Create a backtest on QuantConnect using default parameter values.
    Streams live equity curve updates to the frontend during execution.

    Args:
        compile_id: The QuantConnect compile ID
        backtest_name: Format: "[Symbols] [Strategy Type]" (e.g., "AAPL Momentum Strategy")
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")
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

        backtest = result.get("backtest", {})
        if isinstance(backtest, list):
            backtest = backtest[0] if backtest else {}

        backtest_id = backtest.get("backtestId") if isinstance(backtest, dict) else None

        if not backtest_id:
            return json.dumps({"error": True, "message": "No backtest ID returned."})

        # Start non-blocking background streaming of backtest progress with live equity curve
        start_backtest_streaming(
            qc_project_id=qc_project_id,
            backtest_id=backtest_id,
            backtest_name=backtest_name,
            qc_request=qc_request,
        )

        return json.dumps({
            "success": True,
            "backtest_id": backtest_id,
            "backtest_name": backtest_name,
            "status": "Started",
            "message": f"Backtest started! Live progress streaming in background.",
        })

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to create backtest: {e!s}"}
        )


@tool
async def read_backtest(
    backtest_id: str,
    runtime: ToolRuntime[Context],
) -> str:
    """
    Read backtest status and key statistics from QuantConnect.

    Args:
        backtest_id: The backtest ID to read
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request(
            "/backtests/read",
            {"projectId": qc_project_id, "backtestId": backtest_id},
        )

        backtest = result.get("backtest", {})
        if isinstance(backtest, list):
            backtest = backtest[0] if backtest else {}

        stats = backtest.get("statistics", {}) if isinstance(backtest, dict) else {}

        # Parse total orders as integer if present (QC API uses "Total Orders" not "Total Trades")
        total_orders_raw = stats.get("Total Orders")
        total_orders = None
        if total_orders_raw is not None:
            try:
                total_orders = int(float(str(total_orders_raw).replace(",", "")))
            except (ValueError, TypeError):
                total_orders = total_orders_raw

        # Build UI-friendly data structure
        ui_data = {
            "backtestId": backtest_id,
            "name": backtest.get("name", "Unknown") if isinstance(backtest, dict) else "Unknown",
            "status": "Completed" if (isinstance(backtest, dict) and backtest.get("completed")) else "Running",
            "completed": backtest.get("completed", False) if isinstance(backtest, dict) else False,
            "error": backtest.get("error") if isinstance(backtest, dict) and backtest.get("error") else None,
            "created": backtest.get("created") if isinstance(backtest, dict) else None,
            "summary": {
                "totalReturn": stats.get("Net Profit"),
                "annualReturn": stats.get("Compounding Annual Return"),
                "sharpeRatio": stats.get("Sharpe Ratio"),
                "drawdown": stats.get("Drawdown"),
                "maxDrawdown": stats.get("Drawdown"),  # Alias for UI
                "winRate": stats.get("Win Rate"),
                "totalTrades": total_orders,  # QC uses "Total Orders"
                "profitFactor": stats.get("Profit-Loss Ratio", stats.get("Expectancy")),
                "averageWin": stats.get("Average Win"),
                "averageLoss": stats.get("Average Loss"),
                "startingEquity": stats.get("Start Equity"),
                "endingEquity": stats.get("End Equity"),
                "probabilisticSharpeRatio": stats.get("Probabilistic Sharpe Ratio"),
                "portfolioTurnover": stats.get("Portfolio Turnover"),
            },
            "allStatistics": stats,  # Pass all stats for detailed view
        }

        # Emit UI component via generative UI (linked to tool call message)
        push_ui_message("backtest-stats", ui_data, message={"id": runtime.tool_call_id})

        # Return JSON for LLM context (legacy compatibility)
        return json.dumps(
            {
                "backtest_id": backtest_id,
                "name": ui_data["name"],
                "status": ui_data["status"],
                "completed": ui_data["completed"],
                "statistics": {
                    "net_profit": stats.get("Net Profit", "N/A"),
                    "cagr": stats.get("Compounding Annual Return", "N/A"),
                    "sharpe_ratio": stats.get("Sharpe Ratio", "N/A"),
                    "max_drawdown": stats.get("Drawdown", "N/A"),
                    "win_rate": stats.get("Win Rate", "N/A"),
                    "total_orders": stats.get("Total Orders", "N/A"),
                    "profit_factor": stats.get(
                        "Profit-Loss Ratio", stats.get("Expectancy", "N/A")
                    ),
                    "average_win": stats.get("Average Win", "N/A"),
                    "average_loss": stats.get("Average Loss", "N/A"),
                },
                "error": ui_data["error"],
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to read backtest: {e!s}"})


@tool
async def read_backtest_chart(
    backtest_id: str,
    name: str,
    runtime: ToolRuntime[Context],
    sample_count: int = 100,
) -> str:
    """
    Read chart data from a backtest. Triggers chart generation, polls until ready, returns data.

    This tool handles the full QC chart lifecycle:
    1. Initial request triggers chart generation on QC servers
    2. Polls with short delays until chart data is populated
    3. Returns complete chart with series data

    Args:
        backtest_id: The backtest ID
        name: Chart name (e.g., "Strategy Equity", "Benchmark", "Drawdown")
        sample_count: Number of data points (default: 100, max: 500)
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        effective_count = min(max(sample_count, 10), 500)
        
        # Polling configuration
        max_attempts = 5
        poll_delay = 1.5  # seconds between polls
        
        chart_data = None
        series = {}
        
        for attempt in range(max_attempts):
            data = await qc_request(
                "/backtests/chart/read",
                {
                    "projectId": qc_project_id,
                    "backtestId": backtest_id,
                    "name": name,
                    "count": effective_count,
                },
            )
            
            chart_data = data.get("chart", data)
            series = chart_data.get("series", {})
            
            # Check if we have actual data points in any series
            has_data = False
            for series_name, series_info in series.items():
                values = series_info.get("values", []) if isinstance(series_info, dict) else []
                if values:
                    has_data = True
                    break
            
            if has_data:
                break
            
            # If no data yet and not last attempt, wait and retry
            if attempt < max_attempts - 1:
                await asyncio.sleep(poll_delay)
        
        series_names = list(series.keys())
        
        if not series_names:
            return json.dumps(
                {
                    "error": True,
                    "message": f'Chart "{name}" has no series data after {max_attempts} attempts.',
                    "backtest_id": backtest_id,
                    "hint": 'Common charts: "Strategy Equity", "Benchmark", "Drawdown".',
                }
            )
        
        # Build series summary with data point counts
        series_summary = {}
        for series_name, series_info in series.items():
            if isinstance(series_info, dict):
                values = series_info.get("values", [])
                series_summary[series_name] = {
                    "data_points": len(values),
                    "unit": series_info.get("unit", ""),
                    "series_type": series_info.get("seriesType", ""),
                }

        # Build series summaries with first/last/min/max for display
        series_summaries = {}
        for series_name, series_info in series.items():
            if isinstance(series_info, dict):
                values = series_info.get("values", [])
                if values:
                    # values is array of [timestamp, value] pairs
                    numeric_values = [v[1] for v in values if len(v) >= 2]
                    series_summaries[series_name] = {
                        "dataPoints": len(values),
                        "first": numeric_values[0] if numeric_values else 0,
                        "last": numeric_values[-1] if numeric_values else 0,
                        "min": min(numeric_values) if numeric_values else 0,
                        "max": max(numeric_values) if numeric_values else 0,
                    }

        # Build UI data matching ChartResultDisplay expected structure
        ui_data = {
            "chartInfo": {
                "backtestId": backtest_id,
                "chartName": name,
                "seriesCount": len(series_names),
                "seriesNames": series_names,
                "sampleCount": effective_count,
            },
            "seriesSummaries": series_summaries,
            "chart": {
                "name": name,
                "chartType": chart_data.get("chartType", 0),
                "series": series,
            },
        }

        # Emit chart UI component via generative UI (linked to tool call message)
        push_ui_message("chart", ui_data, message={"id": runtime.tool_call_id})

        return json.dumps(
            {
                "success": True,
                "chart_name": name,
                "series_count": len(series_names),
                "series_names": series_names,
                "series_summaries": series_summaries,
                "message": f'Chart "{name}" loaded. The chart is displayed to the user.',
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


@tool
async def read_backtest_orders(
    backtest_id: str,
    runtime: ToolRuntime[Context],
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
        qc_project_id = runtime.context.get("qc_project_id")

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

        orders = data.get("orders", [])
        total_orders = data.get("totalOrders", len(orders))
        total_pages = (total_orders + page_size - 1) // page_size

        ui_data = {
            "backtest_id": backtest_id,
            "orders": orders,
            "pagination": {
                "current_page": page,
                "page_size": page_size,
                "total_results": total_orders,
                "total_pages": total_pages,
                "has_more_pages": page < total_pages,
            },
        }

        # Emit generative UI component for clean order display
        push_ui_message("backtest-orders", ui_data, message={"id": runtime.tool_call_id})

        # Return summary for LLM context (not full orders to avoid context bloat)
        return json.dumps(
            {
                "backtest_id": backtest_id,
                "total_orders": total_orders,
                "page": page,
                "page_size": page_size,
                "has_more_pages": page < total_pages,
                "summary": f"Retrieved {len(orders)} orders (page {page}/{total_pages}). Full order data displayed in UI.",
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to read orders: {e!s}"})


@tool
async def read_backtest_insights(
    backtest_id: str,
    runtime: ToolRuntime[Context],
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
        qc_project_id = runtime.context.get("qc_project_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        data = await qc_request(
            "/backtests/read/insights",
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


@tool
async def list_backtests(
    runtime: ToolRuntime[Context],
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
        qc_project_id = runtime.context.get("qc_project_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request(
            "/backtests/list",
            {"projectId": qc_project_id, "includeStatistics": True},
        )

        all_backtests = result.get("backtests", [])
        total = len(all_backtests)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 1

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_backtests = all_backtests[start_idx:end_idx]

        backtests = []
        for bt in page_backtests:
            # With includeStatistics=True, stats are inline in the backtest object
            backtests.append(
                {
                    "backtest_id": bt.get("backtestId"),
                    "name": bt.get("name", "Unknown"),
                    "status": bt.get("status", "Unknown"),
                    "created": bt.get("created"),
                    "net_profit": bt.get("netProfit"),
                    "cagr": bt.get("compoundingAnnualReturn"),
                    "sharpe_ratio": bt.get("sharpeRatio"),
                    "max_drawdown": bt.get("drawdown"),
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


@tool
async def update_backtest(
    backtest_id: str,
    runtime: ToolRuntime[Context],
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
        qc_project_id = runtime.context.get("qc_project_id")

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


@tool
async def delete_backtest(
    backtest_id: str,
    runtime: ToolRuntime[Context],
) -> str:
    """
    Delete a backtest. This action cannot be undone.

    Args:
        backtest_id: The backtest ID to delete
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")

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
