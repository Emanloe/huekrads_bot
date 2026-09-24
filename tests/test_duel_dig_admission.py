"""Fresh duel admission after spending points on /dig."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


CHAT_ID = -9901


def _user(user_id, username):
    return SimpleNamespace(
        id=user_id, username=username, first_name=username, last_name=None, is_bot=False,
    )


def _duel_update(user):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=CHAT_ID), chat_id=CHAT_ID, from_user=user,
        message_id=71, text="/duel", reply_text=AsyncMock(
            return_value=SimpleNamespace(message_id=72),
        ),
    )
    return SimpleNamespace(message=message, effective_chat=SimpleNamespace(id=CHAT_ID))


def _set_points(path, user_id, points, *, no_dick=False):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE duel_users SET points = ?, dick_stolen_today = ? "
            "WHERE chat_id = ? AND user_id = ?",
            (points, int(no_dick), CHAT_ID, user_id),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("points,no_dick,expected", [
    (0, False, "duel.admission.initiator.no_points"),
    (-3, False, "duel.admission.initiator.no_points"),
    (0, True, "duel.command.no_dick"),
])
async def test_duel_list_denies_ineligible_initiator_before_selection_or_rng(
    temp_database, fake_context, monkeypatch, points, no_dick, expected,
):
    import database
    from handlers import duel, duel_service
    from text_resources import get_text

    initiator = _user(1, "initiator")
    database.get_or_create_duel_user(initiator, CHAT_ID)
    _set_points(temp_database, 1, points, no_dick=no_dick)
    monkeypatch.setattr(duel_service, "get_duel_top", Mock(side_effect=AssertionError("opened selection")))
    monkeypatch.setattr(duel.random, "choice", Mock(side_effect=AssertionError("used RNG")))
    update = _duel_update(initiator)

    await duel.duel_command(update, fake_context)

    assert update.message.reply_text.await_args.args[0] == get_text(expected)
    assert CHAT_ID not in duel.ACTIVE_DUELS


@pytest.mark.asyncio
async def test_old_selection_button_rechecks_current_db_points_without_rng(
    temp_database, fake_context, monkeypatch,
):
    import database
    from handlers import duel
    monkeypatch.setattr(duel, "_process_persistent_duel_fight", duel._process_duel_fight)

    initiator, opponent = _user(1, "initiator"), _user(2, "opponent")
    database.get_or_create_duel_user(initiator, CHAT_ID)
    database.get_or_create_duel_user(opponent, CHAT_ID)
    selection = _duel_update(initiator)
    await duel.duel_command(selection, fake_context)
    markup = selection.message.reply_text.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "start_duel_opponent"

    _set_points(temp_database, 1, 0)
    rng = Mock(side_effect=AssertionError("rejected duel used RNG"))
    start_fight = AsyncMock()
    monkeypatch.setattr(duel.random, "choice", rng)
    monkeypatch.setattr(duel, "_start_interactive_fight", start_fight)
    query = SimpleNamespace(
        data="start_duel_opponent", from_user=initiator,
        answer=AsyncMock(), message=SimpleNamespace(delete=AsyncMock()),
    )
    callback = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=CHAT_ID))
    fake_context.bot.send_message.reset_mock()

    await duel.duel_select_callback(callback, fake_context)

    query.answer.assert_awaited_once()
    start_fight.assert_not_awaited()
    rng.assert_not_called()
    assert CHAT_ID not in duel.ACTIVE_DUELS
    assert fake_context.bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_completed_dig_from_exactly_ten_points_blocks_new_duel(
    temp_database, fake_context, monkeypatch,
):
    import database
    from handlers import dig, duel
    from text_resources import get_text

    initiator = _user(1, "initiator")
    database.get_or_create_duel_user(initiator, CHAT_ID)
    _set_points(temp_database, 1, 10)
    monkeypatch.setattr(dig.random, "random", Mock(return_value=1.0))
    dig_update = SimpleNamespace(
        effective_user=initiator, effective_chat=SimpleNamespace(id=CHAT_ID),
        message=SimpleNamespace(message_id=70),
    )
    await dig.dig_command(dig_update, fake_context)
    assert database.get_or_create_duel_user(initiator, CHAT_ID)["points"] == 0

    monkeypatch.setattr(duel.random, "choice", Mock(side_effect=AssertionError("used RNG")))
    duel_update = _duel_update(initiator)
    await duel.duel_command(duel_update, fake_context)
    assert duel_update.message.reply_text.await_args.args[0] == get_text(
        "duel.admission.initiator.no_points"
    )
    assert CHAT_ID not in duel.ACTIVE_DUELS
