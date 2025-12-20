"""Compile tools for QuantConnect."""

import os
import json
import asyncio
from typing import Annotated
from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig
from qc_api import qc_request


def get_qc_project_id(config: RunnableConfig) -> int | None:
    """Extract qc_project_id from RunnableConfig."""
    configurable = config.get("configurable", {})
    project_id = configurable.get("qc_project_id")
    if project_id is not None:
        return int(project_id)
    env_id = os.environ.get("QC_PROJECT_ID")
    return int(env_id) if env_id else None


@tool
async def create_compile(
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Compile the current project on QuantConnect.
    Returns the compile ID needed for backtests and optimizations.
    """
    try:
        qc_project_id = get_qc_project_id(config)
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
            return json.dumps({
                "success": True,
                "compile_id": compile_id,
                "state": state,
                "message": "Compilation successful. Ready for backtest or optimization.",
            })
        elif state == "BuildError":
            logs = result.get("logs", [])
            return json.dumps({
                "error": True,
                "compile_id": compile_id,
                "state": state,
                "logs": logs[:20] if logs else [],
                "message": "Compilation failed.",
            })
        else:
            return json.dumps({
                "success": True,
                "compile_id": compile_id,
                "state": state,
                "message": f"Compile created with state: {state}",
            })

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to compile: {str(e)}"})


@tool
async def read_compile(
    compile_id: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Read compile status and logs.

    Args:
        compile_id: The compile ID to check
    """
    try:
        qc_project_id = get_qc_project_id(config)
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        result = await qc_request(
            "/compile/read",
            {"projectId": qc_project_id, "compileId": compile_id},
        )

        state = result.get("state", "Unknown")
        logs = result.get("logs", [])

        return json.dumps({
            "compile_id": compile_id,
            "state": state,
            "logs": logs[:20] if logs else [],
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to read compile: {str(e)}"})
