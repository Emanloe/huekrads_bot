"""A launch message is removed only after its own one-use session succeeds."""

import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import database
import gnome_avatars
from handlers import duel_service
from miniapp_api import create_miniapp_api
from miniapp_sessions import bind_launch_message_id, create_launch_token
from tests.test_miniapp_auth import TEST_BOT_TOKEN, signed_init_data


CHAT_A = -9901
CHAT_B = -9902


def launch(chat_id=CHAT_A, user_id=101, message_id=777, *, now=None):
    token = create_launch_token(chat_id, user_id, now=now)
    assert bind_launch_message_id(token, chat_id, user_id, message_id, now=now)
    return token


def payload(token, user_id=101):
    return {
        "init_data": signed_init_data(user={"id": user_id}, auth_date=int(time.time())),
        "launch_token": token,
    }


@pytest.mark.asyncio
async def test_success_deletes_bound_message_only_after_committed_session(temp_database, monkeypatch):
    token = launch(message_id=777)
    digest = hashlib.sha256(token.encode()).hexdigest()
    events = []

    async def delete_message(*, chat_id, message_id):
        with database.get_db() as conn:
            consumed = conn.execute(
                "SELECT consumed_at FROM miniapp_launch_tokens WHERE token_digest = ?",
                (digest,),
            ).fetchone()[0]
            session_count = conn.execute("SELECT COUNT(*) FROM miniapp_sessions").fetchone()[0]
        assert consumed is not None and session_count == 1
        events.append((chat_id, message_id))

    def forbidden(*args, **kwargs):
        raise AssertionError("launch cleanup used game or avatar RNG")

    monkeypatch.setattr(duel_service.random, "random", forbidden)
    monkeypatch.setattr(duel_service.random, "choice", forbidden)
    monkeypatch.setattr(gnome_avatars.secrets, "choice", forbidden)
    bot = SimpleNamespace(delete_message=AsyncMock(side_effect=delete_message))
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="", telegram_bot=bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        assert (await client.get("/app")).status_code == 200
        assert events == []
        response = await client.post("/api/v1/session", json=payload(token))
    assert response.status_code == 200
    assert set(response.json()) == {"session_token", "expires_at"}
    assert events == [(CHAT_A, 777)]
    bot.delete_message.assert_awaited_once_with(chat_id=CHAT_A, message_id=777)
    assert "launch_message_id" not in response.text


@pytest.mark.asyncio
async def test_failed_auth_wrong_user_expired_or_consumed_token_never_deletes(temp_database):
    token = launch(message_id=111)
    expired = launch(message_id=222, now=int(time.time()) - 121)
    bot = SimpleNamespace(delete_message=AsyncMock())
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="", telegram_bot=bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        bad_auth = await client.post("/api/v1/session", json={
            "init_data": "bad", "launch_token": token,
        })
        wrong_user = await client.post("/api/v1/session", json=payload(token, 202))
        expired_response = await client.post("/api/v1/session", json=payload(expired))
        assert [bad_auth.status_code, wrong_user.status_code, expired_response.status_code] == [
            401, 401, 401,
        ]
        bot.delete_message.assert_not_awaited()
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT consumed_at FROM miniapp_launch_tokens WHERE token_digest = ?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone() == (None,)
        success = await client.post("/api/v1/session", json=payload(token))
        assert success.status_code == 200
        bot.delete_message.assert_awaited_once_with(chat_id=CHAT_A, message_id=111)
        bot.delete_message.reset_mock()
        replay = await client.post("/api/v1/session", json=payload(token))
        assert replay.status_code == 401
        bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_failure_keeps_successful_session_and_does_not_retry(temp_database, caplog):
    token = launch(message_id=303)
    bot = SimpleNamespace(delete_message=AsyncMock(side_effect=RuntimeError("delete denied")))
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="", telegram_bot=bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        accepted = await client.post("/api/v1/session", json=payload(token))
        assert accepted.status_code == 200
        bearer = {"Authorization": f"Bearer {accepted.json()['session_token']}"}
        assert (await client.get("/api/v1/duel/active", headers=bearer)).status_code == 200
        assert (await client.post("/api/v1/session", json=payload(token))).status_code == 401
    bot.delete_message.assert_awaited_once_with(chat_id=CHAT_A, message_id=303)
    assert "Could not delete Mini App launch message" in caplog.text
    assert "delete denied" not in caplog.text


@pytest.mark.asyncio
async def test_multiple_tokens_delete_only_used_message_and_ignore_client_ids(temp_database):
    first = launch(message_id=401)
    second = launch(message_id=402)
    other_chat = launch(chat_id=CHAT_B, message_id=501)
    bot = SimpleNamespace(delete_message=AsyncMock())
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="", telegram_bot=bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        forged_body = await client.post("/api/v1/session", json={
            **payload(second), "chat_id": CHAT_B, "launch_message_id": 501,
        })
        assert forged_body.status_code == 422
        bot.delete_message.assert_not_awaited()
        accepted = await client.post(
            f"/api/v1/session?chat_id={CHAT_B}&launch_message_id=501",
            headers={"X-Chat-Id": str(CHAT_B), "X-Launch-Message-Id": "501"},
            json=payload(second),
        )
        assert accepted.status_code == 200
    bot.delete_message.assert_awaited_once_with(chat_id=CHAT_A, message_id=402)
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT token_digest, consumed_at FROM miniapp_launch_tokens",
        ).fetchall()
    consumed = {digest: timestamp for digest, timestamp in rows}
    assert consumed[hashlib.sha256(first.encode()).hexdigest()] is None
    assert consumed[hashlib.sha256(second.encode()).hexdigest()] is not None
    assert consumed[hashlib.sha256(other_chat.encode()).hexdigest()] is None
