"""AI service tools for search and code assistance."""

import json
import os
import re

from langchain_core.tools import tool
from openai import AsyncOpenAI

from ai_trader.qc_api import qc_request
from ai_trader.supabase_client import SupabaseClient
from ai_trader.tools.utils import format_error, format_success


@tool
async def check_initialization_errors(code: str, file_name: str = "main.py") -> str:
    """
    Check Python code for potential initialization errors by running a short backtest.

    Args:
        code: Python code to check
        file_name: Name of the file (default: main.py)
    """
    try:
        data = await qc_request(
            "/ai/tools/backtest-init",
            {
                "language": "Py",
                "files": [{"name": file_name, "content": code}],
            },
        )
        return json.dumps(data, indent=2)

    except Exception as e:
        return format_error(f"Failed to check initialization errors: {str(e)}")


@tool
async def complete_code(sentence: str, response_limit: int = 10) -> str:
    """
    AI code completion for QuantConnect algorithms.

    Args:
        sentence: The code fragment to complete (e.g., "self.add_eq", "AddEq")
        response_limit: Maximum number of completion suggestions (default: 10)
    """
    try:
        data = await qc_request(
            "/ai/tools/complete",
            {
                "language": "Py",
                "sentence": sentence,
                "responseSizeLimit": response_limit,
            },
        )
        return json.dumps(data, indent=2)

    except Exception as e:
        return format_error(f"Failed to get code completion: {str(e)}")


@tool
async def enhance_error_message(error_message: str, stacktrace: str = None) -> str:
    """
    Get enhanced error explanations with suggestions for fixes.

    Args:
        error_message: Error message to enhance
        stacktrace: Optional stack trace for additional context
    """
    try:
        error_obj = {"message": error_message}
        if stacktrace:
            error_obj["stacktrace"] = stacktrace

        data = await qc_request(
            "/ai/tools/error-enhance",
            {
                "language": "Py",
                "error": error_obj,
            },
        )
        return json.dumps(data, indent=2)

    except Exception as e:
        return format_error(f"Failed to enhance error message: {str(e)}")


@tool
async def check_syntax(code: str, file_name: str = "main.py") -> str:
    """
    Check Python code syntax for errors before compiling.

    Args:
        code: Python code to check
        file_name: Name of the file (default: main.py)
    """
    try:
        data = await qc_request(
            "/ai/tools/syntax-check",
            {
                "language": "Py",
                "files": [{"name": file_name, "content": code}],
            },
        )
        return json.dumps(data, indent=2)

    except Exception as e:
        return format_error(f"Failed to check syntax: {str(e)}")


@tool
async def update_code_to_pep8(code: str, file_name: str = "main.py") -> str:
    """
    Format Python code to PEP8 standards.

    Args:
        code: Code to format
        file_name: Name of the file (default: main.py)
    """
    try:
        data = await qc_request(
            "/ai/tools/pep8-convert",
            {
                "files": [{"name": file_name, "content": code}],
            },
        )
        return json.dumps(data, indent=2)

    except Exception as e:
        return format_error(f"Failed to format code to PEP8: {str(e)}")


@tool
async def search_quantconnect(query: str) -> str:
    """
    Search QuantConnect documentation and examples.

    Args:
        query: Search query
    """
    try:
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
        return format_error(f"Failed to search: {str(e)}")


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
            return format_error("query is required.")

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
        return format_error(f"Failed to search: {str(e)}")


@tool
async def get_algorithm_code(algorithm_id: str) -> str:
    """
    Get full code of an algorithm from the knowledge base.

    Args:
        algorithm_id: The ID or file_path from search results
    """
    try:
        if not algorithm_id:
            return format_error("algorithm_id is required.")

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
            return format_error(f"Algorithm not found: {algorithm_id}")

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
        return format_error(f"Failed to get code: {str(e)}")


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
