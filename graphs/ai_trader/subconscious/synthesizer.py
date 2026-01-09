"""
Synthesizer Agent for Subconscious Layer

Filters and synthesizes retrieved skills into actionable context
for the main agent.
"""

import json
import re

import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from .types import InjectionResult, RetrievedSkill

logger = structlog.getLogger(__name__)

# Synthesizer system prompt
SYNTHESIZER_PROMPT = """You are a context synthesizer that prepares actionable guidance for a trading algorithm developer.

Your task:
1. Review the retrieved skills/knowledge
2. Filter to the most relevant ones for the current conversation
3. Synthesize into concise, actionable context

Output JSON with this structure:
{
  "selected_skill_ids": ["id1", "id2"],
  "synthesized_context": "Concise guidance (max 150 words) that the main agent should follow",
  "reasoning": "Brief explanation of why these skills are relevant"
}

Guidelines:
- Focus on ACTIONABLE guidance, not descriptions
- Be specific to the user's current task
- Prioritize high-importance skills
- Keep synthesis under 150 words
- Use direct, imperative language ("Do X", "Avoid Y")"""


async def synthesize_context(
    skills: list[RetrievedSkill],
    user_intent: str,
    conversation_context: str,
    use_llm: bool = True,
) -> InjectionResult:
    """
    Synthesize retrieved skills into actionable context.

    Args:
        skills: Retrieved skills from database
        user_intent: What the user is trying to accomplish
        conversation_context: Recent conversation summary
        use_llm: Whether to use LLM (False = template-based)

    Returns:
        InjectionResult with synthesized context
    """
    if not skills:
        return InjectionResult(
            content="",
            skill_ids=[],
            token_count=0,
            drift_score=0.0,
            synthesis_method="skipped",
        )

    # For 1-2 skills with high relevance, use template (no LLM needed)
    if (
        len(skills) <= 2
        and all(s.relevance_score > 0.7 for s in skills)
        and not use_llm
    ):
        return _template_synthesis(skills)

    # Use LLM for more complex synthesis
    try:
        return await _llm_synthesis(skills, user_intent, conversation_context)
    except Exception as e:
        logger.warning("LLM synthesis failed, using template fallback", error=str(e))
        return _template_synthesis(skills)


def _template_synthesis(skills: list[RetrievedSkill]) -> InjectionResult:
    """Template-based synthesis for simple cases (no LLM call)."""
    # Sort by relevance
    sorted_skills = sorted(skills, key=lambda s: s.relevance_score, reverse=True)

    # Build context from top skills
    context_parts = ["<active_skills>"]
    for skill in sorted_skills[:3]:
        context_parts.append(f"\n### {skill.name}\n{skill.content}")
    context_parts.append("\n</active_skills>")

    content = "\n".join(context_parts)

    return InjectionResult(
        content=content,
        skill_ids=[s.id for s in sorted_skills[:3]],
        token_count=estimate_token_count(content),
        drift_score=0.0,
        synthesis_method="template",
    )


async def _llm_synthesis(
    skills: list[RetrievedSkill],
    user_intent: str,
    conversation_context: str,
) -> InjectionResult:
    """LLM-based synthesis for complex cases."""
    model = ChatAnthropic(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        temperature=0.2,
    )

    # Format skills for LLM
    skills_text = "\n\n".join(
        [
            f"SKILL {i + 1} (id: {s.id}, relevance: {s.relevance_score:.2f}):\n{s.content}"
            for i, s in enumerate(skills[:7])  # Max 7 skills
        ]
    )

    try:
        # Disable callbacks to prevent LangGraph from capturing this nested LLM call
        response = await model.ainvoke(
            [
                SystemMessage(content=SYNTHESIZER_PROMPT),
                HumanMessage(
                    content=f"""USER INTENT: {user_intent}

CONVERSATION CONTEXT:
{conversation_context[:500]}

RETRIEVED SKILLS:
{skills_text}

Synthesize the most relevant skills into actionable context. Output JSON."""
                ),
            ],
            config={"callbacks": []},
        )

        # Parse JSON response
        content = response.content
        if isinstance(content, list):
            content = content[0].get("text", "") if content else ""

        # Extract JSON
        json_start = content.find("{")
        json_end = content.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            json_str = content[json_start:json_end]
            data = json.loads(json_str)

            synthesized = data.get("synthesized_context", "")
            selected_ids = data.get("selected_skill_ids", [])

            # Wrap in tags for easy parsing
            final_content = f"<active_skills>\n{synthesized}\n</active_skills>"

            return InjectionResult(
                content=final_content,
                skill_ids=selected_ids,
                token_count=estimate_token_count(final_content),
                drift_score=0.0,
                synthesis_method="llm",
            )

    except Exception as e:
        logger.error("Error in LLM synthesis", error=str(e), exc_info=True)

    # Fallback to template
    return _template_synthesis(skills)


def estimate_token_count(text: str) -> int:
    """
    Improved token estimation for Claude models.

    Based on analysis:
    - English prose: ~4 chars/token
    - Code/markdown: ~3.2 chars/token
    - JSON/structured: ~3 chars/token
    - Whitespace-heavy: slightly more tokens

    We use a weighted estimate based on content characteristics.
    """
    if not text:
        return 0

    # Count code indicators (markdown code blocks, brackets, etc.)
    code_block_count = len(re.findall(r'```', text))
    bracket_density = (text.count('{') + text.count('[') + text.count('(')) / max(len(text), 1)

    # Determine content type ratio
    is_code_heavy = code_block_count > 0 or bracket_density > 0.02

    if is_code_heavy:
        # Code/structured content: ~3.2 chars per token
        base_estimate = len(text) / 3.2
    else:
        # Prose content: ~4 chars per token
        base_estimate = len(text) / 4.0

    # Add overhead for special tokens and formatting
    overhead = len(text.split('\n')) * 0.5  # Newlines often become tokens

    return int(base_estimate + overhead)
