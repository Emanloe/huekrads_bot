"""A finished boss battle has one durable, chat-scoped presentation result."""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import gnome_avatars
import miniapp_api
from handlers import boss_registration, duel
from handlers.boss_read_model import get_boss_battle_read_model
from handlers.boss_result_repository import (
    build_boss_result,
    get_latest_boss_result,
    save_boss_result,
)
from miniapp_api import create_miniapp_api
from tests.test_boss_flow import make_battle, make_participant
from tests.test_miniapp_api import register, session_for
from tests.test_miniapp_auth import TEST_BOT_TOKEN


CHAT_A = -9501
CHAT_B = -9502


@pytest.fixture
def result_world(temp_database, monkeypatch):
    monkeypatch.setattr(boss_registration, "_BOSS_REG_DB_PATH", temp_database)
    duel.ACTIVE_BOSS_BATTLES.clear()
    yield temp_database
    duel.ACTIVE_BOSS_BATTLES.clear()


def finished_battle(battle_id, players, *, hits=5, round_num=3):
    battle = make_battle(players, phase="resolving", hits=hits, round_num=round_num)
    battle["boss"] = duel.BOSSES[0]
    battle["battle_id"] = battle_id
    return battle


@pytest.mark.asyncio
async def test_finish_persists_victory_before_cleanup_and_actual_rewards(result_world, monkeypatch):
    register(CHAT_A, 101, "winner")
    register(CHAT_A, 202, "fallen")
    alive = make_participant(101, hits=4, blocks=2, rounds_survived=3)
    dead = make_participant(202, alive=False, hits=1, rounds_survived=2)
    dead["death_round"] = 3
    battle = finished_battle("victory-1", [alive, dead])
    duel.ACTIVE_BOSS_BATTLES[CHAT_A] = battle
    real_save = duel.save_boss_result
    def save_while_active(result):
        assert duel.ACTIVE_BOSS_BATTLES[CHAT_A] is battle
        return real_save(result)
    monkeypatch.setattr(duel, "save_boss_result", save_while_active)
    monkeypatch.setattr(duel, "_boss_send_final_report", AsyncMock())
    def no_rng(*args, **kwargs):
        raise AssertionError("persistence consumed gameplay RNG")
    monkeypatch.setattr(duel.random, "choice", no_rng)
    monkeypatch.setattr(duel.random, "random", no_rng)

    await duel._boss_finish_victory(SimpleNamespace(), CHAT_A)

    assert CHAT_A not in duel.ACTIVE_BOSS_BATTLES
    result = get_latest_boss_result(CHAT_A)
    assert result["battle_id"] == "victory-1"
    assert result["boss_id"] == duel.BOSS_CATALOG_IDS[0]
    assert result["outcome"] == "victory"
    assert (result["hits"], result["required_hits"], result["rounds"]) == (5, 5, 3)
    assert result["hero_user_id"] == 101
    assert result["rewarded_user_ids"] == [101]
    assert result["participants"][1]["death_round"] == 3
    with sqlite3.connect(result_world) as conn:
        assert conn.execute(
            "SELECT points, bosses_defeated FROM duel_users WHERE chat_id=? AND user_id=?",
            (CHAT_A, 101),
        ).fetchone() == (100, 1)
        assert conn.execute(
            "SELECT bosses_defeated FROM duel_users WHERE chat_id=? AND user_id=?",
            (CHAT_A, 202),
        ).fetchone() == (0,)
    await duel._boss_finish_victory(SimpleNamespace(), CHAT_A)
    with sqlite3.connect(result_world) as conn:
        assert conn.execute("SELECT count(*) FROM boss_battle_results").fetchone() == (1,)


@pytest.mark.asyncio
async def test_defeat_and_zero_survivor_victory_keep_existing_semantics(result_world, monkeypatch):
    register(CHAT_A, 101, "player")
    report = AsyncMock()
    monkeypatch.setattr(duel, "_boss_send_final_report", report)
    dead = make_participant(101, alive=False, hits=4)
    duel.ACTIVE_BOSS_BATTLES[CHAT_A] = finished_battle("defeat-1", [dead], hits=4)
    await duel._boss_finish_defeat(SimpleNamespace(), CHAT_A)
    assert get_latest_boss_result(CHAT_A)["outcome"] == "defeat"
    assert get_latest_boss_result(CHAT_A)["rewarded_user_ids"] == []
    report.assert_awaited_once()

    # Five hits still win even if no participant survives the final round.
    duel.ACTIVE_BOSS_BATTLES[CHAT_A] = finished_battle("zero-survivor", [dead])
    await duel._boss_finish_victory(SimpleNamespace(), CHAT_A)
    assert get_latest_boss_result(CHAT_A)["outcome"] == "victory"
    assert get_latest_boss_result(CHAT_A)["rewarded_user_ids"] == []
    with sqlite3.connect(result_world) as conn:
        assert conn.execute(
            "SELECT bosses_defeated FROM duel_users WHERE chat_id=? AND user_id=?",
            (CHAT_A, 101),
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_failed_result_write_does_not_retry_gameplay_rewards(result_world, monkeypatch):
    battle = finished_battle("write-fails", [make_participant(101)])
    duel.ACTIVE_BOSS_BATTLES[CHAT_A] = battle
    def failed_write(*args, **kwargs):
        raise sqlite3.OperationalError("result store unavailable")
    monkeypatch.setattr(duel, "save_boss_result", failed_write)
    reward = Mock(return_value=True)
    monkeypatch.setattr(duel, "reward_boss_victory", reward)
    monkeypatch.setattr(duel, "_boss_send_final_report", AsyncMock())

    await duel._boss_finish_victory(SimpleNamespace(), CHAT_A)
    await duel._boss_finish_victory(SimpleNamespace(), CHAT_A)

    reward.assert_called_once_with(user_id=101, chat_id=CHAT_A)
    assert CHAT_A not in duel.ACTIVE_BOSS_BATTLES
    assert get_latest_boss_result(CHAT_A) is None


@pytest.mark.asyncio
async def test_final_report_persists_actual_item_loot_without_changing_telegram_text(
    result_world, monkeypatch,
):
    player = make_participant(101)
    battle = finished_battle("loot-1", [player])
    save_boss_result(build_boss_result(
        CHAT_A, battle, duel.BOSS_CATALOG_IDS[0], 5, victory=True,
    ))
    monkeypatch.setattr(duel, "_boss_final_report", lambda *_: "existing report")
    random_roll = Mock(return_value=0.0)
    choice = Mock(side_effect=lambda values: values[0])
    inventory = Mock()
    monkeypatch.setattr(duel.random, "random", random_roll)
    monkeypatch.setattr(duel.random, "choice", choice)
    monkeypatch.setattr(duel, "add_duel_inventory_item", inventory)
    bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock())
    await duel._boss_send_final_report(SimpleNamespace(bot=bot), CHAT_A, battle,
                                       victory=True)

    random_roll.assert_called_once_with()
    assert choice.call_count == 2
    inventory.assert_called_once_with(CHAT_A, 101, duel.DUEL_ITEMS[0]["id"])
    loot = get_latest_boss_result(CHAT_A)["item_loot"]
    assert loot == {
        "recipient_user_id": 101,
        "recipient_title": "user101",
        "item_id": duel.DUEL_ITEMS[0]["id"],
        "item_name": duel.DUEL_ITEMS[0]["name"],
    }
    message = bot.edit_message_text.await_args.kwargs["text"]
    assert message.startswith("existing report\n\n")
    assert duel.DUEL_ITEMS[0]["name"] in message
    assert (await get_boss_battle_read_model(CHAT_A, 101))["recent_result"]["viewer"][
        "received_item"] is True


def test_result_key_is_idempotent_and_latest_is_chat_scoped(result_world):
    first = build_boss_result(
        CHAT_A, finished_battle("same-id", [make_participant(101)], hits=5),
        duel.BOSS_CATALOG_IDS[0], 5, victory=True,
    )
    assert save_boss_result(first) is True
    altered = {**first, "outcome": "defeat"}
    assert save_boss_result(altered) is False
    assert get_latest_boss_result(CHAT_A)["outcome"] == "victory"
    second = build_boss_result(
        CHAT_A, finished_battle("later", [make_participant(101, alive=False)], hits=2),
        duel.BOSS_CATALOG_IDS[0], 5, victory=False,
    )
    second["finished_at"] = first["finished_at"] + 1
    save_boss_result(second)
    assert get_latest_boss_result(CHAT_A)["battle_id"] == "later"
    assert get_latest_boss_result(CHAT_B) is None
    with sqlite3.connect(result_world) as conn:
        assert conn.execute("SELECT count(*) FROM boss_battle_results").fetchone() == (2,)


@pytest.mark.asyncio
async def test_recent_result_api_is_read_only_chat_scoped_and_keeps_registration(result_world,
                                                                                  monkeypatch):
    battle_a = finished_battle("chat-a", [make_participant(101, hits=5),
                                          make_participant(202, alive=False, hits=0)])
    battle_b = finished_battle("chat-b", [make_participant(101, alive=False)], hits=2)
    save_boss_result(build_boss_result(CHAT_A, battle_a, duel.BOSS_CATALOG_IDS[0], 5,
                                       victory=True))
    save_boss_result(build_boss_result(CHAT_B, battle_b, duel.BOSS_CATALOG_IDS[0], 5,
                                       victory=False))
    monkeypatch.setattr(boss_registration, "_boss_registration_is_open", lambda: True)
    def no_rng(*args, **kwargs):
        raise AssertionError("GET consumed RNG or assigned an avatar")
    monkeypatch.setattr(duel.random, "choice", no_rng)
    monkeypatch.setattr(duel.random, "random", no_rng)
    monkeypatch.setattr(gnome_avatars.secrets, "choice", no_rng)
    monkeypatch.setattr(miniapp_api, "get_or_assign_gnome_variant", no_rng)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        headers_a = await session_for(client, CHAT_A, 101)
        headers_b = await session_for(client, CHAT_B, 101)
        before = result_world.read_bytes()
        for _ in range(10):
            response = await client.request(
                "GET", f"/api/v1/boss?chat_id={CHAT_B}&battle_id=chat-b",
                headers={**headers_a, "X-Chat-Id": str(CHAT_B)},
                json={"chat_id": CHAT_B, "battle_id": "chat-b"},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["battle"] is None
            assert data["recent_result"]["battle_id"] == "chat-a"
            assert data["recent_result"]["hero"] == {
                "user_id": 101, "title": "user101", "hits": 5,
                "blocks": 0, "rounds_survived": 0,
            }
            assert data["recent_result"]["viewer"] == {
                "participated": True, "alive": True, "hits": 5,
                "blocks": 0, "rounds_survived": 0,
                "rewarded": False, "received_item": False,
            }
            assert data["registration"]["open"] is True
            assert "chat-b" not in response.text
            assert TEST_BOT_TOKEN not in response.text
        assert (await client.get("/api/v1/boss", headers=headers_b)).json()[
            "recent_result"]["battle_id"] == "chat-b"
        assert result_world.read_bytes() == before

        duel.ACTIVE_BOSS_BATTLES[CHAT_A] = finished_battle("active-new", [make_participant(101)])
        duel.ACTIVE_BOSS_BATTLES[CHAT_A]["phase"] = "join"
        duel.ACTIVE_BOSS_BATTLES[CHAT_A]["round"] = 0
        active = (await client.get("/api/v1/boss", headers=headers_a)).json()
        assert active["battle"]["battle_id"] == "active-new"
        assert active["recent_result"] is None
