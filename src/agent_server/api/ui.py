"""UI Component endpoints for serving remote React components

This implements the /ui/{assistant_id} endpoint expected by the LangGraph SDK's
LoadExternalComponent. The SDK sends POST requests with { name: componentName }
and expects HTML/JavaScript back that can be rendered as a React component.

For now, this returns 404 for all components since all UI components are
registered locally in the Next.js app. This endpoint exists to prevent
network errors when the SDK attempts to fetch remote components.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

router = APIRouter()


class UIComponentRequest(BaseModel):
    """Request body for UI component fetch"""
    name: str


@router.post("/ui/{assistant_id}", response_class=HTMLResponse)
async def get_ui_component(
    assistant_id: str,
    request: UIComponentRequest,
):
    """Get a UI component for rendering in the client.

    The LangGraph SDK's LoadExternalComponent calls this endpoint when it
    can't find a component in the local registry. We return 404 to indicate
    the component should be handled locally.

    In the future, this could serve server-rendered React components or
    return component definitions that the client can render.
    """
    # For now, all components are registered locally in the Next.js app
    # Return 404 to indicate no remote component is available
    raise HTTPException(
        status_code=404,
        detail=f"Component '{request.name}' not found for assistant '{assistant_id}'. "
               f"Ensure the component is registered in the client-side component map."
    )
