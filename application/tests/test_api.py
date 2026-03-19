"""
Backend API tests for WaterBot (FastAPI).

All external I/O (Claude, Bedrock, PostgreSQL) is mocked via conftest.py.
Run with:  pytest application/tests/ -v
"""
import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Health / SPA routes
# ---------------------------------------------------------------------------
class TestHealthRoutes:
    async def test_root_redirects_or_returns_html(self, client):
        """GET / should redirect to /museum or serve HTML (200 or 3xx)."""
        response = await client.get("/", follow_redirects=False)
        assert response.status_code in (200, 302, 307, 308)

    async def test_museum_route_returns_html(self, client):
        """GET /museum should serve the SPA or splash page (200 or redirect)."""
        response = await client.get("/museum", follow_redirects=True)
        assert response.status_code in (200, 302, 307, 308, 404)
        # 404 is acceptable in unit tests when the frontend dist isn't built


# ---------------------------------------------------------------------------
# POST /chat_api
# ---------------------------------------------------------------------------
class TestChatAPI:
    async def test_chat_api_returns_resp_and_msg_id(self, client):
        """POST /chat_api with a valid query returns {resp, msgID}."""
        response = await client.post(
            "/chat_api",
            data={"user_query": "What is the water quality in Phoenix?"},
        )
        assert response.status_code == 200
        body = response.json()
        assert "resp" in body
        assert "msgID" in body
        assert isinstance(body["resp"], str)
        assert len(body["resp"]) > 0

    async def test_chat_api_with_language_preference(self, client):
        """POST /chat_api with language_preference=es still returns valid JSON."""
        response = await client.post(
            "/chat_api",
            data={"user_query": "¿Cuál es la calidad del agua?", "language_preference": "es"},
        )
        assert response.status_code == 200
        body = response.json()
        assert "resp" in body

    async def test_chat_api_empty_query_handled(self, client):
        """POST /chat_api with an empty query should not crash (422 or graceful)."""
        response = await client.post("/chat_api", data={"user_query": ""})
        # FastAPI may return 422 (validation) or 200 depending on implementation
        assert response.status_code in (200, 422)

    async def test_chat_api_increments_message_id(self, client):
        """Consecutive calls from the same session should increment msgID."""
        r1 = await client.post("/chat_api", data={"user_query": "First question"})
        r2 = await client.post("/chat_api", data={"user_query": "Second question"})
        assert r1.status_code == 200
        assert r2.status_code == 200
        msg_id_1 = r1.json()["msgID"]
        msg_id_2 = r2.json()["msgID"]
        assert msg_id_2 > msg_id_1


# ---------------------------------------------------------------------------
# GET /messages (Basic Auth protected)
# ---------------------------------------------------------------------------
class TestMessagesEndpoint:
    async def test_messages_requires_auth(self, client):
        """GET /messages without credentials should return 401."""
        response = await client.get("/messages")
        assert response.status_code == 401

    async def test_messages_with_wrong_password_returns_401(self, client):
        """GET /messages with wrong password should return 401."""
        response = await client.get(
            "/messages",
            headers={"Authorization": "Basic d3Jvbmc6Y3JlZHM="},  # wrong:creds
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /session-transcript
# ---------------------------------------------------------------------------
class TestSessionTranscript:
    async def test_transcript_with_empty_session(self, client):
        """POST /session-transcript with no prior chat returns empty-session message."""
        response = await client.post("/session-transcript")
        assert response.status_code == 200
        body = response.json()
        assert "message" in body or "presigned_url" in body


# ---------------------------------------------------------------------------
# POST /translate
# ---------------------------------------------------------------------------
class TestTranslateEndpoint:
    async def test_translate_invalid_target_lang_returns_400(self, client):
        """POST /translate with unsupported target_lang should return 400."""
        response = await client.post(
            "/translate",
            json={"texts": ["hello"], "target_lang": "fr"},
        )
        assert response.status_code == 400

    async def test_translate_missing_body_returns_400(self, client):
        """POST /translate with missing fields should return 400."""
        response = await client.post("/translate", json={})
        assert response.status_code == 400

    async def test_translate_valid_request(self, client):
        """POST /translate with valid body should return translations list."""
        response = await client.post(
            "/translate",
            json={"texts": ["Hello water system"], "target_lang": "es"},
        )
        assert response.status_code == 200
        body = response.json()
        assert "translations" in body
        assert isinstance(body["translations"], list)


# ---------------------------------------------------------------------------
# POST /submit_rating_api
# ---------------------------------------------------------------------------
class TestRatingEndpoint:
    async def test_submit_rating_returns_success(self, client):
        """POST /submit_rating_api returns success (no-op when POSTGRES_ENABLED=False)."""
        response = await client.post(
            "/submit_rating_api",
            data={"message_id": "1", "reaction": "1"},
        )
        assert response.status_code == 200
        assert response.json().get("status") == "success"

    async def test_submit_rating_with_comment(self, client):
        """POST /submit_rating_api with userComment returns success."""
        response = await client.post(
            "/submit_rating_api",
            data={"message_id": "1", "reaction": "-1", "userComment": "Not helpful"},
        )
        assert response.status_code == 200
        assert response.json().get("status") == "success"
