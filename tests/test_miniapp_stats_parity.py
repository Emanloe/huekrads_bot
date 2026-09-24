"""Telegram /duel_stats and Mini App profile/opponents share canonical reads."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import database
from config import MAX_DAILY_POINTS
from handlers import duel, duel_service
from handlers.duel_items import BASE_DUEL_ITEMS, format_duel_display_inventory
from handlers.duel_text import (
    get_duel_title_read_model, get_loss_title, get_stolen_dicks_title, get_win_title,
)
from miniapp_api import create_miniapp_api
from tests.test_miniapp_api import CHAT_A, CHAT_B, register, session_for
from tests.test_miniapp_auth import TEST_BOT_TOKEN
from text_resources import get_text


def set_stats(chat_id, user_id, *, points, wins, losses, stolen, bosses,
              no_dick=False, daily_wins=0):
    with database.get_db() as conn:
        conn.execute(
            """UPDATE duel_users SET points = ?, wins = ?, losses = ?,
               stolen_dicks_count = ?, bosses_defeated = ?,
               dick_stolen_today = ?, daily_wins = ?
               WHERE chat_id = ? AND user_id = ?""",
            (points, wins, losses, stolen, bosses, int(no_dick), daily_wins,
             chat_id, user_id),
        )


def no_rng(*_args, **_kwargs):
    raise AssertionError("A Mini App stats read used gameplay RNG")


@pytest.mark.asyncio
async def test_me_matches_real_duel_stats_including_base_inventory_and_pet(
    temp_database, fake_context, monkeypatch,
):
    register(CHAT_A, 101, "stats_user")
    set_stats(CHAT_A, 101, points=100, wins=138, losses=119, stolen=26,
              bosses=3, daily_wins=6)
    database.add_duel_inventory_item(CHAT_A, 101, "ceremonial_bolt")
    database.add_duel_inventory_item(CHAT_A, 101, "ceremonial_bolt")
    database.add_duel_inventory_item(CHAT_A, 101, "cork_with_bite_marks")
    # A legacy stored base instance must not duplicate a permanent base item.
    database.add_duel_inventory_item(CHAT_A, 101, "knife")
    with database.get_db() as conn:
        conn.execute("INSERT INTO huecrab_owners (chat_id, user_id) VALUES (?, ?)",
                     (CHAT_A, 101))

    sent = AsyncMock()
    monkeypatch.setattr(duel, "send_and_schedule", sent)
    telegram_user = SimpleNamespace(id=101, username="stats_user", first_name="stats_user",
                                    last_name=None, is_bot=False)
    update = SimpleNamespace(message=SimpleNamespace(
        from_user=telegram_user, chat=SimpleNamespace(id=CHAT_A), chat_id=CHAT_A,
    ))
    await duel.duel_stats_command(update, fake_context)
    telegram_text = sent.await_args.args[2]

    monkeypatch.setattr(duel_service, "random", SimpleNamespace(random=no_rng, choice=no_rng))
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        headers = await session_for(client, CHAT_A, 101)
        me = (await client.get("/api/v1/me", headers=headers)).json()
        assert (await client.get("/api/v1/me", headers=headers)).json() == me

    assert (me["points"], me["max_points"], me["wins"], me["losses"]) == (
        100, MAX_DAILY_POINTS, 138, 119,
    )
    assert (me["daily_wins"], me["boss_wins"], me["dick_status"]) == (
        6, 3, {"has_dick": True, "text": get_text("duel.stats.status.has_dick")},
    )
    assert f'Очки: <b>{me["points"]} / {me["max_points"]}</b>' in telegram_text
    assert f'Побед: <b>{me["wins"]}</b>' in telegram_text
    assert f'Поражений: <b>{me["losses"]}</b>' in telegram_text
    assert f'Побеждено боссов: <b>{me["boss_wins"]}</b>' in telegram_text
    assert f'Статус на сегодня: <b>{me["dick_status"]["text"]}</b>' in telegram_text
    for category in ("wins", "losses", "stolen_dicks"):
        title = me["titles"][category]
        assert get_text("duel.stats.title_item", title=title["text"],
                        count=title["count"]) in telegram_text
    assert [item["item_id"] for item in me["inventory"]] == [
        "oiled_vest", "knife", "cork_with_bite_marks", "ceremonial_bolt",
    ]
    assert me["inventory"][:2] == [
        {"item_id": "oiled_vest", "name": "Промасленная жилетка", "count": 1},
        {"item_id": "knife", "name": "Нож", "count": 1},
    ]
    assert me["inventory"][-1]["count"] == 2
    assert format_duel_display_inventory(database.get_duel_inventory(CHAT_A, 101)) in telegram_text
    assert me["pet"] == get_text("huecrab.inventory")
    assert me["pet"] in telegram_text
    assert [item["id"] for item in BASE_DUEL_ITEMS] == ["oiled_vest", "knife"]
    assert [item["item_id"] for item in database.get_duel_inventory(CHAT_A, 101)] == [
        "ceremonial_bolt", "ceremonial_bolt", "cork_with_bite_marks", "knife",
    ]


@pytest.mark.parametrize("wins,losses,stolen", [
    (99, 99, 9), (100, 100, 10), (199, 199, 19), (200, 200, 20),
])
def test_three_title_categories_follow_independent_canonical_thresholds(
    wins, losses, stolen,
):
    titles = get_duel_title_read_model({
        "wins": wins, "losses": losses, "stolen_dicks_count": stolen,
    })
    assert titles == {
        "wins": {"text": get_win_title(wins), "count": wins},
        "losses": {"text": get_loss_title(losses), "count": losses},
        "stolen_dicks": {"text": get_stolen_dicks_title(stolen), "count": stolen},
    }
    assert (titles["wins"]["text"] is not None) == (wins >= 100)
    assert (titles["losses"]["text"] is not None) == (losses >= 100)
    assert (titles["stolen_dicks"]["text"] is not None) == (stolen >= 10)


@pytest.mark.asyncio
async def test_me_and_opponent_titles_are_chat_scoped_with_zero_point_eligibility(
    temp_database, monkeypatch,
):
    for chat_id in (CHAT_A, CHAT_B):
        register(chat_id, 101, "viewer")
        register(chat_id, 202, "shared_opponent")
    register(CHAT_A, 303, "dickless_a")
    register(CHAT_B, 303, "dickless_b")
    set_stats(CHAT_A, 101, points=21, wins=101, losses=0, stolen=0, bosses=4)
    set_stats(CHAT_B, 101, points=52, wins=0, losses=111, stolen=20, bosses=1,
              no_dick=True)
    set_stats(CHAT_A, 202, points=0, wins=138, losses=119, stolen=26, bosses=0)
    set_stats(CHAT_B, 202, points=0, wins=3, losses=0, stolen=0, bosses=0)
    set_stats(CHAT_B, 303, points=10, wins=999, losses=0, stolen=0,
              bosses=0, no_dick=True)
    set_stats(CHAT_A, 303, points=10, wins=999, losses=0, stolen=0,
              bosses=0, no_dick=True)
    database.add_duel_inventory_item(CHAT_A, 101, "ceremonial_bolt")
    database.add_duel_inventory_item(CHAT_B, 101, "cork_with_bite_marks")
    monkeypatch.setattr(duel_service, "random", SimpleNamespace(random=no_rng, choice=no_rng))

    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        a = await session_for(client, CHAT_A, 101)
        b = await session_for(client, CHAT_B, 101)
        me_a = (await client.get(
            f"/api/v1/me?chat_id={CHAT_B}&user_id=202",
            headers={**a, "X-Chat-Id": str(CHAT_B), "X-User-Id": "202"},
        )).json()
        me_b = (await client.get("/api/v1/me", headers=b)).json()
        assert (me_a["points"], me_a["boss_wins"]) == (21, 4)
        assert (me_b["points"], me_b["boss_wins"]) == (52, 1)
        assert me_a["titles"]["wins"]["text"] is not None
        assert me_b["titles"]["wins"]["text"] is None
        assert me_b["titles"]["losses"]["text"] is not None
        assert me_a["dick_status"]["has_dick"] is True
        assert me_b["dick_status"] == {
            "has_dick": False, "text": get_text("duel.stats.status.no_dick"),
        }
        assert me_a["inventory"][-1]["item_id"] == "ceremonial_bolt"
        assert me_b["inventory"][-1]["item_id"] == "cork_with_bite_marks"
        opponents_a = (await client.get(
            f"/api/v1/duel/opponents?chat_id={CHAT_B}",
            headers={**a, "X-Chat-Id": str(CHAT_B)},
        )).json()
        assert [item["user_id"] for item in opponents_a["opponents"]] == [202]
        opponent_a = opponents_a["opponents"][0]
        assert opponent_a["points"] == 0
        assert (opponent_a["wins"], opponent_a["losses"]) == (138, 119)
        assert opponent_a["titles"] == get_duel_title_read_model(
            database.get_duel_user_by_id(CHAT_A, 202, read_only=True)
        )
        assert all(opponent_a["titles"][key]["text"] for key in (
            "wins", "losses", "stolen_dicks",
        ))
        assert "inventory" not in opponent_a and "boss_wins" not in opponent_a
        # The blocked challenger still sees no eligible list in B.
        opponents_b = (await client.get("/api/v1/duel/opponents", headers=b)).json()
        assert opponents_b == {"ineligibility": "no_dick", "opponents": []}


@pytest.mark.asyncio
async def test_opponent_titles_differ_across_chats_without_changing_admission(
    temp_database, monkeypatch,
):
    for chat_id in (CHAT_A, CHAT_B):
        register(chat_id, 101, "viewer")
        register(chat_id, 202, "same_opponent")
    set_stats(CHAT_A, 202, points=0, wins=100, losses=0, stolen=0, bosses=0)
    set_stats(CHAT_B, 202, points=0, wins=0, losses=100, stolen=10, bosses=0)
    monkeypatch.setattr(duel_service, "random", SimpleNamespace(random=no_rng, choice=no_rng))
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        a = await session_for(client, CHAT_A, 101)
        b = await session_for(client, CHAT_B, 101)
        row_a = (await client.get("/api/v1/duel/opponents", headers=a)).json()["opponents"][0]
        row_b = (await client.get("/api/v1/duel/opponents", headers=b)).json()["opponents"][0]
        assert row_a["user_id"] == row_b["user_id"] == 202
        assert row_a["titles"]["wins"]["text"] is not None
        assert row_a["titles"]["losses"]["text"] is None
        assert row_b["titles"]["wins"]["text"] is None
        assert row_b["titles"]["losses"]["text"] is not None
        assert row_b["titles"]["stolen_dicks"]["text"] is not None
        assert (await client.get("/api/v1/duel/opponents", headers=b)).json()[
            "opponents"
        ][0] == row_b
