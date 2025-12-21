"""AI service tools for search and code assistance."""

import json
import os
import re
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool
from openai import AsyncOpenAI
from qc_api import qc_request
from supabase_client import SupabaseClient
from thread_context import get_qc_project_id_from_thread


@tool
async def check_initialization_errors(
    code: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Check Python code for potential initialization errors in the Initialize() method.

    Args:
        code: Python code to check
    """
    try:
        qc_project_id = await get_qc_project_id_from_thread(config)
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        if not code:
            return json.dumps({"error": True, "message": "code is required."})

        data = await qc_request(
            "/ai-services/check-initialization-errors",
            {"projectId": qc_project_id, "code": code},
        )

        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps(
            {
                "error": True,
                "message": f"Failed to check initialization errors: {str(e)}",
            }
        )


@tool
async def complete_code(
    code: str,
    cursor_position: int,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    AI code completion for QuantConnect algorithms.

    Args:
        code: Code context
        cursor_position: Cursor position in the code
    """
    try:
        qc_project_id = await get_qc_project_id_from_thread(config)
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        if not code:
            return json.dumps({"error": True, "message": "code is required."})

        data = await qc_request(
            "/ai-services/complete-code",
            {
                "projectId": qc_project_id,
                "code": code,
                "cursorPosition": cursor_position,
            },
        )

        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps(
            {
                "error": True,
                "message": f"Failed to get code completion: {str(e)}",
            }
        )


@tool
async def enhance_error_message(
    error_message: str,
    code: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Get enhanced error explanations with suggestions for fixes.

    Args:
        error_message: Error message to enhance
        code: Code context
    """
    try:
        qc_project_id = await get_qc_project_id_from_thread(config)
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        if not code:
            return json.dumps({"error": True, "message": "code is required."})

        data = await qc_request(
            "/ai-services/enhance-error",
            {
                "projectId": qc_project_id,
                "errorMessage": error_message,
                "code": code,
            },
        )

        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps(
            {
                "error": True,
                "message": f"Failed to enhance error message: {str(e)}",
            }
        )


@tool
async def check_syntax(
    code: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Check Python code syntax for errors before compiling.

    Args:
        code: Python code to check
    """
    try:
        qc_project_id = await get_qc_project_id_from_thread(config)
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        if not code:
            return json.dumps({"error": True, "message": "code is required."})

        data = await qc_request(
            "/ai-services/check-syntax",
            {"projectId": qc_project_id, "code": code},
        )

        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps(
            {
                "error": True,
                "message": f"Failed to check syntax: {str(e)}",
            }
        )


@tool
async def update_code_to_pep8(
    code: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Format Python code to PEP8 standards (snake_case, proper spacing, etc.).

    Args:
        code: Code to format
    """
    try:
        qc_project_id = await get_qc_project_id_from_thread(config)
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        if not code:
            return json.dumps({"error": True, "message": "code is required."})

        data = await qc_request(
            "/ai-services/pep8",
            {"projectId": qc_project_id, "code": code},
        )

        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps(
            {
                "error": True,
                "message": f"Failed to format code to PEP8: {str(e)}",
            }
        )


@tool
async def search_quantconnect(
    query: str,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Search QuantConnect documentation for API references, examples, and guides.

    Args:
        query: Search query
    """
    try:
        qc_project_id = await get_qc_project_id_from_thread(config)
        if not qc_project_id:
            return json.dumps({"error": True, "message": "No project context."})

        if not query:
            return json.dumps({"error": True, "message": "query is required."})

        data = await qc_request(
            "/ai-services/search",
            {"projectId": qc_project_id, "query": query},
        )

        return json.dumps(data, indent=2)

    except Exception as e:
        return json.dumps(
            {
                "error": True,
                "message": f"Failed to search: {str(e)}",
            }
        )


async def generate_embedding(text: str) -> list[float]:
    """Generate embedding using OpenAI."""
    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    response = await client.embeddings.create(
        model="text-embedding-3-large",
        input=text,
        encoding_format="float",
    )
    return response.data[0].embedding


@tool
async def search_local_algorithms(
    query: str,
    limit: int = 5,
) -> str:
    """
    Search ~1,500 QuantConnect algorithms using semantic search.

    Returns summaries (NOT full code). Use get_algorithm_code for full code.

    Args:
        query: Semantic search query (e.g., "mean reversion with bollinger bands")
        limit: Number of results (default: 5, max: 10)
    """
    try:
        if not query:
            return json.dumps({"error": True, "message": "query is required."})

        effective_limit = min(limit, 10)

        # Generate embedding
        embedding = await generate_embedding(query)
        vector_string = f"[{','.join(str(x) for x in embedding)}]"

        # Use service role for public algorithm_knowledge_base table
        client = SupabaseClient(use_service_role=True)
        results = await client.rpc(
            "match_algorithms",
            {
                "query_embedding": vector_string,
                "match_threshold": 0.4,
                "match_count": effective_limit,
            },
        )

        results = results or []

        return json.dumps(
            {
                "searchInfo": {
                    "query": query,
                    "resultsReturned": len(results),
                    "maxResults": effective_limit,
                    "hint": (
                        f"Found {len(results)} matching algorithms. Use get_algorithm_code with ID to get full code."
                        if results
                        else "No matching algorithms. Try different keywords."
                    ),
                },
                "results": [
                    {
                        "rank": i + 1,
                        "id": r.get("id"),
                        "file_path": r.get("file_path"),
                        "summary": r.get("summary"),
                        "tags": r.get("tags"),
                        "similarity": f"{r.get('similarity', 0) * 100:.1f}%"
                        if r.get("similarity")
                        else None,
                    }
                    for i, r in enumerate(results)
                ],
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps(
            {
                "error": True,
                "message": f"Failed to search: {str(e)}",
            }
        )


@tool
async def get_algorithm_code(algorithm_id: str) -> str:
    """
    Get full code of an algorithm from the knowledge base.

    Use after search_local_algorithms to retrieve complete implementation.

    Args:
        algorithm_id: The ID or file_path from search results
    """
    try:
        if not algorithm_id:
            return json.dumps(
                {
                    "error": True,
                    "message": "algorithm_id is required.",
                }
            )

        # Determine if it's a UUID or file path
        is_uuid = bool(
            re.match(
                r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                algorithm_id,
                re.IGNORECASE,
            )
        )

        # Build query params
        params = {
            "select": "id,file_path,code,summary,tags",
            "limit": "1",
        }
        if is_uuid:
            params["id"] = f"eq.{algorithm_id}"
        else:
            params["file_path"] = f"eq.{algorithm_id}"

        # Use service role for public algorithm_knowledge_base table
        client = SupabaseClient(use_service_role=True)
        data = await client.select("algorithm_knowledge_base", params)

        if not data:
            return json.dumps(
                {
                    "error": True,
                    "message": f"Algorithm not found: {algorithm_id}",
                }
            )

        algorithm = data[0]
        code = algorithm.get("code", "")

        # Truncate if too large (~20k tokens = 80k chars)
        max_chars = 80000
        if len(code) > max_chars:
            code = code[:max_chars] + "\n\n... [CODE TRUNCATED - File too large]"

        return json.dumps(
            {
                "id": algorithm.get("id"),
                "file_path": algorithm.get("file_path"),
                "summary": algorithm.get("summary"),
                "tags": algorithm.get("tags"),
                "code": code,
            },
            indent=2,
        )

    except Exception as e:
        return json.dumps(
            {
                "error": True,
                "message": f"Failed to get code: {str(e)}",
            }
        )
