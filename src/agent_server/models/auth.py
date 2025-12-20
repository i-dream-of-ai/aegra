"""Authentication and user context models"""

from pydantic import BaseModel


class User(BaseModel):
    """User context model for authentication"""

    identity: str
    display_name: str | None = None
    permissions: list[str] = []
    org_id: str | None = None
    is_authenticated: bool = True
    # Additional fields for graph context
    access_token: str | None = None
    project_db_id: str | None = None
    email: str | None = None

    def to_dict(self) -> dict:
        """Return user data as dict for graph context injection."""
        return {
            "identity": self.identity,
            "display_name": self.display_name,
            "permissions": self.permissions,
            "org_id": self.org_id,
            "is_authenticated": self.is_authenticated,
            "access_token": self.access_token,
            "project_db_id": self.project_db_id,
            "email": self.email,
        }


class AuthContext(BaseModel):
    """Authentication context for request processing"""

    user: User
    request_id: str | None = None

    class Config:
        arbitrary_types_allowed = True


class TokenPayload(BaseModel):
    """JWT token payload structure"""

    sub: str  # subject (user ID)
    name: str | None = None
    scopes: list[str] = []
    org: str | None = None
    exp: int | None = None
    iat: int | None = None
