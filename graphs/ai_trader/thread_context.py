"""Thread context utilities for fetching project info from thread metadata.

Instead of passing project context through the SDK (which doesn't work reliably),
we store project IDs in thread metadata when creating/submitting to threads.
Tools can then fetch this metadata directly from the database.
"""

import json
import os
from functools import lru_cache

import asyncpg
from langchain_core.runnables import RunnableConfig


@lru_cache(maxsize=1)
def get_database_url() -> str:
    """Get database URL from environment.

    Handles SQLAlchemy-style URLs (postgresql+asyncpg://) by stripping the driver suffix.
    """
    url = os.environ.get("DATABASE_URL", "")
    # SQLAlchemy uses postgresql+asyncpg://, but asyncpg needs just postgresql://
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "")
    return url


async def get_thread_metadata(thread_id: str) -> dict | None:
    """Fetch thread metadata from database.

    Args:
        thread_id: The thread ID to fetch metadata for

    Returns:
        Thread metadata dict or None if not found
    """
    db_url = get_database_url()
    if not db_url:
        # Return error info instead of silently returning None
        return {"_error": "DATABASE_URL not set"}

    try:
        conn = await asyncpg.connect(db_url)
        try:
            row = await conn.fetchrow(
                "SELECT metadata_json FROM thread WHERE thread_id = $1",
                thread_id
            )
            if row:
                metadata = row["metadata_json"]
                # Handle case where metadata_json is stored as TEXT instead of JSONB
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except json.JSONDecodeError:
                        return {"_error": f"Invalid JSON in metadata: {metadata[:100]}"}
                return metadata if isinstance(metadata, dict) else {"_error": f"Unexpected metadata type: {type(metadata).__name__}"}
            return {"_error": f"Thread {thread_id} not found in database"}
        finally:
            await conn.close()
    except Exception as e:
        import structlog
        logger = structlog.get_logger()
        logger.warning(f"Failed to fetch thread metadata: {e}")
        # Return error info for debugging
        return {"_error": str(e), "_db_url_prefix": db_url[:30] + "..." if db_url else "empty"}


async def get_qc_project_id_from_thread(config: RunnableConfig) -> int | None:
    """Extract qc_project_id from thread metadata.

    This is the preferred way to get project context in tools.
    Falls back to config.configurable if thread metadata doesn't have it.

    Args:
        config: The RunnableConfig passed to the tool

    Returns:
        The QuantConnect project ID or None
    """
    # Defensive type check - config should be a dict
    if config is None:
        return None
    if not isinstance(config, dict):
        import structlog
        logger = structlog.get_logger()
        logger.warning(f"get_qc_project_id_from_thread received non-dict config: {type(config).__name__}, value: {str(config)[:100]}")
        return None

    configurable = config.get("configurable", {})
    thread_id = configurable.get("thread_id")

    if not thread_id:
        return None

    # First try to get from thread metadata
    metadata = await get_thread_metadata(thread_id)
    if metadata:
        qc_project_id = metadata.get("qc_project_id")
        if qc_project_id is not None:
            return int(qc_project_id)

    # Fallback to config.configurable (in case it's passed through context)
    qc_project_id = configurable.get("qc_project_id")
    if qc_project_id is not None:
        return int(qc_project_id)

    # Final fallback to environment variable
    env_id = os.environ.get("QC_PROJECT_ID")
    return int(env_id) if env_id else None


async def get_project_db_id_from_thread(config: RunnableConfig) -> str | None:
    """Extract project_db_id (our Supabase project ID) from thread metadata.

    Args:
        config: The RunnableConfig passed to the tool

    Returns:
        The Supabase project ID or None
    """
    # Defensive type check - config should be a dict
    if config is None:
        return None
    if not isinstance(config, dict):
        import structlog
        logger = structlog.get_logger()
        logger.warning(f"get_project_db_id_from_thread received non-dict config: {type(config).__name__}, value: {str(config)[:100]}")
        return None

    configurable = config.get("configurable", {})
    thread_id = configurable.get("thread_id")

    if not thread_id:
        return None

    # First try to get from thread metadata
    metadata = await get_thread_metadata(thread_id)
    if metadata:
        project_id = metadata.get("project_id")
        if project_id is not None:
            return str(project_id)

    # Fallback to config.configurable
    project_id = configurable.get("project_db_id")
    if project_id is not None:
        return str(project_id)

    return None
