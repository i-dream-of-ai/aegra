"""Web search tool using DuckDuckGo - no API key required."""

from langchain_community.tools import DuckDuckGoSearchResults
from langchain_core.tools import tool


# Create the DuckDuckGo search instance
_ddg_search = DuckDuckGoSearchResults(
    num_results=5,
)


@tool
def web_search(query: str) -> str:
    """Search the web for current information.

    Use this tool to find up-to-date information about:
    - Market news and events
    - Company information
    - Trading concepts and strategies
    - Technical analysis methods
    - Any other current information not in your training data

    Args:
        query: The search query string

    Returns:
        Search results with titles, snippets, and links
    """
    try:
        results = _ddg_search.invoke(query)
        if isinstance(results, list):
            # Format results nicely
            formatted = []
            for i, r in enumerate(results, 1):
                title = r.get("title", "No title")
                snippet = r.get("snippet", r.get("body", "No description"))
                link = r.get("link", r.get("href", ""))
                formatted.append(f"{i}. {title}\n   {snippet}\n   URL: {link}")
            return "\n\n".join(formatted) if formatted else "No results found."
        return str(results)
    except Exception as e:
        return f"Search failed: {e}"


# Export for graph.py
TOOLS = [web_search]
