"""
Outcome Tracking for Skill Effectiveness

Tracks which skills were injected and whether they helped the agent succeed.
This enables learning from experience - skills that consistently help get
higher confidence scores, while skills that don't help get lower scores.
"""

import os
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

logger = structlog.getLogger(__name__)


async def record_skill_injection(
    skill_ids: list[str],
    thread_id: str,
    user_intent: str,
    synthesis_method: str,
) -> str | None:
    """
    Record that skills were injected for a conversation turn.
    Returns an injection_id for later outcome tracking.
    """
    supabase_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    if not supabase_url or not skill_ids:
        return None

    try:
        url = f"{supabase_url}/rest/v1/skill_injections"
        headers = {
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        payload = {
            "skill_ids": skill_ids,
            "thread_id": thread_id,
            "user_intent": user_intent,
            "synthesis_method": synthesis_method,
            "injected_at": datetime.now(timezone.utc).isoformat(),
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url, json=payload, headers=headers, timeout=10.0
            )

            if response.status_code in (200, 201):
                data = response.json()
                if isinstance(data, list) and data:
                    return data[0].get("id")
            else:
                logger.warning(
                    "Failed to record skill injection",
                    status_code=response.status_code,
                )
    except Exception as e:
        logger.error("Error recording skill injection", error=str(e))

    return None


async def record_outcome(
    injection_id: str,
    outcome: str,  # 'success', 'failure', 'partial', 'unknown'
    context: str | None = None,
    feedback: str | None = None,
) -> bool:
    """
    Record the outcome of a skill injection.

    Args:
        injection_id: ID from record_skill_injection
        outcome: 'success', 'failure', 'partial', or 'unknown'
        context: What the agent was trying to do
        feedback: Any user feedback

    Returns:
        True if recorded successfully
    """
    supabase_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    if not supabase_url or not injection_id:
        return False

    try:
        url = f"{supabase_url}/rest/v1/skill_injections"
        headers = {
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
            "Content-Type": "application/json",
        }
        payload = {
            "outcome": outcome,
            "outcome_context": context,
            "user_feedback": feedback,
            "outcome_recorded_at": datetime.now(timezone.utc).isoformat(),
        }

        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{url}?id=eq.{injection_id}",
                json=payload,
                headers=headers,
                timeout=10.0,
            )

            if response.status_code in (200, 204):
                # Also update skill confidence scores based on outcome
                await _update_skill_confidence(injection_id, outcome)
                return True
            else:
                logger.warning(
                    "Failed to record outcome",
                    status_code=response.status_code,
                    injection_id=injection_id,
                )
    except Exception as e:
        logger.error("Error recording outcome", error=str(e))

    return False


async def _update_skill_confidence(injection_id: str, outcome: str) -> None:
    """
    Update skill confidence scores based on outcome.

    - Success: +0.02 confidence (max 1.0)
    - Failure: -0.03 confidence (min 0.1)
    - Partial: +0.005 confidence
    """
    supabase_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    if not supabase_url:
        return

    try:
        # First, get the skill IDs from the injection record
        url = f"{supabase_url}/rest/v1/skill_injections"
        headers = {
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{url}?id=eq.{injection_id}&select=skill_ids",
                headers=headers,
                timeout=10.0,
            )

            if response.status_code != 200:
                return

            data = response.json()
            if not data:
                return

            skill_ids = data[0].get("skill_ids", [])
            if not skill_ids:
                return

            # Calculate confidence delta
            if outcome == "success":
                delta = 0.02
            elif outcome == "failure":
                delta = -0.03
            elif outcome == "partial":
                delta = 0.005
            else:
                return  # Unknown outcome, don't update

            # Update each skill's confidence and track times_applied/succeeded/failed
            for skill_id in skill_ids:
                # Get current confidence
                skill_url = f"{supabase_url}/rest/v1/skills?id=eq.{skill_id}&select=confidence_score,times_applied,times_succeeded,times_failed"
                skill_response = await client.get(
                    skill_url, headers=headers, timeout=10.0
                )

                if skill_response.status_code != 200:
                    continue

                skill_data = skill_response.json()
                if not skill_data:
                    continue

                current = skill_data[0]
                current_confidence = current.get("confidence_score") or 0.5
                times_applied = (current.get("times_applied") or 0) + 1
                times_succeeded = current.get("times_succeeded") or 0
                times_failed = current.get("times_failed") or 0

                if outcome == "success":
                    times_succeeded += 1
                elif outcome == "failure":
                    times_failed += 1

                # Calculate new confidence, bounded [0.1, 1.0]
                new_confidence = max(0.1, min(1.0, current_confidence + delta))

                # Update skill
                update_url = f"{supabase_url}/rest/v1/skills?id=eq.{skill_id}"
                await client.patch(
                    update_url,
                    json={
                        "confidence_score": new_confidence,
                        "times_applied": times_applied,
                        "times_succeeded": times_succeeded,
                        "times_failed": times_failed,
                    },
                    headers=headers,
                    timeout=10.0,
                )

                logger.debug(
                    "Updated skill confidence",
                    skill_id=skill_id,
                    outcome=outcome,
                    old_confidence=current_confidence,
                    new_confidence=new_confidence,
                )

    except Exception as e:
        logger.error("Error updating skill confidence", error=str(e))


async def get_skill_effectiveness_stats(skill_id: str) -> dict[str, Any] | None:
    """
    Get effectiveness statistics for a skill.
    """
    supabase_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    if not supabase_url:
        return None

    try:
        url = f"{supabase_url}/rest/v1/skills?id=eq.{skill_id}&select=name,confidence_score,times_applied,times_succeeded,times_failed"
        headers = {
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=10.0)

            if response.status_code == 200:
                data = response.json()
                if data:
                    skill = data[0]
                    times_applied = skill.get("times_applied") or 0
                    times_succeeded = skill.get("times_succeeded") or 0
                    times_failed = skill.get("times_failed") or 0

                    success_rate = (
                        times_succeeded / times_applied if times_applied > 0 else 0
                    )

                    return {
                        "skill_id": skill_id,
                        "name": skill.get("name"),
                        "confidence_score": skill.get("confidence_score"),
                        "times_applied": times_applied,
                        "times_succeeded": times_succeeded,
                        "times_failed": times_failed,
                        "success_rate": success_rate,
                    }
    except Exception as e:
        logger.error("Error getting skill stats", error=str(e), skill_id=skill_id)

    return None
