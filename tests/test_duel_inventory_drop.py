import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


CHAT_ID = -5151


def test_empty_inventory_skips_drop_roll_and_choice(monkeypatch):
    from handlers import duel

    roll = Mock(side_effect=AssertionError("empty inventory consumed drop roll"))
    choice = Mock(side_effect=AssertionError("empty inventory selected item"))
    monkeypatch.setattr(duel, "get_duel_inventory", lambda *_args: [])
    monkeypatch.setattr(duel.random, "random", roll)
    monkeypatch.setattr(duel.random, "choice", choice)

    assert duel._maybe_drop_loser_inventory_item(CHAT_ID, 2) is None
    roll.assert_not_called()
    choice.assert_not_called()


def test_nonempty_inventory_miss_does_not_choose_or_delete(monkeypatch):
    from handlers import duel

    inventory = [{"id": 7, "item_id": "vevangel_wing"}]
    choice = Mock(side_effect=AssertionError("miss selected item"))
    remove = Mock(side_effect=AssertionError("miss deleted item"))
    monkeypatch.setattr(duel, "get_duel_inventory", lambda *_args: inventory)
    monkeypatch.setattr(duel.random, "random", Mock(return_value=duel.DUEL_ITEM_DROP_CHANCE))
    monkeypatch.setattr(duel.random, "choice", choice)
    monkeypatch.setattr(duel, "remove_duel_inventory_instance", remove)

    assert duel._maybe_drop_loser_inventory_item(CHAT_ID, 2) is None
    choice.assert_not_called()
    remove.assert_not_called()


def test_drop_removes_one_loser_duplicate_only_and_creates_no_event(temp_database):
    import database as db
    from handlers import duel

    winner_id = 1
    loser_id = 2
    db.add_duel_inventory_item(CHAT_ID, winner_id, "rat_knuckle")
    db.add_duel_inventory_item(CHAT_ID, loser_id, "vevangel_wing")
    db.add_duel_inventory_item(CHAT_ID, loser_id, "vevangel_wing")
    db.add_duel_inventory_item(CHAT_ID, loser_id, "formangnome_whisker")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(duel.random, "random", Mock(return_value=0.0))
        monkeypatch.setattr(duel.random, "choice", lambda values: values[0])
        assert duel._maybe_drop_loser_inventory_item(CHAT_ID, loser_id) == (
            "Крыло Вевангела"
        )

    assert [item["item_id"] for item in db.get_duel_inventory(CHAT_ID, loser_id)] == [
        "vevangel_wing",
        "formangnome_whisker",
    ]
    assert [item["item_id"] for item in db.get_duel_inventory(CHAT_ID, winner_id)] == [
        "rat_knuckle"
    ]
    with sqlite3.connect(temp_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM duel_item_events").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_finish_duel_appends_item_loss_after_berserk_and_post_message(
    monkeypatch,
    fake_context,
):
    from handlers import duel, duel_text

    winner = {"user_id": 1, "username": "winner", "points": 20}
    loser = {
        "user_id": 2,
        "username": "loser",
        "points": 20,
        "daily_wins": 0,
    }
    inventory = [{"id": 99, "item_id": "vevangel_wing"}]
    post_catalog = ("post <message> & tail",)
    events = []
    rolls = iter(
        (
            ("regular_steal_roll", 0.99),
            ("berserk_roll", 0.0),
            ("post_message_roll", 0.0),
            ("item_drop_roll", 0.0),
        )
    )

    def random_roll():
        name, value = next(rolls)
        events.append(name)
        return value

    def choose(values):
        if values is post_catalog:
            events.append("post_message_choice")
            return values[0]
        if values is inventory:
            events.append("item_instance_choice")
            return values[0]
        if isinstance(values, tuple):
            events.append("berserker_choice")
            return values[0]
        if values is duel_text.BERSERK_TRIGGERS:
            events.append("berserk_trigger_choice")
            return values[0]
        if values is duel_text.BERSERK_RESULTS:
            events.append("berserk_result_choice")
            return values[0]
        events.append("round_flavor_choice")
        return values[0]

    monkeypatch.setattr(duel, "DUEL_POST_MESSAGES", post_catalog)
    monkeypatch.setattr(duel.random, "random", random_roll)
    monkeypatch.setattr(duel.random, "choice", choose)
    monkeypatch.setattr(duel, "apply_duel_result_plan", lambda *_args: (30, 15))
    monkeypatch.setattr(
        duel,
        "apply_duel_berserk",
        lambda *_args: events.append("berserk_applied") or True,
    )
    monkeypatch.setattr(duel, "get_duel_inventory", lambda *_args: inventory)
    remove = Mock(return_value=True)
    monkeypatch.setattr(duel, "remove_duel_inventory_instance", remove)
    fake_context.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=808))

    await duel._finish_duel(
        fake_context,
        CHAT_ID,
        winner,
        loser,
        custom_text="Финал.\n",
    )

    assert events == [
        "regular_steal_roll",
        "round_flavor_choice",
        "berserk_roll",
        "berserker_choice",
        "berserk_applied",
        "berserk_trigger_choice",
        "berserk_result_choice",
        "post_message_roll",
        "post_message_choice",
        "item_drop_roll",
        "item_instance_choice",
    ]
    remove.assert_called_once_with(CHAT_ID, loser["user_id"], 99)
    output = fake_context.bot.send_message.await_args.kwargs["text"]
    assert "post &lt;message&gt; &amp; tail" in output
    assert output.endswith(
        "\n\n<b>Карман порвался, выпало:</b> Крыло Вевангела"
    )
    assert output.index("На теле проигравшего") < output.index("Карман порвался")
    assert output.index("БЕРСЕРК") < output.index("Карман порвался")
