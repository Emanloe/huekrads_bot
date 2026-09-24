"""Mini App reads durable, participant-scoped ordinary duel outcomes."""

import json
from types import SimpleNamespace

import httpx
import pytest

import database
from duel_outbox_repository import list_persisted_duel_round_resolutions
from duel_session_repository import get_duel_session
from handlers import duel_service
from handlers.duel_items import get_duel_item_name
from handlers.duel_text import ATTACK_PHRASES, MISS_PHRASES
from handlers.persistent_duel_publisher import recover_persistent_duel_chat
from miniapp_api import _plain_duel_text, create_miniapp_api
from tests.test_miniapp_api import CHAT_A, CHAT_B, register, session_for
from tests.test_miniapp_auth import TEST_BOT_TOKEN
from tests.test_miniapp_duel_move import client_for, http_move, started_duel, telegram_move


class ScriptedRng:
    def __init__(self, rolls):
        self.rolls = iter(rolls)
        self.trace = []

    def random(self):
        value = next(self.rolls)
        self.trace.append(("random", value))
        return value

    def choice(self, values):
        self.trace.append(("choice", len(values)))
        return values[0]


def no_game_rng(*_args, **_kwargs):
    raise AssertionError("A result read used gameplay RNG")


@pytest.mark.asyncio
async def test_resolved_block_survives_next_attack_restart_and_uses_zero_read_rng(
    temp_database, fake_context, monkeypatch,
):
    duel_id, rng = await started_duel(fake_context, monkeypatch)
    register(CHAT_A, 303, "spectator")
    async with client_for(fake_context) as client:
        attacker = await session_for(client, CHAT_A, 101)
        defender = await session_for(client, CHAT_A, 202)
        spectator = await session_for(client, CHAT_A, 303)
        assert (await http_move(client, attacker, duel_id, 1, "head")).status_code == 200
        before = (await client.get("/api/v1/duel/active", headers=defender)).json()["duel"]
        assert before["attack_zone"] is None and before["rounds"] == []
        assert (await http_move(client, defender, duel_id, 2, "head")).status_code == 200
        resolved = (await client.get("/api/v1/duel/active", headers=attacker)).json()["duel"]
        assert resolved["round"] == 2 and resolved["turn_id"] == 3
        assert len(resolved["rounds"]) == 1
        round_one = resolved["rounds"][0]
        assert (round_one["round"], round_one["resolved_turn_id"], round_one["outcome"]) == (1, 2, "block")
        assert (round_one["attack_zone"], round_one["defense_zone"]) == ("head", "head")
        assert round_one["attacker"]["user_id"] == 101
        assert round_one["defender"]["user_id"] == 202
        persisted = list_persisted_duel_round_resolutions(CHAT_A, duel_id)[0]
        assert round_one["presentation_text"] == _plain_duel_text(
            persisted["presentation_html"]
        )
        with database.get_db() as conn:
            payload_json = conn.execute(
                "SELECT payload_json FROM duel_outbox WHERE chat_id = ? AND duel_id = ? "
                "AND kind = 'attack_prompt' AND turn_id = 3",
                (CHAT_A, duel_id),
            ).fetchone()[0]
        payload = json.loads(payload_json)
        assert payload["round_resolution"] == persisted
        assert persisted["presentation_html"] in payload["text"]
        assert (await client.get("/api/v1/duel/active", headers=defender)).json()["duel"]["rounds"] == [round_one]
        assert (await client.get("/api/v1/duel/active", headers=spectator)).json()["duel"]["rounds"] == []

        # Polling can skip an entire round. Both checkpoints remain available.
        assert (await http_move(client, defender, duel_id, 3, "body")).status_code == 200
        assert (await http_move(client, attacker, duel_id, 4, "body")).status_code == 200
        assert (await http_move(client, attacker, duel_id, 5, "dick")).status_code == 200
        assert get_duel_session(CHAT_A, duel_id)["result"] is None
        assert len(list_persisted_duel_round_resolutions(CHAT_A, duel_id)) == 2
        trace = list(rng.trace)
        monkeypatch.setattr(duel_service, "random", SimpleNamespace(
            random=no_game_rng, choice=no_game_rng,
        ))
        reopened = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=reopened),
                                     base_url="http://test") as restarted:
            fresh_session = await session_for(restarted, CHAT_A, 202)
            after = (await restarted.get("/api/v1/duel/active", headers=fresh_session)).json()["duel"]
            assert after["phase"] == "block"
            assert [round_result["round"] for round_result in after["rounds"]] == [1, 2]
            assert after["rounds"][0] == round_one
            assert after["rounds"][1]["outcome"] == "block"
            assert after["attack_zone"] is None
        assert rng.trace == trace


@pytest.mark.asyncio
@pytest.mark.parametrize("attack_ui,block_ui", [
    ("http", "telegram"), ("telegram", "http"),
])
async def test_miss_round_from_mixed_interfaces_is_persisted_without_extra_rng(
    temp_database, fake_context, monkeypatch, attack_ui, block_ui,
):
    register(CHAT_A, 101, "attacker")
    register(CHAT_A, 202, "defender")
    rng = ScriptedRng([0.5, 0.01])
    monkeypatch.setattr(duel_service, "random", rng)
    duel_id = duel_service.start_persistent_duel(CHAT_A, 101, 202).session["id"]
    await recover_persistent_duel_chat(CHAT_A, fake_context.bot, job_queue=fake_context.job_queue)
    async with client_for(fake_context) as client:
        attacker = await session_for(client, CHAT_A, 101)
        defender = await session_for(client, CHAT_A, 202)
        if attack_ui == "http":
            assert (await http_move(client, attacker, duel_id, 1, "dick")).status_code == 200
        else:
            query = await telegram_move(fake_context, duel_id, 1, 101, "strike", "dick")
            query.answer.assert_awaited_once_with()
        if block_ui == "http":
            assert (await http_move(client, defender, duel_id, 2, "body")).status_code == 200
        else:
            query = await telegram_move(fake_context, duel_id, 2, 202, "block", "body")
            query.answer.assert_awaited_once_with()
        view = (await client.get("/api/v1/duel/active", headers=defender)).json()["duel"]
        assert view["round"] == 2
        assert view["rounds"][0]["outcome"] == "miss"
        assert (view["rounds"][0]["attack_zone"], view["rounds"][0]["defense_zone"]) == (
            "dick", "body",
        )
        assert isinstance(view["rounds"][0]["outcome_text"], str)
        persisted = list_persisted_duel_round_resolutions(CHAT_A, duel_id)[0]
        assert view["rounds"][0]["presentation_text"] == _plain_duel_text(
            persisted["presentation_html"]
        )
        assert persisted["attack_phrase"] in persisted["presentation_html"]
        assert persisted["outcome_phrase"] in persisted["presentation_html"]
        assert rng.trace == [
            ("choice", 2), ("random", 0.5), ("random", 0.01),
            ("choice", len(MISS_PHRASES)),
            ("choice", len(ATTACK_PHRASES)),
        ]


@pytest.mark.asyncio
async def test_finished_result_is_authoritative_for_both_players_after_restart(
    temp_database, fake_context, monkeypatch,
):
    duel_id, rng = await started_duel(fake_context, monkeypatch)
    async with client_for(fake_context) as client:
        attacker = await session_for(client, CHAT_A, 101)
        defender = await session_for(client, CHAT_A, 202)
        assert (await http_move(client, attacker, duel_id, 1, "head")).status_code == 200
        assert (await http_move(client, defender, duel_id, 2, "body")).status_code == 200
        stored = get_duel_session(CHAT_A, duel_id)["result"]
        assert stored["kind"] == "finalized"
        assert stored["terminal_resolution"]["presentation_html"] == stored["custom_text"]
        first = (await client.get("/api/v1/duel/active", headers=attacker)).json()
        second = (await client.get("/api/v1/duel/active", headers=defender)).json()
        assert first == second
        assert first["duel"] is None
        finished = first["recent_finished"]
        assert finished["status"] == "finished" and finished["id"] == duel_id
        assert finished["winner"]["user_id"] == stored["winner_user_id"] == 101
        assert finished["loser"]["user_id"] == stored["loser_user_id"] == 202
        assert finished["points"] == {
            "winner_before": 20, "winner_after": stored["winner_points"],
            "winner_delta": stored["winner_points"] - 20,
            "winner_delta_awarded": 10,
            "loser_before": 20, "loser_after": stored["loser_points"],
            "loser_delta": stored["loser_points"] - 20,
            "loser_delta_awarded": -5,
        }
        assert finished["dick_stolen"] is stored["is_dick_stolen"]
        assert finished["rounds"][0]["outcome"] == "hit"
        assert finished["rounds"][0]["attack_zone"] == "head"
        assert finished["rounds"][0]["defense_zone"] == "body"
        assert finished["rounds"][0]["presentation_text"] == _plain_duel_text(
            stored["custom_text"]
        )
        assert "final_text" not in finished and "result_json" not in finished
        trace = list(rng.trace)
        monkeypatch.setattr(duel_service, "random", SimpleNamespace(
            random=no_game_rng, choice=no_game_rng,
        ))
        reopened = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=reopened),
                                     base_url="http://test") as restarted:
            fresh_session = await session_for(restarted, CHAT_A, 101)
            assert (await restarted.get("/api/v1/duel/active", headers=fresh_session)).json() == first
        assert rng.trace == trace


@pytest.mark.asyncio
async def test_finished_read_model_shows_persisted_zero_point_steal_and_item_name(
    temp_database, fake_context, monkeypatch,
):
    register(CHAT_A, 101, "winner")
    register(CHAT_A, 202, "loser")
    with database.get_db() as conn:
        conn.execute(
            "UPDATE duel_users SET points = 0 WHERE chat_id = ? AND user_id = ?",
            (CHAT_A, 202),
        )
    database.add_duel_inventory_item(CHAT_A, 202, "ceremonial_bolt")
    rng = ScriptedRng([0.5, 0.5, 0.01, 0.0, 0.5])
    monkeypatch.setattr(duel_service, "random", rng)
    duel_id = duel_service.start_persistent_duel(CHAT_A, 101, 202).session["id"]
    await recover_persistent_duel_chat(CHAT_A, fake_context.bot, job_queue=fake_context.job_queue)
    async with client_for(fake_context) as client:
        winner = await session_for(client, CHAT_A, 101)
        loser = await session_for(client, CHAT_A, 202)
        assert (await http_move(client, winner, duel_id, 1, "head")).status_code == 200
        assert (await http_move(client, loser, duel_id, 2, "body")).status_code == 200
        stored = get_duel_session(CHAT_A, duel_id)["result"]
        assert stored["is_dick_stolen"] is True
        assert stored["stolen_item"]["item_id"] == "ceremonial_bolt"
        view = (await client.get("/api/v1/duel/active", headers=loser)).json()["recent_finished"]
        assert view["dick_stolen"] is True
        assert view["points"]["loser_before"] == view["points"]["loser_after"] == 0
        assert view["stolen_item"] == {
            "item_id": "ceremonial_bolt", "name": get_duel_item_name("ceremonial_bolt"),
        }
        assert view["berserk"]["berserker"]["user_id"] == 101
        assert view["berserk"]["victim"]["user_id"] == 202
        assert view["berserk"]["dick_lost"] is False
        assert [call for call in rng.trace if call[0] == "random"] == [
            ("random", 0.5), ("random", 0.5), ("random", 0.01),
            ("random", 0.0), ("random", 0.5),
        ]


@pytest.mark.asyncio
async def test_timeout_resolution_is_read_from_server_not_client_clock(
    temp_database, fake_context, monkeypatch,
):
    duel_id, rng = await started_duel(fake_context, monkeypatch)
    async with client_for(fake_context) as client:
        attacker = await session_for(client, CHAT_A, 101)
        defender = await session_for(client, CHAT_A, 202)
        assert (await http_move(client, attacker, duel_id, 1, "body")).status_code == 200
        before = (await client.get("/api/v1/duel/active", headers=defender)).json()["duel"]
        assert before["rounds"] == []
        deadline = get_duel_session(CHAT_A, duel_id)["deadline_at"]
        await recover_persistent_duel_chat(
            CHAT_A, fake_context.bot, now_ms=deadline, job_queue=fake_context.job_queue,
        )
        after = (await client.get("/api/v1/duel/active", headers=defender)).json()
        assert after["duel"] is None
        assert after["recent_finished"]["rounds"][0]["outcome"] == "hit"
        assert (after["recent_finished"]["rounds"][0]["attack_zone"],
                after["recent_finished"]["rounds"][0]["defense_zone"]) == ("body", "head")
        assert [player["user_id"] for player in after["recent_finished"]["rounds"][0]["timed_out"]] == [202]
        timeout_texts = after["recent_finished"]["rounds"][0]["timeout_texts"]
        assert len(timeout_texts) == 1 and "зазевался" in timeout_texts[0]
        assert "Время вышло:" not in timeout_texts[0]
        assert rng.trace.count("choice:timeout") == 1


@pytest.mark.asyncio
async def test_attack_timeout_is_recorded_in_resolved_round_without_new_rng(
    temp_database, fake_context, monkeypatch,
):
    duel_id, rng = await started_duel(fake_context, monkeypatch)
    async with client_for(fake_context) as client:
        defender = await session_for(client, CHAT_A, 202)
        deadline = get_duel_session(CHAT_A, duel_id)["deadline_at"]
        await recover_persistent_duel_chat(
            CHAT_A, fake_context.bot, now_ms=deadline, job_queue=fake_context.job_queue,
        )
        before = (await client.get("/api/v1/duel/active", headers=defender)).json()["duel"]
        assert before["phase"] == "block" and before["attack_zone"] is None
        assert before["rounds"] == []
        assert (await http_move(client, defender, duel_id, 2, "head")).status_code == 200
        after = (await client.get("/api/v1/duel/active", headers=defender)).json()["duel"]
        assert after["rounds"][0]["outcome"] == "block"
        assert [player["user_id"] for player in after["rounds"][0]["timed_out"]] == [101]
        assert len(after["rounds"][0]["timeout_texts"]) == 1
        assert "зазевался" in after["rounds"][0]["timeout_texts"][0]
        assert rng.trace.count("choice:timeout") == 1


@pytest.mark.asyncio
async def test_round_result_survives_prompt_send_failure_and_retry_without_duplicate(
    temp_database, fake_context, monkeypatch,
):
    duel_id, rng = await started_duel(fake_context, monkeypatch)
    async with client_for(fake_context) as client:
        attacker = await session_for(client, CHAT_A, 101)
        defender = await session_for(client, CHAT_A, 202)
        assert (await http_move(client, attacker, duel_id, 1, "head")).status_code == 200
        fake_context.bot.edit_message_text.side_effect = RuntimeError("edit unavailable")
        fake_context.bot.send_message.side_effect = RuntimeError("send unavailable")
        assert (await http_move(client, defender, duel_id, 2, "head")).status_code == 200
        assert get_duel_session(CHAT_A, duel_id)["status"] == "publishing"
        pending = (await client.get("/api/v1/duel/active", headers=defender)).json()["duel"]
        assert len(pending["rounds"]) == 1
        assert pending["rounds"][0]["outcome"] == "block"
        trace = list(rng.trace)
        fake_context.bot.edit_message_text.side_effect = None
        fake_context.bot.send_message.side_effect = None
        await recover_persistent_duel_chat(
            CHAT_A, fake_context.bot, job_queue=fake_context.job_queue,
        )
        delivered = (await client.get("/api/v1/duel/active", headers=defender)).json()["duel"]
        assert delivered["status"] == "active"
        assert delivered["rounds"] == pending["rounds"]
        assert rng.trace == trace


@pytest.mark.asyncio
async def test_finished_result_is_scoped_to_participant_and_chat_even_with_forged_ids(
    temp_database, fake_context, monkeypatch,
):
    duel_id, _ = await started_duel(fake_context, monkeypatch, chat_id=CHAT_B)
    register(CHAT_A, 101, "same_user_other_chat")
    register(CHAT_B, 303, "spectator")
    async with client_for(fake_context) as client:
        attacker_b = await session_for(client, CHAT_B, 101)
        defender_b = await session_for(client, CHAT_B, 202)
        assert (await http_move(client, attacker_b, duel_id, 1, "head")).status_code == 200
        assert (await http_move(client, defender_b, duel_id, 2, "body")).status_code == 200
        session_a = await session_for(client, CHAT_A, 101)
        spectator_b = await session_for(client, CHAT_B, 303)
        for headers in (session_a, spectator_b):
            result = await client.get(
                f"/api/v1/duel/active?chat_id={CHAT_B}&duel_id={duel_id}",
                headers={**headers, "X-Chat-Id": str(CHAT_B), "X-Duel-Id": str(duel_id)},
            )
            assert result.json() == {"duel": None, "recent_finished": None}
        assert (await client.get(f"/api/v1/duel/{duel_id}/result", headers=session_a)).status_code == 404
        assert (await client.get("/api/v1/duel/active", headers=attacker_b)).json()[
            "recent_finished"
        ]["id"] == duel_id
