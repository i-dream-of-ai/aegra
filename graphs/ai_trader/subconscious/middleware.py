"""
Subconscious Middleware for LangGraph

Intercepts model calls to provide dynamic context injection based on
conversation analysis. Emits SSE events for UI progress indicator.

TWO-AGENT ARCHITECTURE:
1. PLANNER - Analyzes conversation, generates retrieval queries
2. SYNTHESIZER - Integrates skills into actionable context

Key features:
- Fast path: Confirmation messages skip planner (saves 6+ seconds)
- Template path: ≤2 high-relevance skills use template (no LLM)
- Fail open: Errors don't block the main agent
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

import structlog

from .deduplication import deduplicate_by_content
from .outcome_tracker import record_skill_injection
from .planner import generate_retrieval_plan
from .retriever import retrieve_all_skills_parallel
from .synthesizer import synthesize_context
from .types import (
    SubconsciousEvent,
    SubconsciousState,
    is_confirmation_message,
)

logger = structlog.getLogger(__name__)

# Minimum turns between injections to avoid spamming
MIN_TURNS_BETWEEN_INJECTION = 2


@dataclass
class SubconsciousMiddleware:
    """
    Middleware that provides dynamic context injection.

    Usage in graph:
        middleware = SubconsciousMiddleware(on_event=emit_sse)
        context = await middleware.process(messages, access_token)
        if context:
            messages = [SystemMessage(content=system_prompt + context)] + messages[1:]
    """

    on_event: Callable[[SubconsciousEvent], None] | None = None
    state: SubconsciousState = None

    def __post_init__(self):
        if self.state is None:
            self.state = SubconsciousState()

    def emit(self, event: SubconsciousEvent):
        """Emit event for UI progress indicator."""
        if self.on_event:
            try:
                self.on_event(event)
            except Exception as e:
                logger.warning("Error emitting subconscious event", error=str(e))

    async def process(
        self,
        messages: list[BaseMessage],
        access_token: str,
        current_turn: int = 0,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> str | None:
        """
        Process messages and return context to inject.

        Args:
            messages: Conversation messages
            access_token: Supabase access token for DB queries
            current_turn: Current conversation turn number
            thread_id: Thread ID for outcome tracking

        Returns:
            Context string to inject, or None if no injection needed
        """
        # Check if we should inject (rate limiting)
        if not self._should_inject(current_turn):
            return None

        # Extract the last human message
        last_human = self._get_last_human_message(messages)
        if not last_human:
            return None

        # Fast path: Confirmation messages skip planning
        if is_confirmation_message(last_human):
            logger.debug("Confirmation message detected, skipping subconscious")
            return None

        try:
            # PHASE 1: Planning
            self.emit(SubconsciousEvent(type="subconscious_thinking", stage="planning"))

            # Extract conversation context
            conversation_context = self._extract_context(messages)

            # Generate retrieval plan
            plan = await generate_retrieval_plan(
                messages=self._messages_to_dicts(messages),
                recent_context=conversation_context,
                user_id=user_id,
            )

            if plan.skip_reason:
                self.emit(SubconsciousEvent(type="subconscious_thinking", stage="done"))
                return None

            # PHASE 2: Retrieval (parallel for performance)
            self.emit(
                SubconsciousEvent(type="subconscious_thinking", stage="retrieving")
            )

            # Retrieve all skills in parallel (always + keyword + semantic)
            skills = await retrieve_all_skills_parallel(
                keywords=plan.keyword_queries or [],
                semantic_queries=plan.semantic_queries or [],
                access_token=access_token,
            )

            # Deduplicate by content (catches semantically similar skills)
            unique_skills = deduplicate_by_content(skills)

            if not unique_skills:
                self.emit(SubconsciousEvent(type="subconscious_thinking", stage="done"))
                return None

            # PHASE 3: Synthesis
            self.emit(
                SubconsciousEvent(type="subconscious_thinking", stage="synthesizing")
            )

            result = await synthesize_context(
                skills=unique_skills,
                user_intent=plan.user_intent,
                conversation_context=conversation_context,
                use_llm=len(unique_skills) > 2,
                user_id=user_id,
            )

            # Update state
            self.state.last_injection_turn = current_turn
            self.state.injection_count += 1

            # Record injection for outcome tracking (non-blocking)
            injection_id = None
            if thread_id and result.skill_ids:
                try:
                    injection_id = await record_skill_injection(
                        skill_ids=result.skill_ids,
                        thread_id=thread_id,
                        user_intent=plan.user_intent,
                        synthesis_method=result.synthesis_method,
                    )
                except Exception as e:
                    logger.warning("Failed to record skill injection", error=str(e))

            # Build skill info for UI
            skill_info = [
                {"id": s.id, "name": s.name, "tags": s.tags}
                for s in unique_skills
                if s.id in result.skill_ids
            ]

            # Emit injection event with skill details for UI
            self.emit(
                SubconsciousEvent(
                    type="instinct_injection",
                    data={
                        "skillIds": result.skill_ids,
                        "skills": skill_info,
                        "userIntent": plan.user_intent,
                        "content": result.content[:500] if result.content else None,
                        "tokenCount": result.token_count,
                        "driftScore": result.drift_score,
                        "synthesisMethod": result.synthesis_method,
                        "injectionId": injection_id,  # For later outcome recording
                    },
                )
            )

            self.emit(SubconsciousEvent(type="subconscious_thinking", stage="done"))

            return result.content if result.content else None

        except Exception as e:
            logger.error("Error in subconscious processing", error=str(e), exc_info=True)
            self.emit(SubconsciousEvent(type="subconscious_thinking", stage="done"))
            return None

    def _should_inject(self, current_turn: int) -> bool:
        """Check if we should inject based on rate limiting."""
        turns_since_last = current_turn - self.state.last_injection_turn
        return turns_since_last >= MIN_TURNS_BETWEEN_INJECTION

    def _get_last_human_message(self, messages: list[BaseMessage]) -> str | None:
        """Extract the last human message content."""
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                content = msg.content
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    text_parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    return " ".join(text_parts)
        return None

    def _extract_context(
        self, messages: list[BaseMessage], max_chars: int = 2000
    ) -> str:
        """Extract conversation context for planning."""
        parts = []
        total_chars = 0

        for msg in reversed(messages[-10:]):  # Last 10 messages
            role = "user" if isinstance(msg, HumanMessage) else "assistant"
            content = msg.content
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = " ".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            else:
                continue

            if text:
                if total_chars + len(text) > max_chars:
                    text = text[: max_chars - total_chars]
                parts.insert(0, f"{role}: {text}")
                total_chars += len(text)
                if total_chars >= max_chars:
                    break

        return "\n".join(parts)

    def _messages_to_dicts(self, messages: list[BaseMessage]) -> list[dict[str, Any]]:
        """Convert messages to dicts for planner."""
        result = []
        for msg in messages:
            if isinstance(msg, HumanMessage):
                role = "user"
            elif isinstance(msg, AIMessage):
                role = "assistant"
            elif isinstance(msg, SystemMessage):
                role = "system"
            else:
                role = "unknown"

            result.append(
                {
                    "role": role,
                    "content": msg.content,
                }
            )
        return result


def create_subconscious_middleware(
    on_event: Callable[[SubconsciousEvent], None] | None = None,
) -> SubconsciousMiddleware:
    """
    Create a subconscious middleware instance.

    Args:
        on_event: Callback for SSE events (subconscious_thinking, instinct_injection)

    Returns:
        Configured middleware instance
    """
    return SubconsciousMiddleware(on_event=on_event)
