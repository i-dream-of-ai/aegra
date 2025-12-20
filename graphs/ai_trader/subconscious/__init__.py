"""
Subconscious Layer for Python Agent

A simplified port of the TypeScript subconscious middleware.
Provides dynamic context injection based on conversation analysis.

Features:
- Skill retrieval from database (keyword + semantic search)
- Context synthesis using LLM
- Drift detection to avoid redundant injections
- SSE events for UI progress indicator

Architecture:
1. PLANNER - Analyzes conversation, generates retrieval queries
2. RETRIEVER - Fetches relevant skills from database
3. SYNTHESIZER - Integrates skills into actionable context
"""

from .middleware import SubconsciousMiddleware, create_subconscious_middleware
from .types import SubconsciousState, InjectionResult, SubconsciousEvent

__all__ = [
    "SubconsciousMiddleware",
    "create_subconscious_middleware",
    "SubconsciousState",
    "InjectionResult",
    "SubconsciousEvent",
]
