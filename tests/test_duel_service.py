"""Server-side chat isolation for future Telegram and Mini App duel adapters."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import database
from handlers.duel_service import get_duel_profile, list_duel_opponents


CHAT_A = -7101
CHAT_B = -7102


def user(user_id: int, username: str) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, username=username, first_name=username, is_bot=False)


def set_state(path, chat_id: int, user_id: int, points: int, no_dick: bool = False):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE duel_users SET points = ?, dick_stolen_today = ? "
            "WHERE chat_id = ? AND user_id = ?",
            (points, int(no_dick), chat_id, user_id),
        )


def test_profile_is_scoped_by_chat_and_does_not_register_missing_user(temp_database):
    same_user = user(1, "same_user")
    database.get_or_create_duel_user(same_user, CHAT_A)
    database.get_or_create_duel_user(same_user, CHAT_B)
    set_state(temp_database, CHAT_A, 1, 0, no_dick=True)
    set_state(temp_database, CHAT_B, 1, 35)
    database.add_duel_inventory_item(CHAT_A, 1, "po_lochki")
    database.add_duel_inventory_item(CHAT_B, 1, "you_mom")

    profile_a = get_duel_profile(CHAT_A, 1)
    profile_b = get_duel_profile(CHAT_B, 1)
    assert (profile_a.user["chat_id"], profile_a.user["points"], profile_a.ineligibility) == (
        CHAT_A, 0, "no_dick",
    )
    assert (profile_b.user["chat_id"], profile_b.user["points"], profile_b.ineligibility) == (
        CHAT_B, 35, None,
    )
    assert [item["item_id"] for item in profile_a.inventory] == ["po_lochki"]
    assert [item["item_id"] for item in profile_b.inventory] == ["you_mom"]
    assert get_duel_profile(CHAT_A, 999) is None
    assert not database.is_duel_user_registered(CHAT_A, 999)


def test_opponent_list_contains_only_eligible_users_from_requested_chat(temp_database):
    for chat_id, players in (
        (CHAT_A, ((1, "initiator"), (2, "chat_a_opponent"), (4, "zero_point_opponent"))),
        (CHAT_B, ((1, "initiator"), (3, "chat_b_opponent"))),
    ):
        for user_id, username in players:
            database.get_or_create_duel_user(user(user_id, username), chat_id)
    set_state(temp_database, CHAT_A, 4, 0)

    list_a = list_duel_opponents(CHAT_A, 1)
    list_b = list_duel_opponents(CHAT_B, 1)
    assert list_a.ineligibility is None
    assert [(item.user_id, item.username) for item in list_a.opponents] == [
        (2, "chat_a_opponent"),
        (4, "zero_point_opponent"),
    ]
    assert [(item.user_id, item.username) for item in list_b.opponents] == [
        (3, "chat_b_opponent"),
    ]
    assert list_duel_opponents(CHAT_A, 3).ineligibility == "not_registered"
    assert not database.is_duel_user_registered(CHAT_A, 3)


def test_opponent_list_uses_own_chat_points_and_dick_state(temp_database):
    for chat_id in (CHAT_A, CHAT_B):
        database.get_or_create_duel_user(user(1, "initiator"), chat_id)
        database.get_or_create_duel_user(user(2, "opponent"), chat_id)
    set_state(temp_database, CHAT_A, 1, 0, no_dick=True)
    set_state(temp_database, CHAT_B, 1, 20)
    set_state(temp_database, CHAT_A, 2, 0)
    set_state(temp_database, CHAT_B, 2, 20)

    denied = list_duel_opponents(CHAT_A, 1)
    allowed = list_duel_opponents(CHAT_B, 1)
    assert denied.ineligibility == "no_dick"
    assert denied.opponents == []
    assert [opponent.user_id for opponent in allowed.opponents] == [2]

    set_state(temp_database, CHAT_A, 1, 20)
    assert [opponent.user_id for opponent in list_duel_opponents(CHAT_A, 1).opponents] == [2]
    assert [opponent.user_id for opponent in list_duel_opponents(CHAT_B, 1).opponents] == [2]


@pytest.mark.asyncio
async def test_direct_duel_cannot_target_user_registered_only_in_other_chat(
    temp_database, fake_context, monkeypatch,
):
    from handlers import duel

    initiator = user(1, "initiator")
    database.get_or_create_duel_user(initiator, CHAT_A)
    database.get_or_create_duel_user(user(2, "other_chat_only"), CHAT_B)
    start_fight = AsyncMock()
    random_choice = Mock(side_effect=AssertionError("cross-chat admission used RNG"))
    monkeypatch.setattr(duel, "_start_interactive_fight", start_fight)
    monkeypatch.setattr(duel.random, "choice", random_choice)

    await duel._process_duel_fight(fake_context, initiator, "other_chat_only", CHAT_A)

    start_fight.assert_not_awaited()
    random_choice.assert_not_called()
    assert CHAT_A not in duel.ACTIVE_DUELS
    assert database.get_duel_user_by_id(CHAT_A, 2) is None
    assert database.get_duel_user_by_id(CHAT_B, 2)["username"] == "other_chat_only"
    assert "other_chat_only" in fake_context.bot.send_message.await_args.args[1]
