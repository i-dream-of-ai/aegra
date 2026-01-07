"""Compile tools for QuantConnect."""

import asyncio
import json

from langchain.tools import tool, ToolRuntime

from ..context import Context
from ..qc_api import qc_request


@tool
async def create_compile(runtime: ToolRuntime[Context]) -> str:
    """
    Compile the current project on QuantConnect.
    Returns the compile ID needed for backtests and optimizations.
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request(
            "/compile/create",
            {"projectId": qc_project_id},
        )

        compile_id = result.get("compileId")
        state = result.get("state", "Unknown")

        # Wait for compilation to complete
        max_wait = 60
        waited = 0
        while state == "InQueue" and waited < max_wait:
            await asyncio.sleep(2)
            waited += 2
            status = await qc_request(
                "/compile/read",
                {"projectId": qc_project_id, "compileId": compile_id},
            )
            state = status.get("state", "Unknown")

        if state == "BuildSuccess":
            return json.dumps(
                {
                    "success": True,
                    "compile_id": compile_id,
                    "state": state,
                    "message": "Compilation successful. Ready for backtest or optimization.",
                }
            )
        elif state == "BuildError":
            logs = result.get("logs", [])
            return json.dumps(
                {
                    "error": True,
                    "compile_id": compile_id,
                    "state": state,
                    "logs": logs[:20] if logs else [],
                    "message": "Compilation failed.",
                }
            )
        else:
            return json.dumps(
                {
                    "success": True,
                    "compile_id": compile_id,
                    "state": state,
                    "message": f"Compile created with state: {state}",
                }
            )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to compile: {e!s}"})


@tool
async def read_compile(
    compile_id: str,
    runtime: ToolRuntime[Context],
) -> str:
    """
    Read compile status and logs.

    Args:
        compile_id: The compile ID to check
    """
    try:
        qc_project_id = runtime.context.get("qc_project_id")

        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request(
            "/compile/read",
            {"projectId": qc_project_id, "compileId": compile_id},
        )

        state = result.get("state", "Unknown")
        logs = result.get("logs", [])

        return json.dumps(
            {
                "compile_id": compile_id,
                "state": state,
                "logs": logs[:20] if logs else [],
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to read compile: {e!s}"})


# Export all tools
TOOLS = [create_compile, read_compile]
