"""
Skill Retriever for Subconscious Layer

Retrieves relevant skills from the database using:
- GIN index for exact tag matches
- pgvector for semantic similarity
"""

import asyncio
import os

import httpx
import structlog
from openai import AsyncOpenAI

from .types import RetrievedSkill

logger = structlog.getLogger(__name__)

# OpenAI client for embeddings
_openai_client: AsyncOpenAI | None = None


def get_openai_client() -> AsyncOpenAI:
    """Get or create OpenAI client."""
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    return _openai_client


async def generate_embedding(text: str) -> list[float]:
    """Generate embedding for a query string."""
    client = get_openai_client()
    response = await client.embeddings.create(
        model="text-embedding-3-small",
        input=text,
    )
    return response.data[0].embedding


def format_skill_content(skill: dict) -> str:
    """Format a skill record into readable content."""
    parts = []

    if skill.get("name"):
        parts.append(f"**{skill['name']}**")

    if skill.get("description"):
        parts.append(skill["description"])

    if skill.get("trigger_condition"):
        parts.append(f"When: {skill['trigger_condition']}")

    if skill.get("action"):
        parts.append(f"Do: {skill['action']}")

    if skill.get("reasoning"):
        parts.append(f"Why: {skill['reasoning']}")

    return "\n".join(parts)


async def retrieve_skills_by_keywords(
    keywords: list[str],
    access_token: str,  # Kept for API compatibility but not used - we use service key
    limit: int = 7,
) -> list[RetrievedSkill]:
    """
    Retrieve skills by keyword/tag matching.
    Uses GIN index on tags column for fast lookup.
    Uses service key for auth since skills are shared (not user-specific).
    """
    supabase_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    if not supabase_url or not keywords:
        return []

    # Build query for tag matching
    # tags column is text[] so we use the overlap operator &&
    tags_filter = ",".join(f'"{kw}"' for kw in keywords)

    url = f"{supabase_url}/rest/v1/skills"
    params = {
        "select": "id,name,description,trigger_condition,action,reasoning,tags,importance_level",
        "is_active": "eq.true",
        "limit": str(limit),
    }
    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient() as client:
            # Try tag overlap first
            response = await client.get(
                url,
                params={**params, "tags": f"ov.{{{tags_filter}}}"},
                headers=headers,
                timeout=10.0,
            )

            if response.status_code == 200:
                data = response.json()
                return [
                    RetrievedSkill(
                        id=skill["id"],
                        name=skill["name"],
                        content=format_skill_content(skill),
                        tags=skill.get("tags", []),
                        importance_level=skill.get("importance_level", 1),
                        relevance_score=0.8,  # Tag match = high relevance
                    )
                    for skill in data
                ]
            else:
                logger.warning(
                    "Keyword retrieval failed",
                    status_code=response.status_code,
                    keywords=keywords,
                )
    except Exception as e:
        logger.error(
            "Error retrieving skills by keywords",
            keywords=keywords,
            error=str(e),
            exc_info=True,
        )

    return []


async def retrieve_skills_by_embedding(
    query: str,
    access_token: str,  # Kept for API compatibility but not used - we use service key
    limit: int = 5,
    min_similarity: float = 0.3,
) -> list[RetrievedSkill]:
    """
    Retrieve skills by semantic similarity using pgvector.
    Uses service key for auth since skills are shared (not user-specific).
    """
    supabase_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    if not supabase_url or not query:
        return []

    try:
        # Generate embedding for the query
        embedding = await generate_embedding(query)

        # Call the match_skills RPC function
        url = f"{supabase_url}/rest/v1/rpc/match_skills"
        headers = {
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
            "Content-Type": "application/json",
        }
        payload = {
            "query_embedding": embedding,
            "match_threshold": min_similarity,
            "match_count": limit,
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                json=payload,
                headers=headers,
                timeout=15.0,
            )

            if response.status_code == 200:
                data = response.json()
                return [
                    RetrievedSkill(
                        id=skill["id"],
                        name=skill["name"],
                        content=format_skill_content(skill),
                        tags=skill.get("tags", []),
                        importance_level=skill.get("importance_level", 1),
                        relevance_score=skill.get("similarity", 0.5),
                    )
                    for skill in data
                ]
            else:
                logger.warning(
                    "Embedding search failed",
                    status_code=response.status_code,
                    query=query[:100],
                )
    except Exception as e:
        logger.error(
            "Error retrieving skills by embedding",
            query=query[:100],
            error=str(e),
            exc_info=True,
        )

    return []


async def retrieve_skills_by_embeddings_batch(
    queries: list[str],
    access_token: str,
    limit_per_query: int = 3,
    min_similarity: float = 0.3,
) -> list[RetrievedSkill]:
    """
    Retrieve skills by semantic similarity for multiple queries in parallel.
    """
    if not queries:
        return []

    tasks = [
        retrieve_skills_by_embedding(q, access_token, limit_per_query, min_similarity)
        for q in queries[:3]  # Max 3 queries
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    skills = []
    for result in results:
        if isinstance(result, list):
            skills.extend(result)
        elif isinstance(result, Exception):
            logger.warning("Batch embedding query failed", error=str(result))

    return skills


async def retrieve_always_skills(
    access_token: str,  # Kept for API compatibility but not used - we use service key
) -> list[RetrievedSkill]:
    """
    Retrieve Level 3 (always-inject) skills.
    These are critical instincts that should always be considered.
    Uses service key for auth since skills are shared (not user-specific).
    """
    supabase_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    if not supabase_url:
        return []

    url = f"{supabase_url}/rest/v1/skills"
    params = {
        "select": "id,name,description,trigger_condition,action,reasoning,tags,importance_level",
        "is_active": "eq.true",
        "importance_level": "eq.3",
        "limit": "10",
    }
    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url, params=params, headers=headers, timeout=10.0
            )

            if response.status_code == 200:
                data = response.json()
                return [
                    RetrievedSkill(
                        id=skill["id"],
                        name=skill["name"],
                        content=format_skill_content(skill),
                        tags=skill.get("tags", []),
                        importance_level=3,
                        relevance_score=1.0,  # Always skills get max relevance
                    )
                    for skill in data
                ]
    except Exception as e:
        logger.error(
            "Error retrieving always skills",
            error=str(e),
            exc_info=True,
        )

    return []


async def retrieve_all_skills_parallel(
    keywords: list[str],
    semantic_queries: list[str],
    access_token: str,
) -> list[RetrievedSkill]:
    """
    Retrieve all skills in parallel: always-inject + keywords + semantic.
    This is faster than sequential retrieval.
    """
    # Run all retrievals in parallel
    always_task = retrieve_always_skills(access_token)
    keyword_task = retrieve_skills_by_keywords(keywords, access_token, limit=5) if keywords else asyncio.sleep(0)
    semantic_task = retrieve_skills_by_embeddings_batch(semantic_queries, access_token, limit_per_query=3)

    results = await asyncio.gather(
        always_task,
        keyword_task,
        semantic_task,
        return_exceptions=True,
    )

    skills = []
    for i, result in enumerate(results):
        if isinstance(result, list):
            skills.extend(result)
        elif isinstance(result, Exception):
            source = ["always", "keyword", "semantic"][i]
            logger.warning(f"Parallel retrieval failed for {source}", error=str(result))
        # asyncio.sleep returns None, skip it
        elif result is None:
            pass

    return skills
