"""API endpoints for serving agent configuration.

Serves the complete agent configuration from aegra.
Next.js fetches these at runtime instead of bundling at build time.
User-saved configs (from DB) override these defaults.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter

from ai_trader.prompts import (
    DEFAULT_MAIN_PROMPT,
    DEFAULT_REVIEWER_PROMPT,
    SUBCONSCIOUS_PROMPTS,
    SUMMARIZATION_CONFIG,
    TAG_ALGORITHM_CONFIG,
    get_default_prompts,
)

router = APIRouter(tags=["config"])

# =============================================================================
# Model Configurations
# =============================================================================

MODEL_MAX_TOKENS = {
    "claude-opus-4-5-20251101": 64000,
    "claude-sonnet-4-5-20250929": 64000,
    "claude-haiku-4-5-20251001": 64000,
    "claude-3-5-haiku-latest": 8192,
    "gpt-5.2": 100000,
    "gpt-5.2-pro": 100000,
    "gpt-5.1-codex-max": 100000,
    "gpt-5.1": 100000,
    "gpt-5": 100000,
    "gpt-5-mini": 32000,
    "gpt-5-nano": 16000,
}

MODEL_CONFIGS = {
    "claude-opus-4-5-20251101": {
        "maxTokens": 32000,
        "thinkingBudget": 32000,
        "hardLimit": 64000,
        "timeout": 120000,
        "maxRetries": 3,
    },
    "claude-sonnet-4-5-20250929": {
        "maxTokens": 32000,
        "thinkingBudget": 32000,
        "hardLimit": 64000,
        "timeout": 90000,
        "maxRetries": 3,
    },
    "claude-haiku-4-5-20251001": {
        "maxTokens": 32000,
        "thinkingBudget": 16000,
        "hardLimit": 64000,
        "timeout": 60000,
        "maxRetries": 3,
    },
    "claude-3-5-haiku-latest": {
        "maxTokens": 1024,
        "thinkingBudget": None,
        "hardLimit": 8192,
        "timeout": 30000,
        "maxRetries": 2,
    },
    "gpt-5.2": {
        "maxTokens": 50000,
        "thinkingBudget": None,
        "hardLimit": 100000,
        "reasoningEffort": "high",
        "verbosity": "medium",
        "compaction": None,
        "timeout": 120000,
        "maxRetries": 3,
    },
    "gpt-5.2-pro": {
        "maxTokens": 50000,
        "thinkingBudget": None,
        "hardLimit": 100000,
        "reasoningEffort": "xhigh",
        "verbosity": "medium",
        "compaction": None,
        "timeout": 180000,
        "maxRetries": 3,
    },
    "gpt-5.1-codex-max": {
        "maxTokens": 50000,
        "thinkingBudget": None,
        "hardLimit": 100000,
        "reasoningEffort": "high",
        "verbosity": "low",
        "compaction": None,
        "timeout": 120000,
        "maxRetries": 3,
    },
    "gpt-5.1": {
        "maxTokens": 50000,
        "thinkingBudget": None,
        "hardLimit": 100000,
        "reasoningEffort": "high",
        "timeout": 120000,
        "maxRetries": 3,
    },
    "gpt-5": {
        "maxTokens": 50000,
        "thinkingBudget": None,
        "hardLimit": 100000,
        "reasoningEffort": "medium",
        "timeout": 90000,
        "maxRetries": 3,
    },
    "gpt-5-mini": {
        "maxTokens": 16000,
        "thinkingBudget": None,
        "hardLimit": 32000,
        "reasoningEffort": "low",
        "timeout": 60000,
        "maxRetries": 2,
    },
    "gpt-5-nano": {
        "maxTokens": 8000,
        "thinkingBudget": None,
        "hardLimit": 16000,
        "reasoningEffort": "none",
        "verbosity": "low",
        "timeout": 30000,
        "maxRetries": 2,
    },
}

# =============================================================================
# Agent Configurations
# =============================================================================

DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "claude-opus-4-5-20251101")

AGENTS_CONFIG = {
    "main": {
        "name": "main-agent-1",
        "description": """Lead Quant Dev. Comes up with the approach then hands off to subagents for discussion.

Pass to subagents:
- The user's complete goal/idea - no editing allowed
- Any user constraints or preferences
- Assumptions (dates, cash, risk tolerance)
- Success criteria

Main agent will:
1. Write initial algorithm code base and backtest
2. Hand off to subagents for critique and advice
3. Debate and iterate until STRONG CONSENSUS
4. Loop until you have a conclusion for the theory
5. Return results
""",
        "model": "gpt-5.2",
        "maxTokens": 50000,
        "thinkingBudget": None,
        "reasoningEffort": "high",
        "verbosity": "low",
        "systemPrompt": DEFAULT_MAIN_PROMPT,
    },
    "reviewer": {
        "name": "support-agent-2",
        "description": """Expert quant algo dev focused on high yield. Challenge and validate code assessments. Look for overcomplexity, bugs, edge cases, best practices, risk management, and profitability blockers.
Agree where appropriate, disagree with evidence where needed.
Has access to algorithm search tools for finding best practice examples.""",
        "model": "gpt-5.2",
        "maxTokens": 50000,
        "thinkingBudget": None,
        "reasoningEffort": "high",
        "verbosity": "low",
        "systemPrompt": DEFAULT_REVIEWER_PROMPT,
    },
}

# =============================================================================
# Subconscious Agents Configuration
# =============================================================================

SUBCONSCIOUS_AGENTS = {
    "planner": {
        "name": "gam-planner",
        "description": "Analyzes conversation to understand user intent and generates targeted retrieval queries.",
        "model": "claude-haiku-4-5-20251001",
        "maxTokens": 4000,
        "thinking": {"enabled": False, "budgetTokens": 8000},
        "reasoning": {"effort": "medium"},
        "timeout": 30000,
        "maxRetries": 2,
        "temperature": 0.3,
    },
    "synthesizer": {
        "name": "gam-synthesizer",
        "description": "Filters and synthesizes retrieved skills into actionable context.",
        "model": "claude-haiku-4-5-20251001",
        "maxTokens": 4000,
        "thinking": {"enabled": False, "budgetTokens": 8000},
        "reasoning": {"effort": "medium"},
        "timeout": 30000,
        "maxRetries": 2,
        "temperature": 0.2,
    },
    "memoryAgent": {
        "name": "gam-memory",
        "description": "Generates intelligent abstracts from messages for long-term memory.",
        "model": "claude-haiku-4-5-20251001",
        "maxTokens": 200,
        "thinking": {"enabled": False, "budgetTokens": 2000},
        "reasoning": {"effort": "low"},
        "timeout": 15000,
        "maxRetries": 2,
        "temperature": 0.2,
    },
    "reflection": {
        "name": "gam-reflection",
        "description": "Evaluates if retrieved info is sufficient and generates follow-up queries.",
        "model": "claude-haiku-4-5-20251001",
        "maxTokens": 500,
        "thinking": {"enabled": False},
        "reasoning": {"effort": "low"},
        "timeout": 15000,
        "maxRetries": 2,
        "temperature": 0.2,
    },
}

RESEARCH_CONFIG = {
    "maxIterations": 1,
    "maxAbstracts": 10,
    "minMessageLength": 50,
    "maxPagesPerRequest": 5,
    "pageContentMaxLength": 2000,
    "maxSkillsPerQuery": 7,
    "minRelevanceScore": 0.3,
    "memoryContextMaxTokens": 4000,
    "currentRequestMaxLength": 2000,
    "maxMessagesForContext": 20,
}

DRIFT_CONFIG = {
    "minDriftScore": 0.3,
    "criticalDriftScore": 0.7,
    "minTurnsBetweenInjection": 2,
    "maxTrackedTopics": 20,
    "topicDecayRate": 0.9,
}

# =============================================================================
# Summarization Configuration
# =============================================================================

SUMMARIZATION_FULL_CONFIG = {
    "model": "gpt-5-mini",
    "maxTokens": 4000,
    "keepMessages": 6,
    "summaryPrompt": SUMMARIZATION_CONFIG.get("prompt", ""),
    "summaryPrefix": "Here is the preserved context from earlier in our conversation:\n\n",
}

# =============================================================================
# Micro Services Configuration
# =============================================================================

MICRO_SERVICES_CONFIG = {
    "tagAlgorithm": {
        "model": "gpt-5-mini",
        "reasoningEffort": "medium",
        "verbosity": "medium",
        "maxCodeLength": 15000,
        "prompt": TAG_ALGORITHM_CONFIG.get("prompt", ""),
        "taxonomy": TAG_ALGORITHM_CONFIG.get("taxonomy", {}),
    },
    "embeddings": {
        "search": {
            "model": "text-embedding-3-large",
            "dimensions": 3072,
            "matchThreshold": 0.4,
            "defaultLimit": 5,
            "maxLimit": 10,
        },
        "storage": {
            "model": "text-embedding-3-small",
            "dimensions": 1536,
            "cacheTtlSeconds": 60,
            "maxCacheEntries": 100,
        },
    },
}


# =============================================================================
# API Endpoints
# =============================================================================


@router.get("/config/agents")
async def get_agents_config() -> dict[str, Any]:
    """Get complete agent configurations (main, reviewer).

    Returns all settings needed for agent operation including prompts.
    User-saved configs from DB override these defaults.
    """
    return {
        "agents": AGENTS_CONFIG,
        "defaultModel": DEFAULT_MODEL,
    }


@router.get("/config/models")
async def get_models_config() -> dict[str, Any]:
    """Get model configurations and token limits."""
    return {
        "modelMaxTokens": MODEL_MAX_TOKENS,
        "modelConfigs": MODEL_CONFIGS,
    }


@router.get("/config/subconscious")
async def get_subconscious_config() -> dict[str, Any]:
    """Get subconscious layer configuration."""
    return {
        "agents": SUBCONSCIOUS_AGENTS,
        "research": RESEARCH_CONFIG,
        "drift": DRIFT_CONFIG,
        "prompts": SUBCONSCIOUS_PROMPTS,
    }


@router.get("/config/summarization")
async def get_summarization_config() -> dict[str, Any]:
    """Get summarization configuration."""
    return SUMMARIZATION_FULL_CONFIG


@router.get("/config/micro-services")
async def get_micro_services_config() -> dict[str, Any]:
    """Get micro services configuration (tagging, embeddings)."""
    return MICRO_SERVICES_CONFIG


@router.get("/config/all")
async def get_all_config() -> dict[str, Any]:
    """Get complete configuration in one call.

    Use this for initial load. Individual endpoints for refresh.
    """
    return {
        "agents": AGENTS_CONFIG,
        "defaultModel": DEFAULT_MODEL,
        "modelMaxTokens": MODEL_MAX_TOKENS,
        "modelConfigs": MODEL_CONFIGS,
        "subconscious": {
            "agents": SUBCONSCIOUS_AGENTS,
            "research": RESEARCH_CONFIG,
            "drift": DRIFT_CONFIG,
            "prompts": SUBCONSCIOUS_PROMPTS,
        },
        "summarization": SUMMARIZATION_FULL_CONFIG,
        "microServices": MICRO_SERVICES_CONFIG,
    }


# Legacy endpoint - redirect to new structure
@router.get("/prompts/defaults")
async def get_prompts_defaults() -> dict[str, str]:
    """Get default system prompts for agents.

    Legacy endpoint, use /config/agents for full config.
    """
    return get_default_prompts()
