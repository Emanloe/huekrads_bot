"""Mini App challenges use the ordinary persistent service and chat-scoped session."""

import asyncio
import hashlib
from unittest.mock import Mock

import httpx
import pytest

import database
from duel_session_repository import get_current_duel_session
from handlers import duel_service
from handlers.persistent_duel_publisher import recover_persistent_duel_chat
from miniapp_api import create_miniapp_api
from tests.test_miniapp_api import CHAT_A, CHAT_B, register, session_for
from tests.test_miniapp_auth import TEST_BOT_TOKEN


def client_for(bot=None, job_queue=None):
    app = create_miniapp_api(
        bot_token=TEST_BOT_TOKEN, allowed_origin="",
        telegram_bot=bot, job_queue=job_queue,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def session_count(chat_id):
    with database.get_db() as conn:
        return conn.execute(
            "SELECT count(*) FROM duel_sessions WHERE chat_id = ?", (chat_id,),
        ).fetchone()[0]


@pytest.mark.asyncio
async def test_same_chat_start_publishes_and_is_visible_in_active_read(
    temp_database, fake_context, monkeypatch,
):
    register(CHAT_A, 101, "hero")
    register(CHAT_A, 202, "opponent")
    choice = Mock(return_value=True)
    monkeypatch.setattr(duel_service.random, "choice", choice)
    async with client_for(fake_context.bot, fake_context.job_queue) as client:
        headers = await session_for(client, CHAT_A, 101)
        listed = await client.get("/api/v1/duel/opponents", headers=headers)
        assert [p["user_id"] for p in listed.json()["opponents"]] == [202]
        assert choice.call_count == 0

        started = await client.post(
            "/api/v1/duel/start", headers=headers, json={"opponent_user_id": 202},
        )
        assert started.status_code == 201
        duel = get_current_duel_session(CHAT_A)
        assert started.json() == {"duel_id": duel["id"]}
        assert {duel["player1_user_id"], duel["player2_user_id"]} == {101, 202}
        assert duel["status"] == "active"
        active = (await client.get("/api/v1/duel/active", headers=headers)).json()["duel"]
        assert active["id"] == duel["id"]
        assert active["phase"] == "attack"
        assert choice.call_count == 1
        assert fake_context.bot.send_message.await_count == 1
        assert fake_context.bot.send_message.await_args.kwargs["chat_id"] == CHAT_A
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT kind, status FROM duel_outbox WHERE chat_id = ?", (CHAT_A,),
            ).fetchall() == [("attack_prompt", "delivered")]


@pytest.mark.asyncio
async def test_cross_chat_target_and_client_chat_override_cannot_cross_world(
    temp_database, monkeypatch,
):
    register(CHAT_A, 101, "hero_a")
    register(CHAT_B, 101, "hero_b")
    register(CHAT_B, 303, "only_b")
    register(CHAT_A, 202, "only_a")
    choice = Mock(return_value=True)
    monkeypatch.setattr(duel_service.random, "choice", choice)
    async with client_for() as client:
        headers_a = await session_for(client, CHAT_A, 101)
        headers_b = await session_for(client, CHAT_B, 101)
        foreign = await client.post(
            f"/api/v1/duel/start?chat_id={CHAT_B}",
            headers={**headers_a, "X-Chat-Id": str(CHAT_B)},
            json={"opponent_user_id": 303},
        )
        assert foreign.status_code == 404
        assert foreign.json()["detail"]["code"] == "opponent_not_registered"
        injected = await client.post(
            "/api/v1/duel/start", headers=headers_a,
            json={"opponent_user_id": 303, "chat_id": CHAT_B},
        )
        assert injected.status_code == 422
        assert session_count(CHAT_A) == session_count(CHAT_B) == 0
        assert choice.call_count == 0
        valid = await client.post(
            f"/api/v1/duel/start?chat_id={CHAT_A}", headers=headers_b,
            json={"opponent_user_id": 303},
        )
        assert valid.status_code == 201
        assert get_current_duel_session(CHAT_A) is None
        assert get_current_duel_session(CHAT_B)["id"] == valid.json()["duel_id"]
        assert (await client.get("/api/v1/duel/active", headers=headers_a)).json() == {"duel": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked,code,status", [
    ("self", "self_target", 409),
    ("challenger", "initiator_no_dick", 403),
    ("target", "opponent_no_dick", 403),
])
async def test_expected_rejections_use_no_rng(
    temp_database, monkeypatch, blocked, code, status,
):
    register(CHAT_A, 101, "hero")
    register(CHAT_A, 202, "opponent")
    if blocked in ("challenger", "target"):
        with database.get_db() as conn:
            conn.execute(
                "UPDATE duel_users SET dick_stolen_today = 1 WHERE chat_id = ? AND user_id = ?",
                (CHAT_A, 101 if blocked == "challenger" else 202),
            )
    choice = Mock(side_effect=AssertionError("rejected start used RNG"))
    monkeypatch.setattr(duel_service.random, "choice", choice)
    async with client_for() as client:
        headers = await session_for(client, CHAT_A, 101)
        response = await client.post(
            "/api/v1/duel/start", headers=headers,
            json={"opponent_user_id": 101 if blocked == "self" else 202},
        )
        assert response.status_code == status
        assert response.json()["detail"]["code"] == code
        assert session_count(CHAT_A) == 0
        choice.assert_not_called()


@pytest.mark.asyncio
async def test_zero_point_users_can_start_and_existing_slot_blocks_duplicate(
    temp_database, monkeypatch,
):
    register(CHAT_A, 101, "hero")
    register(CHAT_A, 202, "opponent")
    with database.get_db() as conn:
        conn.execute(
            "UPDATE duel_users SET points = 0 WHERE chat_id = ? AND user_id IN (101, 202)",
            (CHAT_A,),
        )
    choice = Mock(return_value=True)
    monkeypatch.setattr(duel_service.random, "choice", choice)
    async with client_for() as client:
        headers = await session_for(client, CHAT_A, 101)
        response = await client.post(
            "/api/v1/duel/start", headers=headers, json={"opponent_user_id": 202},
        )
        assert response.status_code == 201
        duel = get_current_duel_session(CHAT_A)
        assert duel["player1_snapshot"]["points"] == 0
        assert duel["player2_snapshot"]["points"] == 0
        rejected = await client.post(
            "/api/v1/duel/start", headers=headers, json={"opponent_user_id": 202},
        )
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "active_duel"
        assert session_count(CHAT_A) == 1
        assert choice.call_count == 1


@pytest.mark.asyncio
async def test_concurrent_double_http_start_creates_one_session(temp_database, monkeypatch):
    register(CHAT_A, 101, "hero")
    register(CHAT_A, 202, "opponent")
    choice = Mock(return_value=True)
    monkeypatch.setattr(duel_service.random, "choice", choice)
    async with client_for() as client:
        headers = await session_for(client, CHAT_A, 101)
        async def start():
            return await client.post(
                "/api/v1/duel/start", headers=headers, json={"opponent_user_id": 202},
            )
        responses = await asyncio.gather(start(), start())
        assert sorted(response.status_code for response in responses) == [201, 409]
        assert session_count(CHAT_A) == 1
        assert choice.call_count == 1
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT count(*) FROM duel_outbox WHERE chat_id = ?", (CHAT_A,),
            ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_start_requires_live_session(temp_database):
    register(CHAT_A, 101, "hero")
    register(CHAT_A, 202, "opponent")
    async with client_for() as client:
        payload = {"opponent_user_id": 202}
        assert (await client.post("/api/v1/duel/start", json=payload)).status_code == 401
        assert (await client.post(
            "/api/v1/duel/start", headers={"Authorization": "Bearer invalid"}, json=payload,
        )).status_code == 401
        headers = await session_for(client, CHAT_A, 101)
        digest = hashlib.sha256(headers["Authorization"][7:].encode()).hexdigest()
        with database.get_db() as conn:
            conn.execute(
                "UPDATE miniapp_sessions SET created_at = created_at - 7200, "
                "expires_at = expires_at - 7200 WHERE token_digest = ?", (digest,),
            )
        assert (await client.post(
            "/api/v1/duel/start", headers=headers, json=payload,
        )).status_code == 401
        assert session_count(CHAT_A) == 0


@pytest.mark.asyncio
async def test_telegram_send_failure_keeps_committed_challenge_retryable(
    temp_database, fake_context, monkeypatch,
):
    register(CHAT_A, 101, "hero")
    register(CHAT_A, 202, "opponent")
    choice = Mock(return_value=True)
    monkeypatch.setattr(duel_service.random, "choice", choice)
    fake_context.bot.send_message.side_effect = RuntimeError("Telegram temporarily unavailable")
    async with client_for(fake_context.bot, fake_context.job_queue) as client:
        headers = await session_for(client, CHAT_A, 101)
        started = await client.post(
            "/api/v1/duel/start", headers=headers, json={"opponent_user_id": 202},
        )
        assert started.status_code == 201
        assert get_current_duel_session(CHAT_A)["status"] == "publishing"
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT status FROM duel_outbox WHERE chat_id = ?", (CHAT_A,),
            ).fetchone() == ("pending",)
        fake_context.bot.send_message.side_effect = None
        await recover_persistent_duel_chat(
            CHAT_A, fake_context.bot, job_queue=fake_context.job_queue,
        )
        assert get_current_duel_session(CHAT_A)["status"] == "active"
        assert session_count(CHAT_A) == 1
        assert choice.call_count == 1
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT status FROM duel_outbox WHERE chat_id = ?", (CHAT_A,),
            ).fetchone() == ("delivered",)
