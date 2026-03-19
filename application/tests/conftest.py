"""
Pytest fixtures for WaterBot API tests.

Installs against the real requirements.txt dependencies; external I/O
(Claude, Bedrock, PostgreSQL) is prevented by env var config and by
overriding the module-level singletons after import.

POSTGRES_ENABLED is False (no DB env vars) → psycopg2.connect is never called.
AWS_KB_ID is set to a dummy value → BedrockKnowledgeBase is instantiated
  (boto3.client() at init time doesn't make network calls) then immediately
  replaced with a mock after import.
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

# Ensure env vars are set before main.py is imported
os.environ.setdefault("CLAUDE_API_KEY", "test-key-not-real")
os.environ.setdefault("AWS_KB_ID", "test-kb-id")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
# No DATABASE_URL / DB_HOST → POSTGRES_ENABLED=False → psycopg2.connect never called
os.environ.pop("DATABASE_URL", None)
os.environ.pop("DB_HOST", None)

# Add application/ dir to path so `import main` works from any working directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_llm_adapter():
    adapter = MagicMock()
    adapter.get_embeddings.return_value = MagicMock()
    adapter.safety_checks = AsyncMock(
        return_value=(
            False,
            json.dumps({"user_intent": 0, "prompt_injection": 0, "unrelated_topic": 0}),
        )
    )
    adapter.get_llm_body = AsyncMock(
        return_value=json.dumps({"messages": [], "temperature": 0.5})
    )
    adapter.generate_response = AsyncMock(return_value="Test answer from mock.")
    return adapter


def _make_knowledge_base():
    kb = MagicMock()
    kb.ann_search = AsyncMock(return_value={"documents": ["doc1"], "sources": []})
    kb.knowledge_to_string = AsyncMock(return_value="Mock knowledge content.")
    return kb


@pytest.fixture(scope="session")
def app():
    """
    Import the FastAPI app once per session with all external calls mocked.
    """
    import main as main_module

    main_module.llm_adapter = _make_llm_adapter()
    main_module.knowledge_base = _make_knowledge_base()

    yield main_module.app


@pytest.fixture()
async def client(app):
    """Async HTTP client bound to the FastAPI app — no real network."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
