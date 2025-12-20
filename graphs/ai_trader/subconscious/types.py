"""
Type definitions for Subconscious Layer
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Literal


@dataclass
class RetrievedSkill:
    """A skill retrieved from the database."""
    id: str
    name: str
    content: str
    tags: List[str]
    importance_level: int
    relevance_score: float


@dataclass
class InjectionResult:
    """Result of context injection."""
    content: str
    skill_ids: List[str]
    token_count: int
    drift_score: float
    synthesis_method: str  # 'template' | 'llm' | 'skipped'


@dataclass
class SubconsciousState:
    """State tracked across turns for drift detection."""
    injected_topics: List[str] = field(default_factory=list)
    last_injection_turn: int = 0
    injection_count: int = 0


@dataclass
class SubconsciousEvent:
    """Event emitted for UI progress indicator."""
    type: Literal["subconscious_thinking", "instinct_injection"]
    stage: Optional[Literal["planning", "retrieving", "synthesizing", "done"]] = None
    data: Optional[Dict[str, Any]] = None


# Confirmation patterns - short messages that mean "yes, proceed"
CONFIRMATION_PATTERNS = {
    "ok", "okay", "yes", "do it", "go", "go ahead", "proceed", "continue",
    "sure", "yep", "yeah", "yea", "yup", "sounds good", "let's do it",
    "lets do it", "let's go", "lets go", "perfect", "great", "good",
    "nice", "cool", "alright", "right", "fine", "that works", "looks good",
    "looks great", "do that", "make it so", "approved", "confirmed",
    "affirmative", "agreed", "deal", "k", "kk"
}


def is_confirmation_message(message: str) -> bool:
    """
    Check if a message is a short confirmation.
    These skip the planner (saves 6+ seconds).
    """
    trimmed = message.strip().lower().rstrip("!.,")
    if len(trimmed) > 50:
        return False
    return trimmed in CONFIRMATION_PATTERNS
