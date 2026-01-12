"""AI Trader configuration module.

This is the Python equivalent of the Next.js config/ai-config.js.
All agent configurations, model settings, and subconscious configs live here.

Both Aegra and Next.js use the same DB:
- These defaults are used when DB doesn't have overrides
- User customizations are saved to DB and read by ConfigFetcherMiddleware
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# =============================================================================
# PROMPT LOADING
# =============================================================================

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def load_prompt(name: str) -> dict[str, Any]:
    """Load a prompt JSON file.

    Args:
        name: The prompt file name (without .json extension)

    Returns:
        The parsed JSON content
    """
    path = _PROMPTS_DIR / f"{name}.json"
    with path.open() as f:
        return json.load(f)


# Pre-load prompts
MAIN_PROMPT = load_prompt("main")
REVIEWER_PROMPT = load_prompt("reviewer")
SUBCONSCIOUS_PROMPTS = load_prompt("subconscious")
SUMMARIZATION_CONFIG = load_prompt("summarization")
TAG_ALGORITHM_CONFIG = load_prompt("tag-algorithm")


# =============================================================================
# MODEL CONFIGURATIONS
# =============================================================================

# Default model (can be overridden by DB config)
DEFAULT_MODEL = "claude-opus-4-5-20251101"

# Model max token limits (hard caps from the API)
MODEL_MAX_TOKENS = {
    "claude-opus-4-5-20251101": 64000,
    "claude-sonnet-4-5-20250929": 64000,
    "claude-haiku-4-5-20251001": 64000,
    "claude-3-5-haiku-latest": 8192,
    # GPT-5.2 family
    "gpt-5.2": 100000,
    "gpt-5.2-pro": 100000,
    "gpt-5.1-codex-max": 100000,
    # GPT-5.x family
    "gpt-5.1": 100000,
    "gpt-5": 100000,
    "gpt-5-mini": 32000,
    "gpt-5-nano": 16000,
}


# =============================================================================
# AGENT CONFIGURATIONS
# =============================================================================

AGENTS = {
    "main": {
        "name": "main-agent-1",
        "description": "Lead Quant Dev - writes code and hands off for review",
        "model": DEFAULT_MODEL,  # claude-opus-4-5-20251101
        "max_tokens": 32000,
        "thinking_budget": 10000,  # Extended thinking for complex tasks
        "reasoning_effort": None,  # Claude uses thinking_budget instead
        "verbosity": "low",
        "system_prompt": MAIN_PROMPT["prompt"],
    },
    "reviewer": {
        "name": "support-agent-2",
        "description": "Code Reviewer (Doubtful Deacon) - critiques and improves",
        "model": DEFAULT_MODEL,  # claude-opus-4-5-20251101
        "max_tokens": 32000,
        "thinking_budget": 10000,  # Extended thinking for thorough reviews
        "reasoning_effort": None,  # Claude uses thinking_budget instead
        "verbosity": "low",
        "system_prompt": REVIEWER_PROMPT["prompt"],
    },
}


# =============================================================================
# SUBCONSCIOUS LAYER CONFIGURATION
# =============================================================================


def get_model_provider(model: str) -> str:
    """Detect if a model is Claude or GPT."""
    if model.startswith("claude"):
        return "claude"
    if model.startswith("gpt"):
        return "gpt"
    return "unknown"


def supports_thinking(model: str) -> bool:
    """Check if model supports extended thinking (Claude 4.5+)."""
    return "claude" in model and any(
        x in model for x in ["opus-4", "sonnet-4", "haiku-4"]
    )


def supports_reasoning(model: str) -> bool:
    """Check if model supports reasoning effort (GPT-5+)."""
    return model.startswith("gpt-5")


SUBCONSCIOUS_AGENTS = {
    "planner": {
        "name": "gam-planner",
        "description": "Analyzes conversation and generates retrieval queries",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 4000,
        "thinking": {"enabled": False, "budget_tokens": 8000},
        "reasoning": {"effort": "medium"},
        "timeout": 30000,
        "max_retries": 2,
        "temperature": 0.3,
    },
    "synthesizer": {
        "name": "gam-synthesizer",
        "description": "Integrates skills into actionable context",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 4000,
        "thinking": {"enabled": False, "budget_tokens": 8000},
        "reasoning": {"effort": "medium"},
        "timeout": 30000,
        "max_retries": 2,
        "temperature": 0.2,
    },
    "memory_agent": {
        "name": "gam-memory",
        "description": "Generates intelligent abstracts from messages",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 200,
        "thinking": {"enabled": False, "budget_tokens": 2000},
        "reasoning": {"effort": "low"},
        "timeout": 15000,
        "max_retries": 2,
        "temperature": 0.2,
    },
    "reflection": {
        "name": "gam-reflection",
        "description": "Evaluates if info is sufficient and generates follow-ups",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 500,
        "thinking": {"enabled": False},
        "reasoning": {"effort": "low"},
        "timeout": 15000,
        "max_retries": 2,
        "temperature": 0.2,
    },
}

RESEARCH_CONFIG = {
    "max_iterations": 1,
    "max_abstracts": 10,
    "min_message_length": 50,
    "max_pages_per_request": 5,
    "page_content_max_length": 2000,
    "max_skills_per_query": 7,
    "min_relevance_score": 0.3,
    "memory_context_max_tokens": 4000,
    "current_request_max_length": 2000,
    "max_messages_for_context": 20,
}

DRIFT_CONFIG = {
    "min_drift_score": 0.3,
    "critical_drift_score": 0.7,
    "min_turns_between_injection": 2,
    "max_tracked_topics": 20,
    "topic_decay_rate": 0.9,
}


# =============================================================================
# SUMMARIZATION CONFIGURATION
# =============================================================================

SUMMARIZATION_CONFIG_FULL = {
    "model": "gpt-5-mini",
    "max_tokens": 4000,
    "keep_messages": 6,
    "summary_prompt": SUMMARIZATION_CONFIG["prompt"],
    "summary_prefix": "Here is the preserved context from earlier in our conversation:\n\n",
}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def get_agent_config(agent_name: str) -> dict[str, Any]:
    """Get agent config with model-appropriate settings."""
    if agent_name in AGENTS:
        return AGENTS[agent_name]
    if agent_name in SUBCONSCIOUS_AGENTS:
        agent = SUBCONSCIOUS_AGENTS[agent_name]
        provider = get_model_provider(agent["model"])
        return {
            **agent,
            "provider": provider,
            "use_thinking": (
                provider == "claude"
                and agent.get("thinking", {}).get("enabled")
                and supports_thinking(agent["model"])
            ),
            "use_reasoning": provider == "gpt" and supports_reasoning(agent["model"]),
        }
    raise ValueError(f"Unknown agent: {agent_name}")


def get_default_main_prompt() -> str:
    """Get the default main agent system prompt."""
    return MAIN_PROMPT["prompt"]


def get_default_reviewer_prompt() -> str:
    """Get the default reviewer agent system prompt."""
    return REVIEWER_PROMPT["prompt"]
