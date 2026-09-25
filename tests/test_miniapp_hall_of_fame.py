"""The Mini App hall mirrors /duel_top without assigning avatars or writing game state."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import database
import gnome_avatars
import miniapp_api
from handlers import duel, duel_service
from miniapp_api import create_miniapp_api
from tests.test_miniapp_api import CHAT_A, CHAT_B, register, session_for
from tests.test_miniapp_auth import TEST_BOT_TOKEN


def set_profile(chat_id, user_id, *, wins, losses, points, variant=None,
                display_name=None, dwarf_name=None, dickless=False):
    with database.get_db() as conn:
        conn.execute(
            """UPDATE duel_users SET wins = ?, losses = ?, points = ?,
                      gnome_variant = ?, display_name = COALESCE(?, display_name),
                      dwarf_name = ?, dick_stolen_today = ?
               WHERE chat_id = ? AND user_id = ?""",
            (wins, losses, points, variant, display_name, dwarf_name,
             int(dickless), chat_id, user_id),
        )


@pytest.mark.asyncio
async def test_hall_empty_matches_telegram_and_requires_session(temp_database, monkeypatch,
                                                               fake_context):
    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    update = SimpleNamespace(message=SimpleNamespace(
        chat=SimpleNamespace(id=CHAT_A), chat_id=CHAT_A,
    ))
    await duel.duel_top_command(update, fake_context)
    sent.assert_awaited_once_with(
        update, fake_context, "🏆 Таблица лидеров чата пока пуста.",
    )

    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        assert (await client.get("/api/v1/duel/hall-of-fame")).status_code == 401
        headers = await session_for(client, CHAT_A, 101)
        response = await client.get("/api/v1/duel/hall-of-fame", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"sort_by": "wins", "players": []}
        assert response.headers["cache-control"] == "no-store"
        assert (await client.post("/api/v1/duel/hall-of-fame", headers=headers)).status_code == 405


@pytest.mark.asyncio
async def test_hall_uses_canonical_top_order_limit_ties_and_telegram_fields(
    temp_database, monkeypatch, fake_context,
):
    for user_id in range(101, 113):
        register(CHAT_A, user_id, f"player_{user_id}")
        set_profile(CHAT_A, user_id, wins=user_id - 100, losses=20 - (user_id - 100),
                    points=user_id)
    # Equal wins are not reranked by points or losses. The shared SQL owns their order.
    set_profile(CHAT_A, 108, wins=9, losses=0, points=999,
                display_name="@<Лидер>", dwarf_name="Яйцебород", variant="gnome_07")
    set_profile(CHAT_A, 109, wins=9, losses=99, points=1, variant=None, dickless=True)
    canonical = database.get_duel_top(CHAT_A, limit=10, include_dwarf_name=True)
    canonical_model = database.get_duel_top_read_model(CHAT_A, limit=10)
    assert [(r["username"], r["display_name"], r["wins"], r["losses"],
             r["points"], r["dwarf_name"]) for r in canonical_model] == canonical
    assert len(canonical) == 10
    assert [r["wins"] for r in canonical_model] == sorted(
        [r["wins"] for r in canonical_model], reverse=True,
    )
    assert {r["user_id"] for r in canonical_model if r["wins"] == 9} == {108, 109}
    assert canonical_model[0]["user_id"] == 112

    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        headers = await session_for(client, CHAT_A, 101)
        response = await client.get("/api/v1/duel/hall-of-fame", headers=headers)
    assert response.status_code == 200
    players = response.json()["players"]
    assert [r["user_id"] for r in players] == [r["user_id"] for r in canonical_model]
    assert [(r["rank"], r["points"], r["wins"], r["losses"]) for r in players] == [
        (rank, row[4], row[2], row[3]) for rank, row in enumerate(canonical, 1)
    ]
    target = next(row for row in players if row["user_id"] == 108)
    assert target["title"] == "Яйцебород (<Лидер>)"
    assert target["gnome_image_url"] == gnome_avatars.gnome_image_url("gnome_07")
    assert next(row for row in players if row["user_id"] == 109)["gnome_image_url"] == (
        gnome_avatars.gnome_image_url("gnome_00")
    )
    assert "gnome_variant" not in target and "dwarf_name" not in target
    assert TEST_BOT_TOKEN not in response.text
    assert all(file_id not in response.text for file_id in gnome_avatars.GNOME_FILE_IDS.values())

    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    update = SimpleNamespace(message=SimpleNamespace(
        chat=SimpleNamespace(id=CHAT_A), chat_id=CHAT_A,
    ))
    await duel.duel_top_command(update, fake_context)
    telegram_text = sent.await_args.args[2]
    assert telegram_text.startswith("🏆 <b>Топ-10 гномьих дуэлянтов чата (по победам):</b>\n\n")
    assert "Яйцебород (&lt;Лидер&gt;)" in telegram_text
    assert telegram_text.count(" очков (") == 10


@pytest.mark.asyncio
async def test_hall_is_chat_scoped_read_only_zero_rng_and_uses_avatar_fallbacks(
    temp_database, monkeypatch, caplog,
):
    register(CHAT_A, 101, "viewer_a")
    register(CHAT_B, 101, "viewer_b")
    register(CHAT_A, 202, "shared_a")
    register(CHAT_B, 202, "shared_b")
    register(CHAT_B, 303, "only_b")
    set_profile(CHAT_A, 202, wins=9, losses=2, points=0,
                variant="gnome_02", dickless=True)
    set_profile(CHAT_B, 202, wins=8, losses=3, points=80, variant="gnome_08")
    set_profile(CHAT_B, 303, wins=7, losses=4, points=70, variant="corrupt")

    def forbidden(*args, **kwargs):
        raise AssertionError("hall must not mutate state or consume RNG")

    monkeypatch.setattr(gnome_avatars.secrets, "choice", forbidden)
    monkeypatch.setattr(miniapp_api, "get_or_assign_gnome_variant", forbidden)
    monkeypatch.setattr(duel_service.random, "random", forbidden)
    monkeypatch.setattr(duel_service.random, "choice", forbidden)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        headers_a = await session_for(client, CHAT_A, 101)
        headers_b = await session_for(client, CHAT_B, 101)
        before = temp_database.read_bytes()
        forged = {**headers_a, "X-Chat-Id": str(CHAT_B), "X-User-Id": "303"}
        for _ in range(10):
            a = await client.request(
                "GET", f"/api/v1/duel/hall-of-fame?chat_id={CHAT_B}&target_user_id=303",
                headers=forged, json={"chat_id": CHAT_B, "target_user_id": 303},
            )
            assert a.status_code == 200
            assert {row["user_id"] for row in a.json()["players"]} == {101, 202}
            assert next(row for row in a.json()["players"] if row["user_id"] == 202)[
                "gnome_image_url"] == gnome_avatars.gnome_image_url("gnome_02")
        b = await client.get("/api/v1/duel/hall-of-fame", headers=headers_b)
        assert b.status_code == 200
        assert {row["user_id"] for row in b.json()["players"]} == {101, 202, 303}
        assert next(row for row in b.json()["players"] if row["user_id"] == 202)[
            "gnome_image_url"] == gnome_avatars.gnome_image_url("gnome_08")
        assert next(row for row in b.json()["players"] if row["user_id"] == 303)[
            "gnome_image_url"] == gnome_avatars.gnome_image_url("gnome_00")
        assert temp_database.read_bytes() == before
    assert "Unknown persisted gnome variant" in caplog.text
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT chat_id, user_id, gnome_variant FROM duel_users ORDER BY chat_id, user_id",
        ).fetchall()
    assert rows == [
        (CHAT_B, 101, None), (CHAT_B, 202, "gnome_08"),
        (CHAT_B, 303, "corrupt"), (CHAT_A, 101, None),
        (CHAT_A, 202, "gnome_02"),
    ]
