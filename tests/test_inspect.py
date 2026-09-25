"""Inspect uses one read model and never crosses a chat boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import database
from duel_session_repository import create_duel_session, utc_unix_milliseconds
from gnome_avatars import gnome_image_url
from handlers import duel, duel_service
from handlers.player_stats import player_stats_read_model, public_player_stats
from miniapp_api import create_miniapp_api
from tests.test_miniapp_api import CHAT_A, CHAT_B, register, session_for, snapshot
from tests.test_miniapp_auth import TEST_BOT_TOKEN
from text_resources import get_text


def update_for(chat_id, sender_id, *, reply_id=None, text="/inspect"):
    reply = (SimpleNamespace(from_user=SimpleNamespace(id=reply_id), delete=AsyncMock())
             if reply_id is not None else None)
    return SimpleNamespace(message=SimpleNamespace(
        from_user=SimpleNamespace(id=sender_id), chat=SimpleNamespace(id=chat_id),
        chat_id=chat_id, text=text, reply_to_message=reply, delete=AsyncMock(),
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply_id", "args", "missing_model", "expected_key"),
    [
        (202, [], False, None),
        (None, ["@target"], False, None),
        (None, ["@viewer"], False, None),
        (303, [], False, "duel.inspect.inaccessible"),
        (None, ["@missing"], False, "duel.inspect.inaccessible"),
        (None, ["@@"], False, "duel.inspect.usage"),
        (None, [], False, "duel.inspect.usage"),
        (202, [], True, "duel.inspect.inaccessible"),
    ],
)
async def test_inspect_deletes_only_invoking_command_on_all_response_paths(
    temp_database, fake_context, monkeypatch, reply_id, args, missing_model, expected_key,
):
    register(CHAT_A, 101, "viewer")
    register(CHAT_A, 202, "target")
    fake_context.args = args
    if missing_model:
        monkeypatch.setattr(duel, "player_stats_read_model", lambda *_: None)
    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    update = update_for(CHAT_A, 101, reply_id=reply_id)

    await duel.inspect_command(update, fake_context)

    sent.assert_awaited_once()
    if expected_key:
        assert sent.await_args.args[2] == get_text(expected_key)
    update.message.delete.assert_awaited_once_with()
    if update.message.reply_to_message:
        update.message.reply_to_message.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_inspect_delete_failure_does_not_interrupt_response(
    temp_database, fake_context, monkeypatch,
):
    register(CHAT_A, 101, "viewer")
    register(CHAT_A, 202, "target")
    events = []
    sent = AsyncMock(side_effect=lambda *_: events.append("response"))
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    update = update_for(CHAT_A, 101, reply_id=202)

    async def denied():
        events.append("delete_attempt")
        raise RuntimeError("Telegram denied deletion")

    update.message.delete.side_effect = denied
    await duel.inspect_command(update, fake_context)

    assert events == ["response", "delete_attempt"]
    sent.assert_awaited_once()
    update.message.delete.assert_awaited_once_with()
    update.message.reply_to_message.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_inspect_matches_me_and_telegram_stats_without_gameplay_writes(
    temp_database, fake_context, monkeypatch,
):
    register(CHAT_A, 101, "viewer")
    register(CHAT_A, 202, "other")
    with database.get_db() as conn:
        conn.execute(
            "UPDATE duel_users SET points = 0, wins = 100, losses = 200, "
            "stolen_dicks_count = 10, bosses_defeated = 3, dick_stolen_today = 1 "
            "WHERE chat_id = ? AND user_id = ?", (CHAT_A, 202),
        )
    for item in ("ceremonial_bolt", "ceremonial_bolt", "unknown_legacy"):
        database.add_duel_inventory_item(CHAT_A, 202, item)
    with database.get_db() as conn:
        before = list(conn.execute(
            "SELECT points, wins, losses, dick_stolen_today, last_activity_date "
            "FROM duel_users WHERE chat_id = ? AND user_id = ?", (CHAT_A, 202),
        ))
    monkeypatch.setattr(duel_service.random, "random", lambda: (_ for _ in ()).throw(AssertionError("RNG")))
    monkeypatch.setattr(duel_service.random, "choice", lambda _: (_ for _ in ()).throw(AssertionError("RNG")))
    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    model = player_stats_read_model(CHAT_A, 202)
    assert model["points"] == 0
    assert [item["item_id"] for item in model["inventory"]][:2] == ["oiled_vest", "knife"]
    assert model["inventory"][-1]["name"] == "unknown_legacy"

    reply_update = update_for(CHAT_A, 101, reply_id=202)
    await duel.inspect_command(reply_update, fake_context)
    reply_update.message.delete.assert_awaited_once_with()
    reply_update.message.reply_to_message.delete.assert_not_awaited()
    telegram = sent.await_args.args[2]
    for value in ("0 / 100", "100", "200", "Без хуя", "Парадный болт ×2"):
        assert value in telegram
    sent.reset_mock()
    fake_context.args = ["@other"]
    username_update = update_for(CHAT_A, 101)
    await duel.inspect_command(username_update, fake_context)
    username_update.message.delete.assert_awaited_once_with()
    assert sent.await_args.args[2] == telegram

    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        viewer = await session_for(client, CHAT_A, 101)
        own = await session_for(client, CHAT_A, 202)
        listing = (await client.get("/api/v1/duel/opponents", headers=viewer)).json()
        assert listing["opponents"][0]["user_id"] == 202
        assert listing["opponents"][0]["duel_ineligibility"] == "no_dick"
        inspect = (await client.get("/api/v1/players/202", headers=viewer)).json()
        assert (await client.get("/api/v1/players/202", headers=viewer)).json() == inspect
        assert "gnome_variant" not in inspect
        assert inspect["gnome_image_url"] == gnome_image_url("gnome_00")
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT gnome_variant FROM duel_users WHERE chat_id = ? AND user_id = ?",
                (CHAT_A, 202),
            ).fetchone() == (None,)
        own_me = (await client.get("/api/v1/me", headers=own)).json()
        inspect_stats = {key: value for key, value in inspect.items()
                         if key != "gnome_image_url"}
        assert inspect_stats == {key: value for key, value in own_me.items()
                                 if key not in ("gnome_variant", "gnome_image_url")}
        assert inspect_stats == public_player_stats(model)
        viewer_inspect = (await client.get("/api/v1/players/101", headers=viewer)).json()
        viewer_me = (await client.get("/api/v1/me", headers=viewer)).json()
        assert {key: value for key, value in viewer_inspect.items()
                if key != "gnome_image_url"} == {
                    key: value for key, value in viewer_me.items()
                    if key not in ("gnome_variant", "gnome_image_url")
                }
    with database.get_db() as conn:
        after = list(conn.execute(
            "SELECT points, wins, losses, dick_stolen_today, last_activity_date "
            "FROM duel_users WHERE chat_id = ? AND user_id = ?", (CHAT_A, 202),
        ))
    assert after == before


@pytest.mark.asyncio
async def test_inspect_chat_isolation_unknown_targets_and_dickless_viewer(
    temp_database, fake_context, monkeypatch,
):
    for chat in (CHAT_A, CHAT_B):
        register(chat, 101, "viewer")
        register(chat, 202, "same")
    register(CHAT_B, 303, "b_only")
    with database.get_db() as conn:
        conn.execute("UPDATE duel_users SET points = 12 WHERE chat_id = ? AND user_id = 202", (CHAT_A,))
        conn.execute("UPDATE duel_users SET points = 87 WHERE chat_id = ? AND user_id = 202", (CHAT_B,))
        conn.execute("UPDATE duel_users SET dick_stolen_today = 1 WHERE chat_id = ? AND user_id = 101", (CHAT_A,))
    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    await duel.inspect_command(update_for(CHAT_A, 101, reply_id=303), fake_context)
    assert "недоступен" in sent.await_args.args[2]
    sent.reset_mock()
    fake_context.args = ["@b_only"]
    await duel.inspect_command(update_for(CHAT_A, 101), fake_context)
    assert "недоступен" in sent.await_args.args[2]
    assert player_stats_read_model(CHAT_A, 303) is None

    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        session = await session_for(client, CHAT_A, 101)
        headers = {**session, "X-Chat-Id": str(CHAT_B), "X-User-Id": "303"}
        listing = (await client.get("/api/v1/duel/opponents", headers=headers)).json()
        assert listing["ineligibility"] == "no_dick"
        assert [row["user_id"] for row in listing["opponents"]] == [202]
        assert (await client.request(
            "GET", f"/api/v1/players/202?chat_id={CHAT_B}&user_id=303",
            headers=headers, json={"chat_id": CHAT_B, "user_id": 303},
        )).json()["points"] == 12
        for target in (303, 999, -1):
            response = await client.get(f"/api/v1/players/{target}", headers=headers)
            assert response.status_code == 404
            assert response.json()["detail"]["code"] == "inaccessible_player"
        assert (await client.get("/api/v1/players/202")).status_code == 401
        assert (await client.get("/api/v1/players/not-a-number", headers=headers)).status_code == 422
        denied = await client.post("/api/v1/duel/start", headers=session,
                                   json={"opponent_user_id": 202})
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "initiator_no_dick"


@pytest.mark.asyncio
async def test_inspect_usage_unknown_self_and_active_duel_list(
    temp_database, fake_context, monkeypatch,
):
    first = register(CHAT_A, 101, "viewer")
    second = register(CHAT_A, 202, "target")
    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    await duel.inspect_command(update_for(CHAT_A, 101), fake_context)
    assert "/inspect @username" in sent.await_args.args[2]
    fake_context.args = ["@missing"]
    await duel.inspect_command(update_for(CHAT_A, 101), fake_context)
    assert "недоступен" in sent.await_args.args[2]
    fake_context.args = ["@viewer"]
    await duel.inspect_command(update_for(CHAT_A, 101), fake_context)
    assert "Осмотр игрока" in sent.await_args.args[2]

    create_duel_session(CHAT_A, 101, 202, snapshot(first), snapshot(second),
                        101, 202, status="active", phase="attack",
                        deadline_at=utc_unix_milliseconds() + 60_000)
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        session = await session_for(client, CHAT_A, 101)
        listing = (await client.get("/api/v1/duel/opponents", headers=session)).json()
        assert listing["ineligibility"] == "active_duel"
        assert [row["user_id"] for row in listing["opponents"]] == [202]
        assert (await client.get("/api/v1/players/202", headers=session)).status_code == 200
