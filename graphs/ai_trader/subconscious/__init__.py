"""
Subconscious Layer for Python Agent

A simplified port of the TypeScript subconscious middleware.
Provides dynamic context injection based on conversation analysis.

Features:
- Skill retrieval from database (keyword + semantic search)
- Context synthesis using LLM
- Drift detection to avoid redundant injections
- SSE events for UI progress indicator
- Outcome tracking for skill effectiveness learning
- Skill deduplication to prevent redundant injections
- Automatic skill merging for knowledge consolidation

Architecture:
1. PLANNER - Analyzes conversation, generates retrieval queries
2. RETRIEVER - Fetches relevant skills from database
3. SYNTHESIZER - Integrates skills into actionable context
4. OUTCOME_TRACKER - Records injection outcomes for learning
"""

from .deduplication import (
    deduplicate_by_content,
    deduplicate_by_embedding,
    find_duplicate_skills_in_db,
)
from .middleware import SubconsciousMiddleware, create_subconscious_middleware
from .outcome_tracker import (
    get_skill_effectiveness_stats,
    record_outcome,
    record_skill_injection,
)
from .skill_merger import auto_merge_duplicates, merge_skills
from .types import InjectionResult, SubconsciousEvent, SubconsciousState

__all__ = [
    # Middleware
    "SubconsciousMiddleware",
    "create_subconscious_middleware",
    # Types
    "SubconsciousState",
    "InjectionResult",
    "SubconsciousEvent",
    # Outcome tracking
    "record_skill_injection",
    "record_outcome",
    "get_skill_effectiveness_stats",
    # Deduplication
    "deduplicate_by_content",
    "deduplicate_by_embedding",
    "find_duplicate_skills_in_db",
    # Skill merging
    "merge_skills",
    "auto_merge_duplicates",
]
