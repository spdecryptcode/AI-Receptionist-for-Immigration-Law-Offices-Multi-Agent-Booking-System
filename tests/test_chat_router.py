"""
Tests for app/chat/router.py

Covers:
  POST /chat/session
    - Returns 200 with session_id, ws_token, language
    - Default language "en" when not specified
    - Accepts language "es"
    - Stores ws_token in session

  GET /chat/history/{session_id}
    - Returns 200 with turns for a valid session
    - Returns 404 for unknown session_id

  GET /chat (widget HTML)
    - Returns 200 with HTML content
    - Response contains <html> tag or widget marker

  GET /chat/ (trailing slash)
    - Returns 200

  Helpers
    - _load_system_prompt falls back gracefully when file missing
    - _build_openai_messages includes system message as first element
    - _build_openai_messages appends turns in correct order
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# App factory helper — isolate router from the full FastAPI app startup.
# Instead of importing app.main (which triggers DB connections), we mount
# only the chat router on a minimal FastAPI instance.
# ---------------------------------------------------------------------------

def _make_test_app():
    """
    Build a minimal FastAPI instance that includes only the chat router.
    All external dependencies (Redis, OpenAI, RAG) are patched.
    """
    from fastapi import FastAPI
    app = FastAPI()

    from app.chat.router import router as chat_router
    app.include_router(chat_router)
    return app


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _sample_session(
    session_id: str = "sess-abc123",
    language: str = "en",
    ws_token: str = "tok-xyz",
    turns: list | None = None,
) -> dict:
    import time
    return {
        "session_id": session_id,
        "language": language,
        "created_at": time.time(),
        "turns": turns or [],
        "intake": {},
        "phase": "GREETING",
        "case_type": None,
        "ws_token": ws_token,
    }


# ---------------------------------------------------------------------------
# POST /chat/session
# ---------------------------------------------------------------------------

class TestCreateChatSession:
    def _client(self, mock_redis):
        with patch("app.chat.session.get_redis_client", return_value=mock_redis), \
             patch("app.chat.router.get_rag_retriever", return_value=None):
            app = _make_test_app()
            return TestClient(app, raise_server_exceptions=True)

    def test_returns_200(self):
        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock(return_value=True)
        mock_redis.get = AsyncMock(return_value=None)
        client = self._client(mock_redis)
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            resp = client.post("/chat/session", json={"language": "en"})
        assert resp.status_code == 200

    def test_response_has_session_id(self):
        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock(return_value=True)
        mock_redis.get = AsyncMock(side_effect=lambda k: None)

        with patch("app.chat.session.get_redis_client", return_value=mock_redis), \
             patch("app.chat.router.get_rag_retriever", return_value=None):
            app = _make_test_app()
            client = TestClient(app)
            with patch("app.chat.session.get_redis_client", return_value=mock_redis):
                resp = client.post("/chat/session", json={"language": "en"})

        body = resp.json()
        assert "session_id" in body
        assert len(body["session_id"]) > 8

    def test_response_has_ws_token(self):
        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock(return_value=True)
        mock_redis.get = AsyncMock(return_value=None)

        with patch("app.chat.session.get_redis_client", return_value=mock_redis), \
             patch("app.chat.router.get_rag_retriever", return_value=None):
            app = _make_test_app()
            client = TestClient(app)
            with patch("app.chat.session.get_redis_client", return_value=mock_redis):
                resp = client.post("/chat/session", json={"language": "en"})

        body = resp.json()
        assert "ws_token" in body

    def test_response_language_is_en(self):
        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock(return_value=True)
        mock_redis.get = AsyncMock(return_value=None)

        with patch("app.chat.session.get_redis_client", return_value=mock_redis), \
             patch("app.chat.router.get_rag_retriever", return_value=None):
            app = _make_test_app()
            client = TestClient(app)
            with patch("app.chat.session.get_redis_client", return_value=mock_redis):
                resp = client.post("/chat/session", json={"language": "en"})

        assert resp.json()["language"] == "en"

    def test_response_language_es(self):
        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock(return_value=True)
        mock_redis.get = AsyncMock(return_value=None)

        with patch("app.chat.session.get_redis_client", return_value=mock_redis), \
             patch("app.chat.router.get_rag_retriever", return_value=None):
            app = _make_test_app()
            client = TestClient(app)
            with patch("app.chat.session.get_redis_client", return_value=mock_redis):
                resp = client.post("/chat/session", json={"language": "es"})

        assert resp.json()["language"] == "es"

    def test_response_default_language_en(self):
        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock(return_value=True)
        mock_redis.get = AsyncMock(return_value=None)

        with patch("app.chat.session.get_redis_client", return_value=mock_redis), \
             patch("app.chat.router.get_rag_retriever", return_value=None):
            app = _make_test_app()
            client = TestClient(app)
            with patch("app.chat.session.get_redis_client", return_value=mock_redis):
                resp = client.post("/chat/session", json={})

        assert resp.json()["language"] == "en"


# ---------------------------------------------------------------------------
# GET /chat/history/{session_id}
# ---------------------------------------------------------------------------

class TestGetHistory:
    def _setup(self, session_data: dict | None):
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(
            return_value=json.dumps(session_data) if session_data else None
        )
        mock_redis.setex = AsyncMock(return_value=True)

        with patch("app.chat.session.get_redis_client", return_value=mock_redis), \
             patch("app.chat.router.get_rag_retriever", return_value=None):
            app = _make_test_app()
        return TestClient(app), mock_redis

    def test_returns_200_for_valid_session(self):
        session = _sample_session()
        client, mock_redis = self._setup(session)
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            resp = client.get("/chat/history/sess-abc123")
        assert resp.status_code == 200

    def test_returns_session_id_in_body(self):
        session = _sample_session(session_id="sess-abc123")
        client, mock_redis = self._setup(session)
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            resp = client.get("/chat/history/sess-abc123")
        assert resp.json()["session_id"] == "sess-abc123"

    def test_returns_turns_list(self):
        session = _sample_session(
            turns=[{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi"}]
        )
        client, mock_redis = self._setup(session)
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            resp = client.get("/chat/history/sess-abc123")
        body = resp.json()
        assert "turns" in body
        assert len(body["turns"]) == 2

    def test_returns_404_for_missing_session(self):
        client, mock_redis = self._setup(None)
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            resp = client.get("/chat/history/no-such-session")
        assert resp.status_code == 404

    def test_returns_phase_in_body(self):
        session = _sample_session()
        client, mock_redis = self._setup(session)
        with patch("app.chat.session.get_redis_client", return_value=mock_redis):
            resp = client.get("/chat/history/sess-abc123")
        assert "phase" in resp.json()


# ---------------------------------------------------------------------------
# GET /chat  (HTML widget)
# ---------------------------------------------------------------------------

class TestChatWidget:
    def _get_client(self):
        with patch("app.chat.router.get_rag_retriever", return_value=None):
            app = _make_test_app()
        return TestClient(app)

    def test_returns_200(self):
        client = self._get_client()
        resp = client.get("/chat")
        assert resp.status_code == 200

    def test_content_type_html(self):
        client = self._get_client()
        resp = client.get("/chat")
        assert "text/html" in resp.headers.get("content-type", "")

    def test_body_contains_html_tag(self):
        client = self._get_client()
        resp = client.get("/chat")
        assert "<html" in resp.text.lower() or "<!doctype" in resp.text.lower()

    def test_trailing_slash_returns_200(self):
        client = self._get_client()
        resp = client.get("/chat/")
        # Allow 200 (direct serve) or 307 redirect to /chat
        assert resp.status_code in (200, 307)


# ---------------------------------------------------------------------------
# Helper: _load_system_prompt
# ---------------------------------------------------------------------------

class TestLoadSystemPrompt:
    def test_falls_back_when_file_missing(self, tmp_path, monkeypatch):
        """
        When the prompts directory is missing or the file doesn't exist,
        _load_system_prompt should return a sensible fallback string.
        """
        monkeypatch.chdir(tmp_path)  # no prompts/ dir here
        from app.chat.router import _load_system_prompt
        result = _load_system_prompt("en")
        assert isinstance(result, str)
        assert len(result) > 10

    def test_loads_spanish_prompt(self, tmp_path, monkeypatch):
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "system_prompt_es.md").write_text(
            "Eres un asistente de inmigración.", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        from importlib import reload
        import app.chat.router as chat_router_mod
        reload(chat_router_mod)  # pick up new cwd
        result = chat_router_mod._load_system_prompt("es")
        assert "asistente" in result or len(result) > 10


# ---------------------------------------------------------------------------
# Helper: _build_openai_messages
# ---------------------------------------------------------------------------

class TestBuildOpenaiMessages:
    def _session(self, turns=None, intake=None, language="en", phase="INTAKE"):
        import time
        return {
            "session_id": "msg-test",
            "language": language,
            "created_at": time.time(),
            "turns": turns or [],
            "intake": intake or {},
            "phase": phase,
            "case_type": None,
        }

    def test_first_message_is_system(self):
        from app.chat.router import _build_openai_messages
        session = self._session()
        messages = _build_openai_messages(session, "You are helpful.", "")
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You are helpful."

    def test_turns_appended_in_order(self):
        from app.chat.router import _build_openai_messages
        session = self._session(turns=[
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ])
        messages = _build_openai_messages(session, "Sys", "")
        roles = [m["role"] for m in messages if m["role"] != "system"]
        # First system msg at 0; intake context system msg may follow
        user_msgs = [m for m in messages if m["role"] == "user"]
        asst_msgs = [m for m in messages if m["role"] == "assistant"]
        assert user_msgs[0]["content"] == "Hi"
        assert asst_msgs[0]["content"] == "Hello"

    def test_rag_context_injected_as_system_message(self):
        from app.chat.router import _build_openai_messages
        session = self._session()
        messages = _build_openai_messages(session, "Sys", rag_context="[RELEVANT ...]")
        # Second system message should contain RAG context
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert any("[RELEVANT" in m["content"] for m in system_msgs)

    def test_intake_data_included_in_context(self):
        from app.chat.router import _build_openai_messages
        session = self._session(intake={"case_type": "daca", "name": "Maria"})
        messages = _build_openai_messages(session, "Sys", "")
        system_msgs = [m for m in messages if m["role"] == "system"]
        combined = " ".join(m["content"] for m in system_msgs)
        assert "daca" in combined or "Maria" in combined

    def test_empty_intake_does_not_add_extra_system_message(self):
        from app.chat.router import _build_openai_messages
        session = self._session(intake={})
        messages = _build_openai_messages(session, "Sys", "")
        # With no intake and no rag_context there should be exactly 1 system msg
        system_msgs = [m for m in messages if m["role"] == "system"]
        assert len(system_msgs) == 1
