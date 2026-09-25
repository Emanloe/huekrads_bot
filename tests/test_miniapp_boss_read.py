"""Mini App sees the Telegram boss battle without moving or mutating it."""

import asyncio
import copy
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import gnome_avatars
import miniapp_api
from handlers import boss_registration, duel
from handlers.boss_read_model import get_boss_battle_read_model
from miniapp_api import create_miniapp_api
from tests.test_miniapp_api import session_for
from tests.test_miniapp_auth import TEST_BOT_TOKEN


CHAT_A = -9401
CHAT_B = -9402


@pytest.fixture
def boss_world(temp_database, monkeypatch):
    monkeypatch.setattr(boss_registration, "_BOSS_REG_DB_PATH", temp_database)
    duel.ACTIVE_BOSS_BATTLES.clear()
    yield temp_database
    duel.ACTIVE_BOSS_BATTLES.clear()


def participant(user_id, *, alive=True, attack=None, block=None, hits=0):
    return {
        "tg_user": SimpleNamespace(id=user_id),
        "data": {"username": f"player_{user_id}",
                 "display_name": f"<player {user_id}>", "dwarf_name": "Гном"},
        "alive": alive, "attack": attack, "block": block,
        "hits": hits, "misses": 0, "blocks": 0, "rounds_survived": 0,
    }


def battle(boss, players, *, phase="attack", round_num=2, hits=3):
    return {
        "boss": boss,
        "participants": {p["tg_user"].id: p for p in players},
        "phase": phase, "round": round_num, "hits": hits,
        "boss_attack": "head", "boss_block": "dick",
        "message_id": 98765, "phase_task": None,
        "lock": asyncio.Lock(),
    }


@pytest.mark.asyncio
async def test_no_boss_read_does_not_create_registration_table_or_write_db(boss_world,
                                                                         monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("boss read used RNG or avatar assignment")

    monkeypatch.setattr(duel.random, "choice", forbidden)
    monkeypatch.setattr(duel.random, "random", forbidden)
    monkeypatch.setattr(gnome_avatars.secrets, "choice", forbidden)
    monkeypatch.setattr(miniapp_api, "get_or_assign_gnome_variant", forbidden)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        headers = await session_for(client, CHAT_A, 101)
        before = boss_world.read_bytes()
        for _ in range(10):
            response = await client.get("/api/v1/boss", headers=headers)
            assert response.status_code == 200
            data = response.json()
            assert data["battle"] is None
            assert data["registration"]["participants_count"] == 0
            assert data["registration"]["viewer_registered"] is False
            assert data["viewer"]["in_battle"] is False
            assert response.headers["cache-control"] == "no-store"
        assert boss_world.read_bytes() == before
        assert (await client.post("/api/v1/boss", headers=headers)).status_code == 405
        assert (await client.get("/api/v1/boss")).status_code == 401
    with sqlite3.connect(boss_world) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'boss_registrations'",
        ).fetchone() is None


@pytest.mark.asyncio
async def test_registration_read_is_chat_scoped_and_does_not_mutate(boss_world, monkeypatch):
    monkeypatch.setattr(boss_registration, "_boss_today", lambda: "2026-09-25")
    monkeypatch.setattr(boss_registration, "_boss_registration_is_open", lambda: True)
    user_a = SimpleNamespace(id=101, username="same", first_name="A", last_name=None)
    other_a = SimpleNamespace(id=202, username="other", first_name="B", last_name=None)
    assert boss_registration._boss_register_user(CHAT_A, user_a)
    assert boss_registration._boss_register_user(CHAT_A, other_a)
    assert boss_registration._boss_register_user(CHAT_B, other_a)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        headers_a = await session_for(client, CHAT_A, 101)
        headers_b = await session_for(client, CHAT_B, 101)
        before = boss_world.read_bytes()
        a = (await client.get("/api/v1/boss", headers=headers_a)).json()
        b = (await client.get("/api/v1/boss", headers=headers_b)).json()
        assert a["registration"] == {
            "open": True, "participants_count": 2, "viewer_registered": True,
        }
        assert b["registration"] == {
            "open": True, "participants_count": 1, "viewer_registered": False,
        }
        assert boss_world.read_bytes() == before


@pytest.mark.asyncio
async def test_live_boss_chat_isolation_viewer_state_and_safe_snapshot(boss_world, monkeypatch):
    a = battle(duel.BOSSES[-1], [
        participant(101, attack="body", hits=2),
        participant(202, alive=False, hits=1),
    ])
    b = battle(duel.BOSSES[0], [participant(101, attack=None, hits=0)],
               phase="join", round_num=0, hits=0)
    duel.ACTIVE_BOSS_BATTLES[CHAT_A] = a
    duel.ACTIVE_BOSS_BATTLES[CHAT_B] = b
    before_state = copy.deepcopy({
        "a_participants": a["participants"], "b_participants": b["participants"],
        "a_hits": a["hits"], "b_hits": b["hits"],
        "a_phase": a["phase"], "b_phase": b["phase"],
    })
    before_telegram = duel._boss_phase_text(a, duel.BOSS_REQUIRED_HITS)

    def forbidden(*args, **kwargs):
        raise AssertionError("boss read used RNG or avatar assignment")

    monkeypatch.setattr(duel.random, "choice", forbidden)
    monkeypatch.setattr(duel.random, "random", forbidden)
    monkeypatch.setattr(gnome_avatars.secrets, "choice", forbidden)
    monkeypatch.setattr(miniapp_api, "get_or_assign_gnome_variant", forbidden)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        headers_a = await session_for(client, CHAT_A, 101)
        headers_b = await session_for(client, CHAT_B, 101)
        headers_dead = await session_for(client, CHAT_A, 202)
        headers_outsider = await session_for(client, CHAT_A, 303)
        before_db = boss_world.read_bytes()
        forged = {**headers_a, "X-Chat-Id": str(CHAT_B), "X-User-Id": "202"}
        for _ in range(10):
            response = await client.request(
                "GET", f"/api/v1/boss?chat_id={CHAT_B}&target_user_id=202",
                headers=forged, json={"chat_id": CHAT_B, "target_user_id": 202},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["battle"]["boss"]["id"] == "chizyanovsky_skier"
            assert data["battle"]["hits"] == 3
            assert data["battle"]["required_hits"] == 5
            assert data["battle"]["participants_count"] == 2
            assert data["battle"]["alive_count"] == 1
            assert data["viewer"] == {
                "in_battle": True, "alive": True, "hits": 2,
                "choice_submitted": True, "selected_action": "body",
                "can_act_in_telegram": True,
            }
            assert data["battle"]["participants"][0]["title"] == "Гном (player_101)"
            assert "boss_attack" not in response.text
            assert "boss_block" not in response.text
            assert "message_id" not in response.text
            assert data["available_actions"][0]["id"] == "head"
            assert data["available_actions"][-1]["id"] == "dick"
            assert TEST_BOT_TOKEN not in response.text
        other_chat = (await client.get("/api/v1/boss", headers=headers_b)).json()
        dead = (await client.get("/api/v1/boss", headers=headers_dead)).json()
        outsider = (await client.get("/api/v1/boss", headers=headers_outsider)).json()
        assert other_chat["battle"]["boss"]["id"] == "deep_snouted_baron"
        assert other_chat["viewer"]["can_act_in_telegram"] is False
        assert dead["viewer"]["in_battle"] is True
        assert dead["viewer"]["alive"] is False
        assert dead["viewer"]["can_act_in_telegram"] is False
        assert outsider["viewer"]["in_battle"] is False
        assert outsider["viewer"]["can_act_in_telegram"] is False
        assert boss_world.read_bytes() == before_db
    assert before_telegram == duel._boss_phase_text(a, duel.BOSS_REQUIRED_HITS)
    assert before_state == {
        "a_participants": a["participants"], "b_participants": b["participants"],
        "a_hits": a["hits"], "b_hits": b["hits"],
        "a_phase": a["phase"], "b_phase": b["phase"],
    }


@pytest.mark.asyncio
async def test_phase_visibility_and_finished_battle_limit(boss_world):
    current = battle(duel.BOSSES[0], [participant(101)],
                     phase="join", round_num=0, hits=0)
    duel.ACTIVE_BOSS_BATTLES[CHAT_A] = current
    join = await get_boss_battle_read_model(CHAT_A, 202)
    assert join["viewer"]["in_battle"] is False
    assert join["viewer"]["can_act_in_telegram"] is True
    current["phase"] = "block"
    block = await get_boss_battle_read_model(CHAT_A, 101)
    assert block["viewer"]["choice_submitted"] is False
    assert block["viewer"]["can_act_in_telegram"] is True
    current["participants"][101]["block"] = "head"
    chosen = await get_boss_battle_read_model(CHAT_A, 101)
    assert chosen["viewer"]["choice_submitted"] is True
    assert chosen["viewer"]["can_act_in_telegram"] is True
    current["phase"] = "resolving"
    resolving = await get_boss_battle_read_model(CHAT_A, 101)
    assert resolving["viewer"]["choice_submitted"] is None
    assert resolving["viewer"]["can_act_in_telegram"] is False
    duel.ACTIVE_BOSS_BATTLES.pop(CHAT_A)
    finished = await get_boss_battle_read_model(CHAT_A, 101)
    assert finished["battle"] is None
    assert "recent_result" not in finished


@pytest.mark.asyncio
async def test_telegram_callback_updates_same_battle_seen_by_miniapp(boss_world, monkeypatch):
    current = battle(duel.BOSSES[0], [participant(101), participant(202)])
    duel.ACTIVE_BOSS_BATTLES[CHAT_A] = current
    monkeypatch.setattr(duel, "_boss_render_phase", AsyncMock())
    before = await get_boss_battle_read_model(CHAT_A, 101)
    assert before["viewer"]["choice_submitted"] is False

    query = SimpleNamespace(
        data="boss_attack_head_2", from_user=SimpleNamespace(id=101),
        answer=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=CHAT_A))
    await duel.boss_callback(update, SimpleNamespace())

    after = await get_boss_battle_read_model(CHAT_A, 101)
    assert current["participants"][101]["attack"] == "head"
    assert after["viewer"]["choice_submitted"] is True
    assert after["viewer"]["can_act_in_telegram"] is True
    assert after["battle"]["participants"][0]["choice_submitted"] is True
    assert (await get_boss_battle_read_model(CHAT_B, 101))["battle"] is None
