"""
Skill Merger

Automatically merges duplicate or highly similar skills into one.
This keeps the knowledge base clean and prevents redundant injections.
"""

import os
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from .deduplication import find_duplicate_skills_in_db

logger = structlog.getLogger(__name__)

MERGE_PROMPT = """You are a skill consolidation expert. Your task is to merge two similar skills into one improved skill.

Guidelines:
1. Combine the best aspects of both skills
2. Keep the description concise but comprehensive
3. Preserve all important trigger conditions
4. Maintain clear, actionable guidance
5. Use the most specific and helpful reasoning

Output a JSON object with these fields:
{
  "name": "Merged skill name",
  "description": "Consolidated description",
  "trigger_condition": "When to apply this skill",
  "action": "What to do",
  "reasoning": "Why this approach works",
  "tags": ["tag1", "tag2"],
  "importance_level": 1-3
}"""


async def merge_skills(
    skill_a_id: str,
    skill_b_id: str,
    keep_primary: str | None = None,
) -> dict[str, Any] | None:
    """
    Merge two skills into one.

    Args:
        skill_a_id: First skill ID
        skill_b_id: Second skill ID
        keep_primary: Which skill ID to keep (other gets deactivated).
                     If None, creates a new merged skill and deactivates both.

    Returns:
        The merged skill data, or None if merge failed
    """
    supabase_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    if not supabase_url:
        return None

    try:
        headers = {
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            # Fetch both skills
            skill_a = await _fetch_skill(client, supabase_url, skill_a_id, headers)
            skill_b = await _fetch_skill(client, supabase_url, skill_b_id, headers)

            if not skill_a or not skill_b:
                logger.warning("Could not fetch skills for merge", a=skill_a_id, b=skill_b_id)
                return None

            # Use LLM to generate merged skill
            merged = await _llm_merge_skills(skill_a, skill_b)

            if not merged:
                logger.warning("LLM merge failed")
                return None

            if keep_primary:
                # Update the primary skill with merged content
                primary_id = keep_primary
                secondary_id = skill_b_id if keep_primary == skill_a_id else skill_a_id

                # Update primary skill
                update_url = f"{supabase_url}/rest/v1/skills?id=eq.{primary_id}"
                merged["updated_at"] = datetime.now(timezone.utc).isoformat()
                merged["evolution_count"] = (skill_a.get("evolution_count") or 0) + 1
                merged["last_evolved_at"] = datetime.now(timezone.utc).isoformat()
                merged["last_evolution_type"] = "merge"

                response = await client.patch(
                    update_url, json=merged, headers=headers, timeout=10.0
                )

                if response.status_code not in (200, 204):
                    logger.error("Failed to update primary skill", status=response.status_code)
                    return None

                # Deactivate secondary skill
                deactivate_url = f"{supabase_url}/rest/v1/skills?id=eq.{secondary_id}"
                await client.patch(
                    deactivate_url,
                    json={
                        "is_active": False,
                        "merged_into": primary_id,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    headers=headers,
                    timeout=10.0,
                )

                # Log the merge in evolution log
                await _log_merge_evolution(
                    client, supabase_url, headers,
                    primary_id, skill_a, skill_b
                )

                merged["id"] = primary_id
                logger.info(
                    "Skills merged successfully",
                    primary_id=primary_id,
                    secondary_id=secondary_id,
                )
                return merged

            else:
                # Create new merged skill, deactivate both originals
                create_url = f"{supabase_url}/rest/v1/skills"
                merged["created_at"] = datetime.now(timezone.utc).isoformat()
                merged["is_active"] = True
                merged["merged_from"] = [skill_a_id, skill_b_id]

                # Add Prefer header for returning the created record
                create_headers = {**headers, "Prefer": "return=representation"}

                response = await client.post(
                    create_url, json=merged, headers=create_headers, timeout=10.0
                )

                if response.status_code not in (200, 201):
                    logger.error("Failed to create merged skill", status=response.status_code)
                    return None

                created = response.json()
                new_id = created[0]["id"] if isinstance(created, list) else created.get("id")

                # Deactivate both original skills
                for old_id in [skill_a_id, skill_b_id]:
                    deactivate_url = f"{supabase_url}/rest/v1/skills?id=eq.{old_id}"
                    await client.patch(
                        deactivate_url,
                        json={
                            "is_active": False,
                            "merged_into": new_id,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                        headers=headers,
                        timeout=10.0,
                    )

                logger.info(
                    "New merged skill created",
                    new_id=new_id,
                    merged_from=[skill_a_id, skill_b_id],
                )

                merged["id"] = new_id
                return merged

    except Exception as e:
        logger.error("Error merging skills", error=str(e), exc_info=True)
        return None


async def _fetch_skill(
    client: httpx.AsyncClient,
    supabase_url: str,
    skill_id: str,
    headers: dict,
) -> dict[str, Any] | None:
    """Fetch a skill from the database."""
    url = f"{supabase_url}/rest/v1/skills?id=eq.{skill_id}"
    response = await client.get(url, headers=headers, timeout=10.0)

    if response.status_code == 200:
        data = response.json()
        return data[0] if data else None
    return None


async def _llm_merge_skills(
    skill_a: dict[str, Any],
    skill_b: dict[str, Any],
) -> dict[str, Any] | None:
    """Use LLM to intelligently merge two skills."""
    try:
        model = ChatAnthropic(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            temperature=0.2,
        )

        skill_a_text = f"""SKILL A:
Name: {skill_a.get('name', 'Unknown')}
Description: {skill_a.get('description', '')}
Trigger: {skill_a.get('trigger_condition', '')}
Action: {skill_a.get('action', '')}
Reasoning: {skill_a.get('reasoning', '')}
Tags: {skill_a.get('tags', [])}
Importance: {skill_a.get('importance_level', 1)}"""

        skill_b_text = f"""SKILL B:
Name: {skill_b.get('name', 'Unknown')}
Description: {skill_b.get('description', '')}
Trigger: {skill_b.get('trigger_condition', '')}
Action: {skill_b.get('action', '')}
Reasoning: {skill_b.get('reasoning', '')}
Tags: {skill_b.get('tags', [])}
Importance: {skill_b.get('importance_level', 1)}"""

        response = await model.ainvoke(
            [
                SystemMessage(content=MERGE_PROMPT),
                HumanMessage(content=f"{skill_a_text}\n\n{skill_b_text}\n\nMerge these skills into one. Output JSON."),
            ],
            config={"callbacks": []},
        )

        content = response.content
        if isinstance(content, list):
            content = content[0].get("text", "") if content else ""

        # Extract JSON
        import json
        json_start = content.find("{")
        json_end = content.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            json_str = content[json_start:json_end]
            return json.loads(json_str)

    except Exception as e:
        logger.error("LLM merge failed", error=str(e))

    return None


async def _log_merge_evolution(
    client: httpx.AsyncClient,
    supabase_url: str,
    headers: dict,
    primary_id: str,
    skill_a: dict,
    skill_b: dict,
) -> None:
    """Log the merge in the skill evolution log."""
    try:
        url = f"{supabase_url}/rest/v1/skill_evolution_log"
        payload = {
            "skill_id": primary_id,
            "evolution_type": "merge",
            "changelog": f"Merged with skill '{skill_b.get('name', 'Unknown')}'",
            "learning_note": "Skills consolidated to reduce redundancy",
            "trigger_outcome": "automation",
            "trigger_context": "Duplicate detection",
        }
        await client.post(url, json=payload, headers=headers, timeout=10.0)
    except Exception as e:
        logger.warning("Failed to log merge evolution", error=str(e))


async def auto_merge_duplicates(
    similarity_threshold: float = 0.92,
    max_merges: int = 5,
    dry_run: bool = True,
) -> list[dict[str, Any]]:
    """
    Automatically find and merge duplicate skills.

    Args:
        similarity_threshold: Minimum similarity to consider duplicates
        max_merges: Maximum number of merges to perform
        dry_run: If True, only reports what would be merged without actually merging

    Returns:
        List of merge results or proposed merges
    """
    # Find duplicates
    duplicates = await find_duplicate_skills_in_db(
        similarity_threshold=similarity_threshold,
        limit=100,
    )

    if not duplicates:
        logger.info("No duplicates found")
        return []

    results = []

    for dup in duplicates[:max_merges]:
        if dry_run:
            results.append({
                "action": "would_merge",
                "skill_a": dup["skill_a_name"],
                "skill_b": dup["skill_b_name"],
                "similarity": dup["similarity"],
            })
            logger.info(
                "Would merge",
                skill_a=dup["skill_a_name"],
                skill_b=dup["skill_b_name"],
                similarity=dup["similarity"],
            )
        else:
            # Actually merge - keep the first one as primary
            merged = await merge_skills(
                dup["skill_a_id"],
                dup["skill_b_id"],
                keep_primary=dup["skill_a_id"],
            )

            if merged:
                results.append({
                    "action": "merged",
                    "primary_id": dup["skill_a_id"],
                    "merged_skill": merged,
                    "similarity": dup["similarity"],
                })
            else:
                results.append({
                    "action": "merge_failed",
                    "skill_a": dup["skill_a_id"],
                    "skill_b": dup["skill_b_id"],
                })

    return results
