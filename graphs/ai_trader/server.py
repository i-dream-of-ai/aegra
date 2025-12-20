"""FastAPI server for the AI Trading Agent - No license required."""

import os
import json
import asyncio
from uuid import uuid4
from typing import Any, AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

# Import the graph
from src.graph import graph

# In-memory thread storage (for production, use Redis or Postgres)
threads: dict[str, list[dict]] = {}


class Message(BaseModel):
    role: str
    content: str


class CreateThreadResponse(BaseModel):
    thread_id: str


class RunRequest(BaseModel):
    input: dict[str, Any]
    config: dict[str, Any] | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    print("Starting AI Trading Agent server...")
    yield
    print("Shutting down...")


app = FastAPI(
    title="AI Trading Agent",
    description="LangGraph-based trading agent for QuantConnect",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/ok")
async def health_check():
    """Health check endpoint."""
    return {"ok": True}


@app.get("/info")
async def info():
    """Server info endpoint."""
    return {
        "version": "1.0.0",
        "graphs": ["agent"],
    }


@app.post("/threads", response_model=CreateThreadResponse)
async def create_thread():
    """Create a new conversation thread."""
    thread_id = str(uuid4())
    threads[thread_id] = []
    return {"thread_id": thread_id}


@app.get("/threads/{thread_id}")
async def get_thread(thread_id: str):
    """Get thread state."""
    if thread_id not in threads:
        raise HTTPException(status_code=404, detail="Thread not found")
    return {"thread_id": thread_id, "messages": threads[thread_id]}


@app.post("/threads/{thread_id}/runs/stream")
async def stream_run(thread_id: str, request: RunRequest):
    """Stream a run on the graph."""
    if thread_id not in threads:
        threads[thread_id] = []

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            # Get messages from request
            messages = request.input.get("messages", [])

            # Build config with thread_id for checkpointing
            config = request.config or {}
            config["configurable"] = config.get("configurable", {})
            config["configurable"]["thread_id"] = thread_id

            # Add any extra config (like qc_project_id)
            if "qc_project_id" in request.input:
                config["configurable"]["qc_project_id"] = request.input["qc_project_id"]

            # Stream events from the graph
            async for event in graph.astream_events(
                {"messages": messages},
                config=config,
                version="v2",
            ):
                event_type = event.get("event")

                if event_type == "on_chat_model_stream":
                    # Stream tokens
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        yield f"data: {json.dumps({'type': 'token', 'content': chunk.content})}\n\n"

                elif event_type == "on_tool_start":
                    # Tool started
                    tool_name = event.get("name", "unknown")
                    tool_input = event.get("data", {}).get("input", {})
                    yield f"data: {json.dumps({'type': 'tool_start', 'tool': tool_name, 'input': tool_input})}\n\n"

                elif event_type == "on_tool_end":
                    # Tool ended
                    tool_name = event.get("name", "unknown")
                    tool_output = event.get("data", {}).get("output", "")
                    yield f"data: {json.dumps({'type': 'tool_end', 'tool': tool_name, 'output': str(tool_output)[:1000]})}\n\n"

            # Send done event
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.post("/threads/{thread_id}/runs")
async def run(thread_id: str, request: RunRequest):
    """Run the graph (non-streaming)."""
    if thread_id not in threads:
        threads[thread_id] = []

    # Get messages from request
    messages = request.input.get("messages", [])

    # Build config
    config = request.config or {}
    config["configurable"] = config.get("configurable", {})
    config["configurable"]["thread_id"] = thread_id

    if "qc_project_id" in request.input:
        config["configurable"]["qc_project_id"] = request.input["qc_project_id"]

    # Invoke the graph
    result = await graph.ainvoke({"messages": messages}, config=config)

    return {"result": result}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
