"""
Skill Deduplication

Detects and merges semantically similar skills to avoid redundant injections.
Uses embedding similarity to identify skills that say the same thing differently.
"""

import os
from typing import Any

import httpx
import structlog

from .retriever import generate_embedding
from .types import RetrievedSkill

logger = structlog.getLogger(__name__)

# Similarity threshold for considering skills as duplicates
SIMILARITY_THRESHOLD = 0.92


def deduplicate_by_id(skills: list[RetrievedSkill]) -> list[RetrievedSkill]:
    """
    Basic deduplication by skill ID.
    """
    seen_ids = set()
    unique = []
    for skill in skills:
        if skill.id not in seen_ids:
            seen_ids.add(skill.id)
            unique.append(skill)
    return unique


def deduplicate_by_content(skills: list[RetrievedSkill]) -> list[RetrievedSkill]:
    """
    Deduplicate skills by content similarity.
    Groups skills with similar content and keeps the highest relevance one.

    This is a fast, non-embedding approach using text overlap.
    """
    if len(skills) <= 1:
        return skills

    # Sort by relevance (highest first)
    sorted_skills = sorted(skills, key=lambda s: s.relevance_score, reverse=True)

    unique = []
    seen_content_hashes = set()

    for skill in sorted_skills:
        # Create a simple content fingerprint
        content_words = set(skill.content.lower().split())
        # Remove common words
        content_words -= {"the", "a", "an", "is", "are", "to", "for", "in", "on", "of", "and", "or", "when", "if", "do"}

        # Create a hash of the top words
        top_words = sorted(content_words)[:20]
        content_hash = hash(tuple(top_words))

        # Check if we've seen similar content
        is_duplicate = False
        for existing_hash in seen_content_hashes:
            # Simple overlap check
            if content_hash == existing_hash:
                is_duplicate = True
                break

        if not is_duplicate:
            unique.append(skill)
            seen_content_hashes.add(content_hash)

    logger.debug(
        "Content deduplication complete",
        original_count=len(skills),
        unique_count=len(unique),
    )

    return unique


async def deduplicate_by_embedding(
    skills: list[RetrievedSkill],
    similarity_threshold: float = SIMILARITY_THRESHOLD,
) -> list[RetrievedSkill]:
    """
    Advanced deduplication using embedding similarity.

    For each pair of skills, computes cosine similarity of their embeddings.
    If similarity > threshold, keeps the one with higher relevance score.

    Note: This is slower due to embedding generation. Use for important cases.
    """
    if len(skills) <= 1:
        return skills

    try:
        # Generate embeddings for all skills
        embeddings = []
        for skill in skills:
            embedding = await generate_embedding(skill.content[:500])
            embeddings.append(embedding)

        # Compute pairwise similarities and mark duplicates
        duplicate_indices = set()

        for i in range(len(skills)):
            if i in duplicate_indices:
                continue

            for j in range(i + 1, len(skills)):
                if j in duplicate_indices:
                    continue

                # Compute cosine similarity
                similarity = _cosine_similarity(embeddings[i], embeddings[j])

                if similarity > similarity_threshold:
                    # Keep the one with higher relevance
                    if skills[i].relevance_score >= skills[j].relevance_score:
                        duplicate_indices.add(j)
                    else:
                        duplicate_indices.add(i)
                        break  # i is now a duplicate, stop comparing

        # Filter out duplicates
        unique = [s for idx, s in enumerate(skills) if idx not in duplicate_indices]

        logger.info(
            "Embedding deduplication complete",
            original_count=len(skills),
            unique_count=len(unique),
            duplicates_removed=len(duplicate_indices),
        )

        return unique

    except Exception as e:
        logger.warning("Embedding deduplication failed, using ID-based", error=str(e))
        return deduplicate_by_id(skills)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot_product = sum(x * y for x, y in zip(a, b))
    magnitude_a = sum(x * x for x in a) ** 0.5
    magnitude_b = sum(x * x for x in b) ** 0.5

    if magnitude_a == 0 or magnitude_b == 0:
        return 0.0

    return dot_product / (magnitude_a * magnitude_b)


async def find_duplicate_skills_in_db(
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Find duplicate skills in the database using embedding similarity.

    Returns a list of duplicate pairs with their similarity scores.
    Useful for manual review and merging.
    """
    supabase_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    if not supabase_url:
        return []

    try:
        # Get all active skills with embeddings
        url = f"{supabase_url}/rest/v1/skills"
        params = {
            "select": "id,name,description,embedding",
            "is_active": "eq.true",
            "limit": str(limit),
        }
        headers = {
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(
                url, params=params, headers=headers, timeout=30.0
            )

            if response.status_code != 200:
                logger.warning("Failed to fetch skills for duplicate detection")
                return []

            skills = response.json()

            # Filter skills with embeddings
            skills_with_embeddings = [
                s for s in skills if s.get("embedding")
            ]

            duplicates = []

            # Find pairs with high similarity
            for i, skill_a in enumerate(skills_with_embeddings):
                for skill_b in skills_with_embeddings[i + 1:]:
                    similarity = _cosine_similarity(
                        skill_a["embedding"],
                        skill_b["embedding"],
                    )

                    if similarity > similarity_threshold:
                        duplicates.append({
                            "skill_a_id": skill_a["id"],
                            "skill_a_name": skill_a["name"],
                            "skill_b_id": skill_b["id"],
                            "skill_b_name": skill_b["name"],
                            "similarity": similarity,
                        })

            logger.info(
                "Duplicate detection complete",
                total_skills=len(skills_with_embeddings),
                duplicate_pairs=len(duplicates),
            )

            return sorted(duplicates, key=lambda x: x["similarity"], reverse=True)

    except Exception as e:
        logger.error("Error detecting duplicate skills", error=str(e))
        return []
