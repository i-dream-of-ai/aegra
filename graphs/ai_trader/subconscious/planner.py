"""
Planner Agent for Subconscious Layer

Analyzes conversation to understand user intent and generates
targeted retrieval queries for skills/knowledge.
"""

import os
import json
from typing import List, Optional
from dataclasses import dataclass
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage


@dataclass
class RetrievalPlan:
    """Output from the planner agent."""
    user_intent: str
    keyword_queries: List[str]
    semantic_queries: List[str]
    skip_reason: Optional[str] = None


# Planner system prompt
PLANNER_PROMPT = """You are a planning agent that analyzes conversations to understand what knowledge would help.

Your task:
1. Understand what the user is trying to accomplish
2. Generate targeted queries to retrieve relevant skills/knowledge

Output JSON with this structure:
{
  "user_intent": "Brief description of what user wants to accomplish",
  "keyword_queries": ["exact", "tag", "matches"],
  "semantic_queries": ["natural language queries for semantic search"]
}

Focus on:
- Trading strategies (momentum, mean reversion, breakout, etc.)
- Technical indicators (RSI, MACD, Bollinger, etc.)
- Risk management patterns
- QuantConnect-specific patterns
- User preferences mentioned in conversation

Keep queries specific and actionable. Max 5 keyword queries, max 3 semantic queries."""


async def generate_retrieval_plan(
    messages: List[dict],
    recent_context: str,
) -> RetrievalPlan:
    """
    Analyze conversation and generate retrieval queries.

    Args:
        messages: Recent conversation messages
        recent_context: Extracted context from recent messages

    Returns:
        RetrievalPlan with queries for skill retrieval
    """
    # Use Haiku for fast planning
    model = ChatAnthropic(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        temperature=0.3,
    )

    # Build conversation summary
    conv_summary = []
    for msg in messages[-10:]:  # Last 10 messages
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        if content:
            conv_summary.append(f"{role}: {content[:500]}")

    conversation_text = "\n".join(conv_summary)

    try:
        response = await model.ainvoke([
            SystemMessage(content=PLANNER_PROMPT),
            HumanMessage(content=f"""Analyze this conversation and generate retrieval queries:

CONVERSATION:
{conversation_text}

RECENT CONTEXT:
{recent_context}

Output JSON with user_intent, keyword_queries, and semantic_queries."""),
        ])

        # Parse JSON response
        content = response.content
        if isinstance(content, list):
            content = content[0].get("text", "") if content else ""

        # Extract JSON from response
        json_start = content.find("{")
        json_end = content.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            json_str = content[json_start:json_end]
            data = json.loads(json_str)
            return RetrievalPlan(
                user_intent=data.get("user_intent", ""),
                keyword_queries=data.get("keyword_queries", [])[:5],
                semantic_queries=data.get("semantic_queries", [])[:3],
            )

    except Exception as e:
        print(f"[Planner] Error generating plan: {e}")

    # Fallback: extract simple keywords from context
    keywords = extract_simple_keywords(recent_context)
    return RetrievalPlan(
        user_intent="Unable to determine",
        keyword_queries=keywords,
        semantic_queries=[recent_context[:200]] if recent_context else [],
    )


def extract_simple_keywords(text: str) -> List[str]:
    """Extract simple keywords from text as fallback."""
    # Common trading/QC keywords to look for
    trading_keywords = {
        "momentum", "mean reversion", "breakout", "trend", "rsi", "macd",
        "bollinger", "moving average", "sma", "ema", "backtest", "optimize",
        "risk", "drawdown", "sharpe", "position sizing", "stop loss",
        "take profit", "leverage", "portfolio", "rebalance", "universe",
        "selection", "alpha", "beta", "volatility", "correlation",
    }

    text_lower = text.lower()
    found = [kw for kw in trading_keywords if kw in text_lower]
    return found[:5] if found else []
