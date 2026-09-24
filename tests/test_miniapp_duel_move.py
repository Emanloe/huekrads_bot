"""Mini App moves share Telegram's transactional persistent duel operations."""

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import database
from duel_session_repository import get_duel_session
from handlers import duel, duel_service
from handlers.persistent_duel_publisher import recover_persistent_duel_chat
from miniapp_api import create_miniapp_api
from tests.test_miniapp_api import CHAT_A, CHAT_B, register, session_for
from tests.test_miniapp_auth import TEST_BOT_TOKEN


class RngTrace:
    def __init__(self):
        self.trace = []

    def random(self):
        self.trace.append("random")
        return 0.5

    def choice(self, values):
        label = ("start" if values == [True, False] else
                 "timeout" if values == ["head", "body", "dick"] else "flavor")
        self.trace.append(f"choice:{label}")
        return values[0]


def client_for(fake_context):
    app = create_miniapp_api(
        bot_token=TEST_BOT_TOKEN, allowed_origin="",
        telegram_bot=fake_context.bot, job_queue=fake_context.job_queue,
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def started_duel(fake_context, monkeypatch, *, chat_id=CHAT_A):
    register(chat_id, 101, "attacker")
    register(chat_id, 202, "defender")
    rng = RngTrace()
    monkeypatch.setattr(duel_service, "random", rng)
    duel_id = duel_service.start_persistent_duel(chat_id, 101, 202).session["id"]
    await recover_persistent_duel_chat(
        chat_id, fake_context.bot, job_queue=fake_context.job_queue,
    )
    assert get_duel_session(chat_id, duel_id)["status"] == "active"
    return duel_id, rng


def outbox(chat_id):
    with database.get_db() as conn:
        return conn.execute(
            "SELECT kind, status, turn_id FROM duel_outbox WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        ).fetchall()


async def http_move(client, headers, duel_id, turn_id, zone="head", **extra):
    return await client.post(
        "/api/v1/duel/move", headers=headers,
        json={"duel_id": duel_id, "turn_id": turn_id, "zone": zone, **extra},
    )


async def telegram_move(fake_context, duel_id, turn_id, actor, action, zone="head"):
    query = SimpleNamespace(
        data=f"duel_{action}_{zone}_{duel_id}_{turn_id}",
        from_user=SimpleNamespace(id=actor), answer=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=CHAT_A))
    await duel.persistent_duel_action_callback(update, fake_context)
    return query


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ["head", "body", "dick"])
async def test_http_attack_uses_shared_service_and_keeps_defender_zone_hidden(
    temp_database, fake_context, monkeypatch, zone,
):
    duel_id, rng = await started_duel(fake_context, monkeypatch)
    register(CHAT_A, 303, "spectator")
    async with client_for(fake_context) as client:
        attacker = await session_for(client, CHAT_A, 101)
        defender = await session_for(client, CHAT_A, 202)
        spectator = await session_for(client, CHAT_A, 303)
        initial_attacker = (await client.get("/api/v1/duel/active", headers=attacker)).json()["duel"]
        initial_defender = (await client.get("/api/v1/duel/active", headers=defender)).json()["duel"]
        assert initial_attacker["can_act"] is True
        assert initial_defender["can_act"] is False
        assert initial_attacker["own_attack_accepted"] is False
        assert initial_defender["own_attack_accepted"] is False
        assert initial_attacker["attack_zone"] is initial_defender["attack_zone"] is None
        assert initial_attacker["turn_id"] == 1

        accepted = await http_move(client, attacker, duel_id, 1, zone)
        assert accepted.status_code == 200
        assert accepted.json() == {"accepted": True, "duel_id": duel_id, "turn_id": 1}
        state = get_duel_session(CHAT_A, duel_id)
        assert (state["status"], state["phase"], state["turn_id"], state["attack_zone"]) == (
            "active", "block", 2, zone,
        )
        visible_attacker = (await client.get("/api/v1/duel/active", headers=attacker)).json()["duel"]
        visible_defender = (await client.get("/api/v1/duel/active", headers=defender)).json()["duel"]
        visible_spectator = (await client.get("/api/v1/duel/active", headers=spectator)).json()["duel"]
        assert visible_attacker["attack_zone"] == zone
        assert visible_attacker["own_attack_accepted"] is True
        assert visible_defender["attack_zone"] is None
        assert visible_defender["own_attack_accepted"] is False
        assert visible_spectator["attack_zone"] is None
        assert visible_spectator["own_attack_accepted"] is False
        assert visible_spectator["can_act"] is False
        assert visible_attacker["can_act"] is False
        assert visible_defender["can_act"] is True
        assert visible_defender["turn_id"] == 2
        assert rng.trace == ["choice:start"]
        assert outbox(CHAT_A) == [
            ("attack_prompt", "delivered", 1), ("block_prompt", "delivered", 2),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("attack_ui,block_ui", [
    ("http", "telegram"), ("telegram", "http"),
    ("http", "http"), ("telegram", "telegram"),
])
async def test_mixed_interfaces_share_one_round_and_rng_trace(
    temp_database, fake_context, monkeypatch, attack_ui, block_ui,
):
    duel_id, rng = await started_duel(fake_context, monkeypatch)
    async with client_for(fake_context) as client:
        attacker = await session_for(client, CHAT_A, 101)
        defender = await session_for(client, CHAT_A, 202)
        if attack_ui == "http":
            assert (await http_move(client, attacker, duel_id, 1)).status_code == 200
        else:
            query = await telegram_move(fake_context, duel_id, 1, 101, "strike")
            query.answer.assert_awaited_once_with()
        assert get_duel_session(CHAT_A, duel_id)["phase"] == "block"
        assert (await client.get("/api/v1/duel/active", headers=attacker)).json()[
            "duel"]["own_attack_accepted"] is True

        if block_ui == "http":
            assert (await http_move(client, defender, duel_id, 2)).status_code == 200
        else:
            query = await telegram_move(fake_context, duel_id, 2, 202, "block")
            query.answer.assert_awaited_once_with()
        state = get_duel_session(CHAT_A, duel_id)
        assert (state["status"], state["phase"], state["turn_id"], state["round_no"]) == (
            "active", "attack", 3, 2,
        )
        assert state["attacker_user_id"] == 202
        assert (await client.get("/api/v1/duel/active", headers=defender)).json()["duel"]["can_act"] is True
        assert outbox(CHAT_A) == [
            ("attack_prompt", "delivered", 1),
            ("block_prompt", "delivered", 2),
            ("attack_prompt", "delivered", 3),
        ]
        assert rng.trace == [
            "choice:start", "random", "random", "choice:flavor", "choice:flavor",
        ]


@pytest.mark.asyncio
async def test_timeout_selected_attack_zone_is_not_a_user_accepted_action(
    temp_database, fake_context, monkeypatch,
):
    duel_id, rng = await started_duel(fake_context, monkeypatch)
    deadline = get_duel_session(CHAT_A, duel_id)["deadline_at"]
    timed_out = duel_service.resolve_persistent_duel_timeout(
        CHAT_A, duel_id, 1, deadline,
    )
    assert timed_out.reason == "success"
    assert timed_out.session["phase"] == "block"
    assert timed_out.session["attack_zone"] == "head"
    async with client_for(fake_context) as client:
        attacker = await session_for(client, CHAT_A, 101)
        pending = (await client.get("/api/v1/duel/active", headers=attacker)).json()["duel"]
        assert pending["attack_zone"] == "head"
        assert pending["own_attack_accepted"] is False
        await recover_persistent_duel_chat(
            CHAT_A, fake_context.bot, now_ms=deadline + 1,
            job_queue=fake_context.job_queue,
        )
        active = (await client.get("/api/v1/duel/active", headers=attacker)).json()["duel"]
        assert active["phase"] == "block"
        assert active["own_attack_accepted"] is False
    assert rng.trace == ["choice:start", "choice:timeout"]


@pytest.mark.asyncio
async def test_http_duplicate_stale_turn_and_previous_round_use_no_rng_or_outbox(
    temp_database, fake_context, monkeypatch,
):
    duel_id, rng = await started_duel(fake_context, monkeypatch)
    async with client_for(fake_context) as client:
        attacker = await session_for(client, CHAT_A, 101)
        defender = await session_for(client, CHAT_A, 202)
        assert (await http_move(client, attacker, duel_id, 1)).status_code == 200
        trace = list(rng.trace)
        before = outbox(CHAT_A)
        for headers, turn in ((attacker, 1), (defender, 1), (attacker, 999)):
            rejected = await http_move(client, headers, duel_id, turn)
            assert rejected.status_code == 409
            assert rejected.json()["detail"]["code"] == "stale_turn"
        assert outbox(CHAT_A) == before and rng.trace == trace
        assert (await http_move(client, defender, duel_id, 2)).status_code == 200
        after_round = outbox(CHAT_A)
        round_trace = list(rng.trace)
        assert (await http_move(client, defender, duel_id, 2)).status_code == 409
        assert (await http_move(client, attacker, duel_id, 1)).status_code == 409
        assert outbox(CHAT_A) == after_round and rng.trace == round_trace


@pytest.mark.asyncio
async def test_concurrent_http_and_telegram_http_races_accept_only_one(
    temp_database, fake_context, monkeypatch,
):
    duel_id, rng = await started_duel(fake_context, monkeypatch)
    async with client_for(fake_context) as client:
        attacker = await session_for(client, CHAT_A, 101)

        async def post():
            return await http_move(client, attacker, duel_id, 1)

        first, second = await asyncio.gather(post(), post())
        assert sorted((first.status_code, second.status_code)) == [200, 409]
        assert outbox(CHAT_A) == [
            ("attack_prompt", "delivered", 1), ("block_prompt", "delivered", 2),
        ]
        assert rng.trace == ["choice:start"]

    # A fresh duel in another chat tests the independent Telegram/HTTP adapter race.
    second_chat = CHAT_B
    register(second_chat, 101, "attacker_b")
    register(second_chat, 202, "defender_b")
    duel_b = duel_service.start_persistent_duel(second_chat, 101, 202).session["id"]
    await recover_persistent_duel_chat(second_chat, fake_context.bot, job_queue=fake_context.job_queue)
    query = SimpleNamespace(
        data=f"duel_strike_head_{duel_b}_1", from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=second_chat))
    async with client_for(fake_context) as client:
        attacker_b = await session_for(client, second_chat, 101)
        http_result, _ = await asyncio.gather(
            http_move(client, attacker_b, duel_b, 1),
            duel.persistent_duel_action_callback(update, fake_context),
        )
        accepted = int(http_result.status_code == 200) + int(query.answer.await_args.kwargs == {})
        assert accepted == 1
        assert get_duel_session(second_chat, duel_b)["turn_id"] == 2
        assert outbox(second_chat) == [
            ("attack_prompt", "delivered", 1), ("block_prompt", "delivered", 2),
        ]
        assert rng.trace == ["choice:start", "choice:start"]


@pytest.mark.asyncio
async def test_inaccessible_foreign_nonparticipant_and_forged_chat_are_rng_free(
    temp_database, fake_context, monkeypatch,
):
    duel_id, rng = await started_duel(fake_context, monkeypatch, chat_id=CHAT_B)
    register(CHAT_A, 101, "same_user_a")
    register(CHAT_A, 303, "spectator_a")
    register(CHAT_B, 303, "spectator_b")
    async with client_for(fake_context) as client:
        session_a = await session_for(client, CHAT_A, 101)
        unrelated_a = await session_for(client, CHAT_A, 303)
        spectator_b = await session_for(client, CHAT_B, 303)
        for headers in (session_a, unrelated_a, spectator_b):
            rejected = await client.post(
                f"/api/v1/duel/move?chat_id={CHAT_B}",
                headers={**headers, "X-Chat-Id": str(CHAT_B)},
                json={"duel_id": duel_id, "turn_id": 1, "zone": "head"},
            )
            assert rejected.status_code == 404
            assert rejected.json()["detail"]["code"] == "inaccessible_duel"
        forged = await http_move(client, session_a, duel_id, 1, chat_id=CHAT_B)
        assert forged.status_code == 422
        assert get_duel_session(CHAT_B, duel_id)["turn_id"] == 1
        assert outbox(CHAT_B) == [("attack_prompt", "delivered", 1)]
        assert rng.trace == ["choice:start"]


@pytest.mark.asyncio
async def test_move_validates_auth_schema_zone_role_and_finished_state(
    temp_database, fake_context, monkeypatch,
):
    duel_id, rng = await started_duel(fake_context, monkeypatch)
    register(CHAT_A, 303, "spectator")
    async with client_for(fake_context) as client:
        attacker = await session_for(client, CHAT_A, 101)
        defender = await session_for(client, CHAT_A, 202)
        assert (await http_move(client, defender, duel_id, 1)).json()["detail"]["code"] == "wrong_actor"
        for payload in (
            {}, {"turn_id": 1, "zone": "head"},
            {"duel_id": duel_id, "zone": "head"},
            {"duel_id": True, "turn_id": 1, "zone": "head"},
            {"duel_id": duel_id, "turn_id": False, "zone": "head"},
            {"duel_id": -1, "turn_id": 1, "zone": "head"},
            {"duel_id": duel_id, "turn_id": 0, "zone": "head"},
            {"duel_id": str(duel_id), "turn_id": 1, "zone": "head"},
            {"duel_id": duel_id, "turn_id": 1, "zone": "Голова"},
            {"duel_id": duel_id, "turn_id": 1, "zone": "leg"},
            {"duel_id": duel_id, "turn_id": 1, "zone": "head", "action": "attack"},
        ):
            response = await client.post("/api/v1/duel/move", headers=attacker, json=payload)
            assert response.status_code == 422
            if payload.get("zone") in ("Голова", "leg"):
                assert response.json()["detail"]["code"] == "invalid_zone"
        assert (await client.post(
            "/api/v1/duel/move", json={"duel_id": duel_id, "turn_id": 1, "zone": "head"},
        )).status_code == 401
        assert (await http_move(
            client, {"Authorization": "Bearer invalid"}, duel_id, 1,
        )).status_code == 401
        digest = hashlib.sha256(attacker["Authorization"][7:].encode()).hexdigest()
        with database.get_db() as conn:
            conn.execute(
                "UPDATE miniapp_sessions SET created_at = created_at - 7200, "
                "expires_at = expires_at - 7200 WHERE token_digest = ?", (digest,),
            )
        assert (await http_move(client, attacker, duel_id, 1)).status_code == 401
        assert get_duel_session(CHAT_A, duel_id)["turn_id"] == 1
        assert outbox(CHAT_A) == [("attack_prompt", "delivered", 1)]
        assert rng.trace == ["choice:start"]


@pytest.mark.asyncio
async def test_terminal_http_block_uses_existing_finalization_and_finished_duel_rejects_moves(
    temp_database, fake_context, monkeypatch,
):
    duel_id, rng = await started_duel(fake_context, monkeypatch)
    async with client_for(fake_context) as client:
        attacker = await session_for(client, CHAT_A, 101)
        defender = await session_for(client, CHAT_A, 202)
        assert (await http_move(client, attacker, duel_id, 1, "head")).status_code == 200
        assert (await http_move(client, defender, duel_id, 2, "body")).status_code == 200
        finished = get_duel_session(CHAT_A, duel_id)
        assert finished["status"] == "finished"
        assert finished["result"]["kind"] == "finalized"
        finished_view = (await client.get("/api/v1/duel/active", headers=attacker)).json()
        assert finished_view["duel"] is None
        assert finished_view["recent_finished"]["id"] == duel_id
        assert [kind for kind, _, _ in outbox(CHAT_A)] == [
            "attack_prompt", "block_prompt", "final_result",
        ]
        trace = list(rng.trace)
        result = await http_move(client, defender, duel_id, finished["turn_id"], "body")
        assert result.status_code == 409
        assert result.json()["detail"]["code"] == "not_active"
        assert rng.trace == trace
        assert database.get_monthly_chat_stats(
            CHAT_A, database.moscow_month_key(),
        )["duels"] == 1


@pytest.mark.asyncio
async def test_http_move_deadline_and_worker_race_are_authoritative(
    temp_database, fake_context, monkeypatch,
):
    duel_id, rng = await started_duel(fake_context, monkeypatch)
    state = get_duel_session(CHAT_A, duel_id)
    deadline = state["deadline_at"]
    async with client_for(fake_context) as client:
        attacker = await session_for(client, CHAT_A, 101)
        monkeypatch.setattr(duel_service, "utc_unix_milliseconds", lambda: deadline)
        import miniapp_api
        monkeypatch.setattr(miniapp_api, "utc_unix_milliseconds", lambda: deadline)
        active = (await client.get("/api/v1/duel/active", headers=attacker)).json()["duel"]
        assert active["can_act"] is False
        expired = await http_move(client, attacker, duel_id, 1)
        assert expired.status_code == 409
        assert expired.json()["detail"]["code"] == "turn_expired"
        assert rng.trace == ["choice:start"]
        assert get_duel_session(CHAT_A, duel_id)["turn_id"] == 1

        raced, _ = await asyncio.gather(
            http_move(client, attacker, duel_id, 1),
            recover_persistent_duel_chat(
                CHAT_A, fake_context.bot, now_ms=deadline,
                job_queue=fake_context.job_queue,
            ),
        )
        assert raced.status_code == 409
        assert raced.json()["detail"]["code"] in ("turn_expired", "stale_turn")
        assert get_duel_session(CHAT_A, duel_id)["turn_id"] == 2
        assert outbox(CHAT_A) == [
            ("attack_prompt", "delivered", 1), ("block_prompt", "delivered", 2),
        ]
        assert rng.trace == ["choice:start", "choice:timeout"]


@pytest.mark.asyncio
async def test_move_commit_survives_telegram_send_failure_and_retries(
    temp_database, fake_context, monkeypatch,
):
    duel_id, rng = await started_duel(fake_context, monkeypatch)
    fake_context.bot.edit_message_text.side_effect = RuntimeError("edit unavailable")
    fake_context.bot.send_message.side_effect = RuntimeError("send unavailable")
    async with client_for(fake_context) as client:
        attacker = await session_for(client, CHAT_A, 101)
        response = await http_move(client, attacker, duel_id, 1)
        assert response.status_code == 200
        assert get_duel_session(CHAT_A, duel_id)["status"] == "publishing"
        assert outbox(CHAT_A) == [
            ("attack_prompt", "delivered", 1), ("block_prompt", "pending", 2),
        ]
        assert rng.trace == ["choice:start"]
        fake_context.bot.edit_message_text.side_effect = None
        fake_context.bot.send_message.side_effect = None
        await recover_persistent_duel_chat(
            CHAT_A, fake_context.bot, job_queue=fake_context.job_queue,
        )
        assert get_duel_session(CHAT_A, duel_id)["status"] == "active"
        assert outbox(CHAT_A)[1] == ("block_prompt", "delivered", 2)
        assert rng.trace == ["choice:start"]
