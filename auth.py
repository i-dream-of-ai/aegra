"""
Authentication configuration for LangGraph Agent Server.

This module provides environment-based authentication switching between:
- noop: No authentication (allow all requests)
- custom: Custom authentication integration

Set AUTH_TYPE environment variable to choose authentication mode.
"""

import os
from typing import Any

import structlog
from langgraph_sdk import Auth

logger = structlog.getLogger(__name__)

# Initialize LangGraph Auth instance
auth = Auth()

# Get authentication type from environment
AUTH_TYPE = os.getenv("AUTH_TYPE", "noop").lower()

if AUTH_TYPE == "noop":
    logger.info("Using noop authentication (no auth required)")

    @auth.authenticate
    async def authenticate(headers: dict[str, str]) -> Auth.types.MinimalUserDict:
        """No-op authentication that allows all requests."""
        _ = headers  # Suppress unused warning
        return {
            "identity": "anonymous",
            "display_name": "Anonymous User",
            "is_authenticated": True,
        }

    @auth.on
    async def authorize(
        ctx: Auth.types.AuthContext, value: dict[str, Any]
    ) -> dict[str, Any]:
        """No-op authorization that allows access to all resources."""
        _ = ctx, value  # Suppress unused warnings
        return {}  # Empty filter = no access restrictions

elif AUTH_TYPE == "custom":
    logger.info("Using custom authentication")

    @auth.authenticate
    async def authenticate(headers: dict[str, str]) -> Auth.types.MinimalUserDict:
        """
        Custom authentication handler for Supabase JWT tokens.

        Extracts:
        - Authorization Bearer token (Supabase access_token)
        - X-User-Id header (Supabase user ID)
        - X-Project-Id header (optional project context)
        """

        def get_header(name: str) -> str | None:
            """Get header value, handling both string and bytes keys."""
            value = (
                headers.get(name.lower())
                or headers.get(name)
                or headers.get(name.lower().encode())
                or headers.get(name.encode())
            )
            if isinstance(value, bytes):
                return value.decode("utf-8")
            return value

        # Extract authorization header
        authorization = get_header("authorization")

        if not authorization:
            logger.warning("Missing Authorization header")
            raise Auth.exceptions.HTTPException(
                status_code=401, detail="Authorization header required"
            )

        if not authorization.startswith("Bearer "):
            raise Auth.exceptions.HTTPException(
                status_code=401,
                detail="Invalid authorization format. Expected 'Bearer <token>'",
            )

        # Extract access token from Bearer header
        access_token = authorization[7:]  # Remove "Bearer " prefix

        # Get user ID from header (set by Next.js proxy from Supabase session)
        user_id = get_header("x-user-id")
        if not user_id:
            logger.warning("Missing X-User-Id header")
            raise Auth.exceptions.HTTPException(
                status_code=401, detail="X-User-Id header required"
            )

        # Get optional project ID from header
        project_id = get_header("x-project-id")

        # Get optional user email from header
        user_email = get_header("x-user-email")

        # Return user data including access token for graph middleware
        return {
            "identity": user_id,
            "display_name": user_email or user_id,
            "email": user_email,
            "is_authenticated": True,
            # Include access_token and project_id for graph context
            "access_token": access_token,
            "project_db_id": project_id,
        }

    @auth.on
    async def authorize(
        ctx: Auth.types.AuthContext, value: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Multi-tenant authorization with user-scoped access control.
        """
        try:
            # Get user identity from authentication context
            user_id = ctx.user.identity

            if not user_id:
                logger.error("Missing user identity in auth context")
                raise Auth.exceptions.HTTPException(
                    status_code=401, detail="Invalid user identity"
                )

            # Create owner filter for resource access control
            owner_filter = {"owner": user_id}

            # Add owner information to metadata for create/update operations
            metadata = value.setdefault("metadata", {})
            metadata.update(owner_filter)

            # Return filter for database operations
            return owner_filter

        except Auth.exceptions.HTTPException:
            raise
        except Exception as e:
            logger.error(f"Authorization error: {e}", exc_info=True)
            raise Auth.exceptions.HTTPException(
                status_code=500, detail="Authorization system error"
            ) from e

else:
    raise ValueError(
        f"Unknown AUTH_TYPE: {AUTH_TYPE}. Supported values: 'noop', 'custom'"
    )
