"""API endpoint for serving default agent prompts.

Exposes prompts from the single source of truth (ai_trader/prompts/).
Next.js calls this instead of maintaining duplicate prompt files.
"""

from fastapi import APIRouter

from ai_trader.prompts import get_default_prompts

router = APIRouter(tags=["prompts"])


@router.get("/prompts/defaults")
async def get_prompts_defaults() -> dict[str, str]:
    """Get default system prompts for all agents.
    
    Returns:
        Dict mapping agent key to prompt string, e.g.:
        {"main": "...", "reviewer": "..."}
    """
    return get_default_prompts()
