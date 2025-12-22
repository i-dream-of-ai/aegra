"""AI service tools for search and code assistance."""

import json
import os
import re

from langchain_core.tools import tool
from openai import AsyncOpenAI

from ai_trader.qc_api import qc_request
from ai_trader.supabase_client import SupabaseClient


def _get_qc_project_id():
    """Get QC project ID from LangGraph config."""
    from langgraph.config import get_config

    config = get_config()
    return config.get("configurable", {}).get("qc_project_id")


@tool
async def check_initialization_errors(code: str) -> str:
    """
    Check Python code for potential initialization errors.

    Args:
        code: Python code to check
    """
    try:
        qc_project_id = _get_qc_project_id()
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        data = await qc_request(
            "/ai/tools/backtest-init",
            {"projectId": qc_project_id, "code": code},
        )
        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to check initialization errors: {e!s}"}
        )


@tool
async def complete_code(code: str, cursor_position: int) -> str:
    """
    AI code completion for QuantConnect algorithms.

    Args:
        code: Code context
        cursor_position: Cursor position in the code
    """
    try:
        qc_project_id = _get_qc_project_id()
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        data = await qc_request(
            "/ai/tools/complete",
            {
                "projectId": qc_project_id,
                "code": code,
                "cursorPosition": cursor_position,
            },
        )
        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to get code completion: {e!s}"}
        )


@tool
async def enhance_error_message(error_message: str, code: str) -> str:
    """
    Get enhanced error explanations with suggestions for fixes.

    Args:
        error_message: Error message to enhance
        code: Code context
    """
    try:
        qc_project_id = _get_qc_project_id()
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        data = await qc_request(
            "/ai/tools/error-enhance",
            {"projectId": qc_project_id, "errorMessage": error_message, "code": code},
        )
        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to enhance error message: {e!s}"}
        )


@tool
async def check_syntax(code: str) -> str:
    """
    Check Python code syntax for errors before compiling.

    Args:
        code: Python code to check
    """
    try:
        qc_project_id = _get_qc_project_id()
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        data = await qc_request(
            "/ai/tools/syntax-check", {"projectId": qc_project_id, "code": code}
        )
        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to check syntax: {e!s}"})


@tool
async def update_code_to_pep8(code: str) -> str:
    """
    Format Python code to PEP8 standards.

    Args:
        code: Code to format
    """
    try:
        qc_project_id = _get_qc_project_id()
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        data = await qc_request(
            "/ai/tools/pep8-convert", {"projectId": qc_project_id, "code": code}
        )
        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps(
            {"error": True, "message": f"Failed to format code to PEP8: {e!s}"}
        )


@tool
async def search_quantconnect(query: str) -> str:
    """
    Search QuantConnect documentation.

    Args:
        query: Search query
    """
    try:
        qc_project_id = _get_qc_project_id()
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        # Use QC's structured search format with criteria
        data = await qc_request(
            "/ai/tools/search",
            {
                "language": "Py",
                "criteria": [
                    {"input": query, "type": "Docs", "count": 3},
                    {"input": query, "type": "Examples", "count": 3},
                ],
            },
        )
        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to search: {e!s}"})


async def _generate_embedding(text: str) -> list[float]:
    """Generate embedding using OpenAI."""
    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    response = await client.embeddings.create(
        model="text-embedding-3-large", input=text, encoding_format="float"
    )
    return response.data[0].embedding


@tool
async def search_local_algorithms(query: str, limit: int = 5) -> str:
    """
    Search ~1,500 QuantConnect algorithms using semantic search.

    Args:
        query: Semantic search query
        limit: Number of results (default: 5, max: 10)
    """
    try:
        if not query:
            return json.dumps({"error": True, "message": "query is required."})

        embedding = await _generate_embedding(query)
        vector_string = f"[{','.join(str(x) for x in embedding)}]"

        client = SupabaseClient(use_service_role=True)
        results = await client.rpc(
            "match_algorithms",
            {
                "query_embedding": vector_string,
                "match_threshold": 0.4,
                "match_count": min(limit, 10),
            },
        )

        return json.dumps(
            {
                "searchInfo": {"query": query, "resultsReturned": len(results or [])},
                "results": [
                    {
                        "rank": i + 1,
                        "id": r.get("id"),
                        "file_path": r.get("file_path"),
                        "summary": r.get("summary"),
                        "tags": r.get("tags"),
                    }
                    for i, r in enumerate(results or [])
                ],
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to search: {e!s}"})


@tool
async def get_algorithm_code(algorithm_id: str) -> str:
    """
    Get full code of an algorithm from the knowledge base.

    Args:
        algorithm_id: The ID or file_path from search results
    """
    try:
        if not algorithm_id:
            return json.dumps({"error": True, "message": "algorithm_id is required."})

        is_uuid = bool(
            re.match(
                r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                algorithm_id,
                re.IGNORECASE,
            )
        )
        params = {"select": "id,file_path,code,summary,tags", "limit": "1"}
        if is_uuid:
            params["id"] = f"eq.{algorithm_id}"
        else:
            params["file_path"] = f"eq.{algorithm_id}"

        client = SupabaseClient(use_service_role=True)
        data = await client.select("algorithm_knowledge_base", params)

        if not data:
            return json.dumps(
                {"error": True, "message": f"Algorithm not found: {algorithm_id}"}
            )

        algorithm = data[0]
        code = algorithm.get("code", "")
        if len(code) > 80000:
            code = code[:80000] + "\n\n... [CODE TRUNCATED]"

        return json.dumps(
            {
                "id": algorithm.get("id"),
                "file_path": algorithm.get("file_path"),
                "summary": algorithm.get("summary"),
                "code": code,
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps({"error": True, "message": f"Failed to get code: {e!s}"})


# Export all tools
TOOLS = [
    check_initialization_errors,
    complete_code,
    enhance_error_message,
    check_syntax,
    update_code_to_pep8,
    search_quantconnect,
    search_local_algorithms,
    get_algorithm_code,
]
