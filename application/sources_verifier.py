"""
Claude-based classifier to decide whether a response deserves a sources list.
Uses CLAUDE_API_KEY (or ANTHROPIC_API_KEY) with a heuristic fallback when the API is unavailable.
"""

import os
import re
import logging
import asyncio
from typing import List, Any

# Configuration knobs (env with defaults)
SOURCES_VERIFIER_MODEL = os.getenv("SOURCES_VERIFIER_MODEL", "claude-sonnet-4-6")
SOURCES_VERIFIER_TIMEOUT = float(os.getenv("SOURCES_VERIFIER_TIMEOUT", "5.0"))
SOURCES_VERIFIER_MAX_TOKENS = int(os.getenv("SOURCES_VERIFIER_MAX_TOKENS", "64"))

GREETING_REGEX = re.compile(
    r"\b(hi|hello|hey|hola|howdy|sup|yo|hiya|thanks|thank you|thx|good morning|good afternoon|good evening)\b",
    re.I,
)


def _heuristic_should_show_sources(user_query: str, sources: List[Any]) -> bool:
    """Fallback when OpenAI is unavailable: no sources or short/greeting-only -> False."""
    if not user_query or not isinstance(user_query, str):
        return False
    text = user_query.strip()
    if len(text) <= 6:
        return False
    if GREETING_REGEX.search(text) and len(text) < 80:
        return False
    if not sources or (isinstance(sources, list) and len(sources) == 0):
        return False
    return True


async def should_show_sources(
    user_query: str,
    bot_response: str = "",
    sources: List[Any] = None,
) -> bool:
    """
    Return True if the last response deserves a sources list, False otherwise.
    Uses Claude with timeout and error handling; falls back to heuristics on failure.
    """
    sources = sources or []
    user_query = (user_query or "").strip()
    bot_response = (bot_response or "").strip()

    # Heuristic: no sources stored -> do not show sources
    if not sources or (isinstance(sources, list) and len(sources) == 0):
        return False

    api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logging.warning("SOURCES_VERIFIER: CLAUDE_API_KEY/ANTHROPIC_API_KEY not set, using heuristic only.")
        return _heuristic_should_show_sources(user_query, sources)

    system_prompt = """You are a classifier for a water-in-Arizona chatbot. Given the user's last question and the bot's reply, decide whether showing "sources" (citations) makes sense.

Answer YES only if the user asked a substantive, informational question and the bot gave an answer that could be backed by documents (e.g. facts about water, policy, quality). Answer NO for greetings, small talk, "hi", "hello", "thanks", unrelated content, or when the exchange is not about factual information that would have sources."""

    user_prompt = f"User question: {user_query}\n\nBot reply (excerpt): {bot_response[:500] if bot_response else '(no reply)'}\n\nShould we show sources? Answer only YES or NO."

    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=api_key)
        response = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: client.messages.create(
                    model=SOURCES_VERIFIER_MODEL,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                    max_tokens=SOURCES_VERIFIER_MAX_TOKENS,
                    temperature=0,
                )
            ),
            timeout=SOURCES_VERIFIER_TIMEOUT,
        )
        content_blocks = getattr(response, "content", None) or []
        text_out = ""
        for block in content_blocks:
            block_text = getattr(block, "text", None)
            if isinstance(block_text, str):
                text_out += block_text

        if not text_out:
            return _heuristic_should_show_sources(user_query, sources)
        content = text_out.strip().upper()
        return "YES" in content and not content.startswith("NO")
    except asyncio.TimeoutError:
        logging.warning("SOURCES_VERIFIER: Claude request timed out, using heuristic.")
        return _heuristic_should_show_sources(user_query, sources)
    except Exception as e:
        logging.warning("SOURCES_VERIFIER: Claude request failed (%s), using heuristic.", e)
        return _heuristic_should_show_sources(user_query, sources)
