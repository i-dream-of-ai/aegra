"""Thread context utilities for fetching project info from thread metadata.

Instead of passing project context through the SDK (which doesn't work reliably),
we store project IDs in thread metadata when creating/submitting to threads.
Tools can then fetch this metadata directly from the database.
"""

import os
from functools import lru_cache

import asyncpg
from langchain_core.runnables import RunnableConfig


@lru_cache(maxsize=1)
def get_database_url() -> str:
    """Get database URL from environment."""
    return os.environ.get("DATABASE_URL", "")


async def get_thread_metadata(thread_id: str) -> dict | None:
    """Fetch thread metadata from database.

    Args:
        thread_id: The thread ID to fetch metadata for

    Returns:
        Thread metadata dict or None if not found
    """
    db_url = get_database_url()
    if not db_url:
        return None

    try:
        conn = await asyncpg.connect(db_url)
        try:
            row = await conn.fetchrow(
                "SELECT metadata_json FROM thread WHERE thread_id = $1",
                thread_id
            )
            if row:
                return row["metadata_json"]
            return None
        finally:
            await conn.close()
    except Exception as e:
        import structlog
        logger = structlog.get_logger()
        logger.warning(f"Failed to fetch thread metadata: {e}")
        return None


async def get_qc_project_id_from_thread(config: RunnableConfig) -> int | None:
    """Extract qc_project_id from thread metadata.

    This is the preferred way to get project context in tools.
    Falls back to config.configurable if thread metadata doesn't have it.

    Args:
        config: The RunnableConfig passed to the tool

    Returns:
        The QuantConnect project ID or None
    """
    configurable = config.get("configurable", {}) if config else {}
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
    configurable = config.get("configurable", {}) if config else {}
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
