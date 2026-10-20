"""Mini App chronicle shares the Telegram read model and session boundary."""

from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import database as db
from duel_session_repository import create_duel_session
from handlers import hyperborean_event
from handlers.recap import (
    build_recap_model, build_recap_text, game_day_bounds_ms, load_recap_events, recap_command,
)
from miniapp_api import create_miniapp_api
from tests.test_miniapp_api import CHAT_A, CHAT_B, register, session_for, snapshot
from tests.test_miniapp_auth import TEST_BOT_TOKEN


def duel_data(initiator, opponent, winner, name="<b>Соперник</b>"):
    return {
        "initiator_user_id": initiator,
        "initiator_name": "Герой" if initiator == 101 else name,
        "opponent_user_id": opponent,
        "opponent_name": "Герой" if opponent == 101 else name,
        "winner_user_id": winner,
        "winner_name": "Герой" if winner == 101 else name,
        "loser_user_id": opponent if winner == initiator else initiator,
        "loser_name": "Герой" if winner != 101 else name,
        "winner_points_delta": 10, "loser_points_delta": -5,
        "rounds": 3, "is_dick_stolen": False, "stolen_item": None,
    }


@pytest.mark.asyncio
async def test_recap_api_matches_telegram_and_is_scoped_to_day_chat_and_user(temp_database):
    register(CHAT_A, 101, "hero")
    register(CHAT_A, 202, "opponent")
    register(CHAT_A, 303, "other")
    register(CHAT_B, 101, "hero_elsewhere")
    start, end = game_day_bounds_ms()
    for index, (initiator, opponent, winner) in enumerate((
        (101, 202, 101), (202, 101, 202), (202, 101, 101), (101, 303, 303),
    )):
        db.record_game_event(CHAT_A, 101, "duel_finished",
                             duel_data(initiator, opponent, winner),
                             source_id=f"duel:{index + 1}", occurred_at_ms=start + 100 + index)
    db.record_game_event(CHAT_A, 101, "dick_stolen", {}, occurred_at_ms=start + 200)
    db.record_game_event(CHAT_A, 101, "item_received",
                         {"item_id": "knife", "method": "claim"}, occurred_at_ms=start + 201)
    db.record_game_event(CHAT_A, 101, "huecrab_tamed", {}, occurred_at_ms=start + 202)
    db.record_game_event(CHAT_A, 101, "duel_finished", duel_data(101, 202, 101),
                         occurred_at_ms=start - 1)
    db.record_game_event(CHAT_A, 101, "duel_finished", duel_data(101, 202, 101),
                         occurred_at_ms=end)
    db.record_game_event(CHAT_A, 202, "huecrab_tamed", {}, occurred_at_ms=start + 203)
    db.record_game_event(CHAT_B, 101, "huecrab_tamed", {}, occurred_at_ms=start + 204)

    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        assert (await client.get("/api/v1/recap")).status_code == 401
        headers = await session_for(client, CHAT_A, 101)
        response = await client.get("/api/v1/recap", headers=headers,
                                    params={"chat_id": CHAT_B, "user_id": 202})
        other = await client.get("/api/v1/recap",
                                 headers=await session_for(client, CHAT_B, 101))
        absent = await client.get("/api/v1/recap",
                                  headers=await session_for(client, CHAT_A, 404))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    model = response.json()
    assert model["empty"] is False
    assert [(row["user_id"], row["incoming"], row["outgoing"], row["wins"], row["losses"])
            for row in model["duels"]] == [(202, 2, 1, 2, 1), (303, 0, 1, 0, 1)]
    assert model["totals"] == {"wins": 2, "losses": 2, "found": 0, "received": 1, "lost": 0}
    assert sum(row["wins"] for row in model["duels"]) == model["totals"]["wins"]
    assert sum(row["losses"] for row in model["duels"]) == model["totals"]["losses"]
    assert model["duels"][0]["name"] == "<b>Соперник</b>"
    assert model["thefts"] and model["loot"] and model["pets"]
    telegram_text = build_recap_text(load_recap_events(CHAT_A, 101), 101)
    assert "&lt;b&gt;Соперник&lt;/b&gt;" in telegram_text
    assert "Твои победы: 2 · Победы соперника: 1" in telegram_text
    for section in ("thefts", "loot", "pets"):
        assert all(escape(line) in telegram_text for line in model[section])
    assert "winner_points_delta" not in str(model)
    assert "rounds" not in str(model)
    assert other.json()["duels"] == []
    assert other.json()["pets"]
    assert absent.status_code == 404


@pytest.mark.asyncio
async def test_recap_api_uses_native_duels_without_double_counting_journal(temp_database):
    hero = register(CHAT_A, 101, "hero")
    rival = register(CHAT_A, 202, "rival")
    start, _ = game_day_bounds_ms()
    native = create_duel_session(
        CHAT_A, 101, 202, snapshot(hero), snapshot(rival), 101, 202,
        status="finished", phase="block", attack_zone="head", result={
            "kind": "finalized", "winner_user_id": 202, "loser_user_id": 101,
        }, finished_at=start + 1000, now_ms=start + 900,
    )
    db.record_game_event(CHAT_A, 101, "duel_finished", duel_data(101, 202, 202),
                         source_id=f"duel:{native['id']}", occurred_at_ms=start + 1000)
    create_duel_session(
        CHAT_A, 202, 101, snapshot(rival), snapshot(hero), 202, 101,
        status="finished", phase="block", attack_zone="head", result={
            "kind": "finalized", "winner_user_id": 101, "loser_user_id": 202,
        }, finished_at=start + 2000, now_ms=start + 1900,
    )
    create_duel_session(
        CHAT_A, 101, 202, snapshot(hero), snapshot(rival), 101, 202,
        status="finished", result={
            "kind": "finalized", "winner_user_id": 101, "loser_user_id": 202,
        }, finished_at=start - 100, now_ms=start - 200,
    )
    create_duel_session(
        CHAT_A, 101, 202, snapshot(hero), snapshot(rival), 101, 202,
        status="finished", result={"kind": "cancelled"},
        finished_at=start + 3000, now_ms=start + 2900,
    )
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        response = await client.get("/api/v1/recap",
                                    headers=await session_for(client, CHAT_A, 101))
    assert response.status_code == 200
    assert response.json()["duels"][0]["incoming"] == 1
    assert response.json()["duels"][0]["outgoing"] == 1
    assert response.json()["totals"]["wins"] == 1
    assert response.json()["totals"]["losses"] == 1


@pytest.mark.asyncio
async def test_recap_api_empty_day(temp_database):
    register(CHAT_A, 101, "hero")
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        response = await client.get("/api/v1/recap",
                                    headers=await session_for(client, CHAT_A, 101))
    assert response.status_code == 200
    assert response.json() == {
        "empty": True, "duels": [], "thefts": [], "loot": [], "pets": [],
        "totals": {"wins": 0, "losses": 0, "found": 0, "received": 0, "lost": 0},
    }


@pytest.mark.asyncio
async def test_actual_event_losses_match_in_telegram_command_and_miniapp(
    temp_database, monkeypatch, fake_context,
):
    register(CHAT_A, 101, "hero")
    actor = SimpleNamespace(id=101, username="hero", first_name="hero", last_name=None)
    monkeypatch.setattr(hyperborean_event, "_HYPERBOREAN_DB_PATH", temp_database)
    for source, reason, expected in [
        ("parity:1", "hyperboreic", "exploded"),
        ("parity:2", "hyperboreic", "restored"),
        ("parity:3", "arthur", "exploded"),
    ]:
        assert hyperborean_event._claim_hyperboreic_huy(CHAT_A, actor, reason, source) == expected

    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        response = await client.get("/api/v1/recap",
                                    headers=await session_for(client, CHAT_A, 101))
    assert response.status_code == 200
    events = load_recap_events(CHAT_A, 101)
    assert response.json() == build_recap_model(events, 101)
    assert response.json()["thefts"] == [
        "• Лишился хуя из-за гиперборейского хуя.",
        "• Лишился хуя из-за хуя Короля Артура.",
    ]
    message = SimpleNamespace(message_id=55,
                              reply_text=AsyncMock(return_value=SimpleNamespace(message_id=77)))
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=CHAT_A),
                             effective_user=actor, message=message)
    await recap_command(update, fake_context)
    telegram_text = message.reply_text.await_args.args[0]
    assert telegram_text == build_recap_text(events, 101)
    assert "🍆 <b>Происшествия с хуём</b>" in telegram_text
    assert all(escape(line) in telegram_text for line in response.json()["thefts"])
    assert fake_context.job_queue.calls[-1][1] == 60
