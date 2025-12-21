"""
Supabase client helper for Python tools.

Provides authenticated access to Supabase using either:
1. User's access token (for RLS-protected tables like code_versions)
2. Service role key (for public tables like algorithm_knowledge_base)
"""

import os
from typing import Any

import httpx
from langchain_core.runnables import RunnableConfig


def get_supabase_config() -> tuple[str, str]:
    """Get Supabase URL and service role key from environment."""
    supabase_url = os.environ.get("SUPABASE_URL") or os.environ.get(
        "NEXT_PUBLIC_SUPABASE_URL"
    )
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    return supabase_url or "", supabase_key or ""


def get_user_token(config: RunnableConfig) -> str | None:
    """Extract user's access token from LangGraph config."""
    configurable = config.get("configurable", {})
    auth_user = configurable.get("langgraph_auth_user", {})
    return auth_user.get("access_token")


def get_project_db_id(config: RunnableConfig) -> str | None:
    """Extract project_db_id from config."""
    configurable = config.get("configurable", {})
    return configurable.get("project_db_id") or os.environ.get("PROJECT_DB_ID")


def get_qc_project_id(config: RunnableConfig) -> int | None:
    """Extract qc_project_id from RunnableConfig."""
    configurable = config.get("configurable", {})
    project_id = configurable.get("qc_project_id")
    if project_id is not None:
        return int(project_id)
    env_id = os.environ.get("QC_PROJECT_ID")
    return int(env_id) if env_id else None


class SupabaseClient:
    """
    Async Supabase REST API client.

    Usage:
        client = SupabaseClient(config)  # Uses user token for RLS
        data = await client.select("code_versions", {"project_id": "eq.xxx"})

        # For public tables, pass use_service_role=True
        client = SupabaseClient(config, use_service_role=True)
    """

    def __init__(self, config: RunnableConfig = None, use_service_role: bool = False):
        self.supabase_url, self.service_role_key = get_supabase_config()

        # Use user token for RLS, or service role for public access
        if use_service_role or config is None:
            self.token = self.service_role_key
        else:
            self.token = get_user_token(config) or self.service_role_key

        self.anon_key = (
            os.environ.get("SUPABASE_ANON_KEY")
            or os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY")
            or ""
        )

    def _headers(self) -> dict[str, str]:
        """Build request headers."""
        return {
            "Authorization": f"Bearer {self.token}",
            "apikey": self.anon_key or self.service_role_key,
            "Content-Type": "application/json",
        }

    async def select(
        self,
        table: str,
        params: dict[str, str] = None,
        timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        """
        SELECT from a table.

        Args:
            table: Table name
            params: Query params (e.g., {"id": "eq.123", "select": "*"})
            timeout: Request timeout in seconds

        Returns:
            List of matching rows
        """
        if not self.supabase_url:
            raise ValueError("Supabase URL not configured")

        url = f"{self.supabase_url}/rest/v1/{table}"

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, params=params, headers=self._headers())
            response.raise_for_status()
            return response.json() or []

    async def insert(
        self,
        table: str,
        data: dict[str, Any] | list[dict[str, Any]],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        """
        INSERT into a table.

        Args:
            table: Table name
            data: Row(s) to insert
            timeout: Request timeout in seconds

        Returns:
            Inserted rows
        """
        if not self.supabase_url:
            raise ValueError("Supabase URL not configured")

        url = f"{self.supabase_url}/rest/v1/{table}"
        headers = self._headers()
        headers["Prefer"] = "return=representation"

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=data, headers=headers)
            response.raise_for_status()
            return response.json() or []

    async def update(
        self,
        table: str,
        data: dict[str, Any],
        match: dict[str, str],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        """
        UPDATE rows in a table.

        Args:
            table: Table name
            data: Fields to update
            match: Filter params (e.g., {"id": "eq.123"})
            timeout: Request timeout in seconds

        Returns:
            Updated rows
        """
        if not self.supabase_url:
            raise ValueError("Supabase URL not configured")

        url = f"{self.supabase_url}/rest/v1/{table}"
        headers = self._headers()
        headers["Prefer"] = "return=representation"

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.patch(url, params=match, json=data, headers=headers)
            response.raise_for_status()
            return response.json() or []

    async def delete(
        self,
        table: str,
        match: dict[str, str],
        timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        """
        DELETE rows from a table.

        Args:
            table: Table name
            match: Filter params (e.g., {"id": "eq.123"})
            timeout: Request timeout in seconds

        Returns:
            Deleted rows
        """
        if not self.supabase_url:
            raise ValueError("Supabase URL not configured")

        url = f"{self.supabase_url}/rest/v1/{table}"
        headers = self._headers()
        headers["Prefer"] = "return=representation"

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.delete(url, params=match, headers=headers)
            response.raise_for_status()
            return response.json() or []

    async def rpc(
        self,
        function_name: str,
        params: dict[str, Any] = None,
        timeout: float = 30.0,
    ) -> Any:
        """
        Call a Postgres RPC function.

        Args:
            function_name: Function name
            params: Function parameters
            timeout: Request timeout in seconds

        Returns:
            Function result
        """
        if not self.supabase_url:
            raise ValueError("Supabase URL not configured")

        url = f"{self.supabase_url}/rest/v1/rpc/{function_name}"

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url, json=params or {}, headers=self._headers()
            )
            response.raise_for_status()
            return response.json()
