"""
AI Trader Agent - Using create_agent with SummarizationMiddleware

Uses the LangChain create_agent API with built-in SummarizationMiddleware
for automatic context window management.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import structlog
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware

from ai_trader.context import Context
from ai_trader.prompts import DEFAULT_MAIN_PROMPT

# Import all tools
from ai_trader.tools.ai_services import TOOLS as AI_SERVICES_TOOLS
from ai_trader.tools.backtest import TOOLS as BACKTEST_TOOLS
from ai_trader.tools.compile import TOOLS as COMPILE_TOOLS
from ai_trader.tools.composite import TOOLS as COMPOSITE_TOOLS
from ai_trader.tools.files import TOOLS as FILES_TOOLS
from ai_trader.tools.misc import TOOLS as MISC_TOOLS
from ai_trader.tools.object_store import TOOLS as OBJECT_STORE_TOOLS
from ai_trader.tools.optimization import TOOLS as OPTIMIZATION_TOOLS
from ai_trader.tools.review import TOOLS as REVIEW_TOOLS

logger = structlog.getLogger(__name__)

# Combine all tools
ALL_TOOLS = (
    FILES_TOOLS
    + BACKTEST_TOOLS
    + COMPILE_TOOLS
    + COMPOSITE_TOOLS
    + OPTIMIZATION_TOOLS
    + OBJECT_STORE_TOOLS
    + AI_SERVICES_TOOLS
    + MISC_TOOLS
    + REVIEW_TOOLS
)

# Get model from environment
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "gpt-5.2")

# Build system prompt with timestamp
system_prompt = DEFAULT_MAIN_PROMPT.format(system_time=datetime.now(tz=UTC).isoformat())

# Create agent with SummarizationMiddleware
graph = create_agent(
    model=DEFAULT_MODEL,
    tools=ALL_TOOLS,
    system_prompt=system_prompt,
    context_schema=Context,
    middleware=[
        SummarizationMiddleware(
            model="gpt-4o-mini",
            trigger=("tokens", 100000),  # Summarize at 100K tokens
            keep=("messages", 20),       # Keep last 20 messages unsummarized
        ),
    ],
    name="AI Trader",
)
