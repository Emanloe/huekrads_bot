"""Both boss interfaces mutate one locked, chat-scoped in-memory battle."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from handlers import duel
from miniapp_api import create_miniapp_api
from tests.test_miniapp_api import register, session_for
from tests.test_miniapp_auth import TEST_BOT_TOKEN
from tests.test_miniapp_boss_read import CHAT_A, CHAT_B, battle, participant


@pytest.fixture
def boss_actions_world(temp_database):
    duel.ACTIVE_BOSS_BATTLES.clear()
    yield
    duel.ACTIVE_BOSS_BATTLES.clear()


def current_battle(players, *, phase="attack", round_num=2, identity="battle_A_001"):
    current = battle(duel.BOSSES[0], players, phase=phase, round_num=round_num, hits=0)
    current["battle_id"] = identity
    current["deadline_at"] = time.time() + 10
    return current


def fake_bot():
    return SimpleNamespace(edit_message_text=AsyncMock(), delete_message=AsyncMock())


def request_body(current, *, phase="attack", action_id="head"):
    return {
        "battle_id": current["battle_id"], "round": current["round"],
        "phase": phase, "action_id": action_id,
    }


@pytest.mark.asyncio
async def test_join_uses_verified_chat_user_and_preserves_telegram_join(boss_actions_world):
    register(CHAT_A, 101, "hero_a")
    register(CHAT_B, 101, "hero_b")
    current = current_battle([], phase="join", round_num=0)
    duel.ACTIVE_BOSS_BATTLES[CHAT_A] = current
    bot = fake_bot()
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="", telegram_bot=bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        a = await session_for(client, CHAT_A, 101)
        b = await session_for(client, CHAT_B, 101)
        state = (await client.get("/api/v1/boss", headers=a)).json()
        assert state["battle"]["battle_id"] == current["battle_id"]
        assert state["battle"]["deadline_at"] == int(current["deadline_at"] * 1000)
        assert state["available_actions"][0]["id"] == "join"
        forged = {"battle_id": current["battle_id"], "round": 0,
                  "chat_id": CHAT_B, "user_id": 202}
        assert (await client.post("/api/v1/boss/join", headers=a, json=forged)).status_code == 422
        assert current["participants"] == {}
        assert (await client.post("/api/v1/boss/join", headers=b, json={
            "battle_id": current["battle_id"], "round": 0,
        })).json()["detail"]["code"] == "no_active_battle"
        response = await client.post("/api/v1/boss/join",
                                     headers={**a, "X-Chat-Id": str(CHAT_B)},
                                     json={"battle_id": current["battle_id"], "round": 0})
        assert response.status_code == 200
        assert set(current["participants"]) == {101}
        assert current["participants"][101]["tg_user"].id == 101
        bot.edit_message_text.assert_awaited_once()
        again = await client.post("/api/v1/boss/join", headers=a,
                                  json={"battle_id": current["battle_id"], "round": 0})
        assert again.json()["detail"]["code"] == "already_joined"
        assert set(current["participants"]) == {101}
        telegram_user = SimpleNamespace(
            id=202, username="telegram_player", first_name="Telegram", last_name=None,
        )
        query = SimpleNamespace(data="boss_join", from_user=telegram_user, answer=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=CHAT_A))
        await duel.boss_callback(update, SimpleNamespace(bot=bot))
        assert set(current["participants"]) == {101, 202}
        query.answer.assert_awaited_once()
        current["phase"] = "attack"
        closed = await client.post("/api/v1/boss/join", headers=a,
                                   json={"battle_id": current["battle_id"], "round": 0})
        assert closed.json()["detail"]["code"] == "recruitment_closed"


@pytest.mark.asyncio
async def test_action_guards_and_replace_choice_semantics(boss_actions_world, monkeypatch):
    current = current_battle([
        participant(101), participant(202), participant(303, alive=False),
    ])
    duel.ACTIVE_BOSS_BATTLES[CHAT_A] = current
    duel.ACTIVE_BOSS_BATTLES[CHAT_B] = current_battle(
        [participant(101)], identity="battle_B_001",
    )
    monkeypatch.setattr(duel, "_boss_render_phase", AsyncMock())
    monkeypatch.setattr(duel.random, "choice",
                        lambda _: (_ for _ in ()).throw(AssertionError("action used RNG")))
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="",
                             telegram_bot=fake_bot())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        a = await session_for(client, CHAT_A, 101)
        dead = await session_for(client, CHAT_A, 303)
        outsider = await session_for(client, CHAT_A, 404)
        body = request_body(current)
        for changed, code in [
            ({"battle_id": "battle_B_001"}, "stale_battle"),
            ({"round": 1}, "stale_round"),
            ({"phase": "block"}, "stale_phase"),
            ({"action_id": "invalid"}, "invalid_action"),
        ]:
            response = await client.post("/api/v1/boss/action", headers=a,
                                         json={**body, **changed})
            assert response.json()["detail"]["code"] == code
        assert (await client.post("/api/v1/boss/action", headers=dead,
                                  json=body)).json()["detail"]["code"] == "eliminated"
        assert (await client.post("/api/v1/boss/action", headers=outsider,
                                  json=body)).json()["detail"]["code"] == "not_participant"
        assert (await client.post("/api/v1/boss/action", headers=a,
                                  json={**body, "user_id": 202})).status_code == 422
        first = await client.post("/api/v1/boss/action", headers=a, json=body)
        assert first.status_code == 200
        duplicate = await client.post("/api/v1/boss/action", headers=a, json=body)
        assert duplicate.json()["detail"]["code"] == "already_acted"
        # Existing Telegram gameplay allows replacing a choice before phase end.
        replacement = await client.post("/api/v1/boss/action", headers=a,
                                        json={**body, "action_id": "body"})
        assert replacement.status_code == 200
        assert current["participants"][101]["attack"] == "body"
        assert current["phase"] == "attack"
        assert current["hits"] == 0
        duel.ACTIVE_BOSS_BATTLES[CHAT_A] = current_battle(
            [participant(101)], identity="next_battle_001",
        )
        stale = await client.post("/api/v1/boss/action", headers=a, json=body)
        assert stale.json()["detail"]["code"] == "stale_battle"
        assert duel.ACTIVE_BOSS_BATTLES[CHAT_A]["participants"][101]["attack"] is None


@pytest.mark.asyncio
async def test_mixed_telegram_and_miniapp_actions_transition_once(
    boss_actions_world, monkeypatch,
):
    current = current_battle([participant(101), participant(202)])
    duel.ACTIVE_BOSS_BATTLES[CHAT_A] = current
    render = AsyncMock()
    schedule = Mock()
    monkeypatch.setattr(duel, "_boss_render_phase", render)
    monkeypatch.setattr(duel, "_boss_schedule_phase_timer", schedule)
    bot = fake_bot()
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="", telegram_bot=bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        a = await session_for(client, CHAT_A, 101)
        query = SimpleNamespace(
            data="boss_attack_body_2", from_user=SimpleNamespace(id=202),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=CHAT_A))
        post, _ = await asyncio.gather(
            client.post("/api/v1/boss/action", headers=a, json=request_body(current)),
            duel.boss_callback(update, SimpleNamespace(bot=bot)),
        )
        assert post.status_code == 200
        query.answer.assert_awaited_once()
        assert current["participants"][101]["attack"] == "head"
        assert current["participants"][202]["attack"] == "body"
        assert current["phase"] == "block"
        schedule.assert_called_once()
        assert render.await_count == 3
        old = await client.post("/api/v1/boss/action", headers=a,
                                json=request_body(current, phase="attack"))
        assert old.json()["detail"]["code"] == "stale_phase"


@pytest.mark.asyncio
async def test_concurrent_same_player_choices_keep_one_effective_move(
    boss_actions_world, monkeypatch,
):
    current = current_battle([participant(101), participant(202)])
    duel.ACTIVE_BOSS_BATTLES[CHAT_A] = current
    monkeypatch.setattr(duel, "_boss_render_phase", AsyncMock())
    bot = fake_bot()
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="", telegram_bot=bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        a = await session_for(client, CHAT_A, 101)
        body = request_body(current)
        first, second = await asyncio.gather(
            client.post("/api/v1/boss/action", headers=a, json=body),
            client.post("/api/v1/boss/action", headers=a,
                        json={**body, "action_id": "body"}),
        )
        # Product decision: changing the one selected zone is allowed in this phase.
        assert first.status_code == second.status_code == 200
        assert current["participants"][101]["attack"] in ("head", "body")
        assert current["phase"] == "attack"
        assert current["hits"] == 0
        current["participants"][101]["attack"] = None
        same = await asyncio.gather(
            client.post("/api/v1/boss/action", headers=a, json=body),
            client.post("/api/v1/boss/action", headers=a, json=body),
        )
        assert sorted(response.status_code for response in same) == [200, 409]
        assert [response.json().get("detail", {}).get("code") for response in same].count(
            "already_acted"
        ) == 1
        assert current["participants"][101]["attack"] == "head"
        query = SimpleNamespace(
            data="boss_attack_dick_2", from_user=SimpleNamespace(id=101),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=CHAT_A))
        await duel.boss_callback(update, SimpleNamespace(bot=bot))
        assert current["participants"][101]["attack"] == "dick"
        assert current["phase"] == "attack"


@pytest.mark.asyncio
async def test_defense_uses_same_transition_and_resolves_once(
    boss_actions_world, monkeypatch,
):
    current = current_battle([
        participant(101, attack="head"), participant(202, attack="body"),
    ], phase="block")
    duel.ACTIVE_BOSS_BATTLES[CHAT_A] = current
    monkeypatch.setattr(duel, "_boss_render_phase", AsyncMock())
    resolve = AsyncMock()
    monkeypatch.setattr(duel, "_boss_resolve_round", resolve)
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="",
                             telegram_bot=fake_bot())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        a = await session_for(client, CHAT_A, 101)
        body = request_body(current, phase="block")
        first = await client.post("/api/v1/boss/action", headers=a, json=body)
        assert first.status_code == 200
        query = SimpleNamespace(
            data="boss_block_body_2", from_user=SimpleNamespace(id=202),
            answer=AsyncMock(),
        )
        update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=CHAT_A))
        await duel.boss_callback(update, SimpleNamespace(bot=fake_bot()))
        await asyncio.sleep(0)
        resolve.assert_awaited_once()
        assert current["participants"][101]["block"] == "head"
        assert current["participants"][202]["block"] == "body"


@pytest.mark.asyncio
async def test_timeout_and_miniapp_attack_race_has_one_phase_transition(
    boss_actions_world, monkeypatch,
):
    current = current_battle([participant(101), participant(202)])
    duel.ACTIVE_BOSS_BATTLES[CHAT_A] = current
    entered, release = asyncio.Event(), asyncio.Event()

    async def controlled_sleep(_seconds):
        entered.set()
        await release.wait()

    monkeypatch.setattr(duel, "asyncio", SimpleNamespace(
        sleep=controlled_sleep, current_task=asyncio.current_task,
        CancelledError=asyncio.CancelledError, create_task=asyncio.create_task,
    ))
    monkeypatch.setattr(duel, "_boss_render_phase", AsyncMock())
    schedule = Mock()
    monkeypatch.setattr(duel, "_boss_schedule_phase_timer", schedule)
    monkeypatch.setattr(duel, "schedule_auto_delete", lambda *_: None)
    bot = fake_bot()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=123))
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="", telegram_bot=bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        a = await session_for(client, CHAT_A, 101)
        choices = []
        monkeypatch.setattr(duel.random, "choice",
                            lambda population: choices.append(tuple(population)) or "head")
        timer = asyncio.create_task(duel._boss_phase_timer(
            SimpleNamespace(bot=bot), CHAT_A, current["round"], "attack",
        ))
        current["phase_task"] = timer
        await entered.wait()
        post = asyncio.create_task(client.post("/api/v1/boss/action", headers=a,
                                               json=request_body(current)))
        release.set()
        response, _ = await asyncio.gather(post, timer)
        assert response.status_code in (200, 409)
        if response.status_code == 409:
            assert response.json()["detail"]["code"] == "stale_phase"
        assert current["phase"] == "block"
        schedule.assert_called_once()
        assert current["participants"][101]["attack"] in duel.BOSS_ZONES
        assert current["participants"][202]["attack"] in duel.BOSS_ZONES
        assert len(choices) == (1 if response.status_code == 200 else 2)


@pytest.mark.asyncio
async def test_waiting_old_round_task_cannot_mutate_next_battle(boss_actions_world, monkeypatch):
    old = current_battle([participant(101, attack="head", block="body")],
                         phase="block", identity="old_battle_001")
    next_battle = current_battle([participant(202)], identity="new_battle_001")
    duel.ACTIVE_BOSS_BATTLES[CHAT_A] = old
    monkeypatch.setattr(duel, "_boss_auto_zone",
                        Mock(side_effect=AssertionError("stale round used RNG")))
    bot = fake_bot()
    async with old["lock"]:
        resolver = asyncio.create_task(
            duel._boss_resolve_round(SimpleNamespace(bot=bot), CHAT_A),
        )
        await asyncio.sleep(0)
        duel.ACTIVE_BOSS_BATTLES[CHAT_A] = next_battle
    await resolver
    assert old["phase"] == "block"
    assert next_battle["phase"] == "attack"
    bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_actions_during_telegram_round_edit_do_not_restore_attack_timer(
    boss_actions_world, monkeypatch,
):
    current = current_battle([participant(101), participant(202)],
                             phase="join", round_num=0)
    duel.ACTIVE_BOSS_BATTLES[CHAT_A] = current
    editing, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def render(_context, _chat_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            editing.set()
            await release.wait()

    monkeypatch.setattr(duel, "_boss_render_phase", render)
    monkeypatch.setattr(duel.random, "choice", lambda population: population[0])
    schedule = Mock()
    monkeypatch.setattr(duel, "_boss_schedule_phase_timer", schedule)
    bot = fake_bot()
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="", telegram_bot=bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        a = await session_for(client, CHAT_A, 101)
        b = await session_for(client, CHAT_A, 202)
        start = asyncio.create_task(duel._boss_start_round(SimpleNamespace(bot=bot), CHAT_A))
        await editing.wait()
        assert current["round"] == 1
        body = request_body(current)
        assert (await client.post("/api/v1/boss/action", headers=a, json=body)).status_code == 200
        assert (await client.post("/api/v1/boss/action", headers=b, json=body)).status_code == 200
        assert current["phase"] == "block"
        release.set()
        await start
        schedule.assert_called_once()
        assert schedule.call_args.args[-1] == "block"
