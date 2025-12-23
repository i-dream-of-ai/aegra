"""Default prompts for AI Trader agents.

These are the source of truth for system prompts. The DB stores only
user customizations (overrides). If no custom prompt is in the DB,
these defaults are used.

Both Aegra and Next.js UI use the same DB:
- Aegra reads these local files for defaults
- User customizations saved to DB via Next.js UI
- ConfigFetcherMiddleware loads from DB, falls back to these
"""

from __future__ import annotations

import json
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """Load a prompt from a JSON file.

    Args:
        name: The prompt name (e.g., "main", "reviewer")

    Returns:
        The prompt string

    Raises:
        FileNotFoundError: If the prompt file doesn't exist
        KeyError: If the prompt key is missing in the JSON
    """
    path = _PROMPTS_DIR / f"{name}.json"
    with path.open() as f:
        data = json.load(f)
    return data["prompt"]


def load_prompt_json(name: str) -> dict:
    """Load the full JSON content of a prompt file.

    Args:
        name: The prompt name (e.g., "subconscious", "summarization")

    Returns:
        The full JSON dict
    """
    path = _PROMPTS_DIR / f"{name}.json"
    with path.open() as f:
        return json.load(f)


def get_default_prompts() -> dict[str, str]:
    """Get all default prompts.

    Returns:
        Dict mapping agent key to prompt string
    """
    return {
        "main": load_prompt("main"),
        "reviewer": load_prompt("reviewer"),
    }


# Pre-load agent prompts for convenience
DEFAULT_MAIN_PROMPT = load_prompt("main")
DEFAULT_REVIEWER_PROMPT = load_prompt("reviewer")

# Pre-load other configs
SUBCONSCIOUS_PROMPTS = load_prompt_json("subconscious")
SUMMARIZATION_CONFIG = load_prompt_json("summarization")
TAG_ALGORITHM_CONFIG = load_prompt_json("tag-algorithm")
