from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def participant(user_id, *, alive=True, title=None):
    return {
        "tg_user": SimpleNamespace(id=user_id),
        "data": {
            "user_id": user_id,
            "username": None,
            "display_name": title or f"user{user_id}",
        },
        "alive": alive,
    }


def battle(*participants):
    return {"participants": {item["tg_user"].id: item for item in participants}}


def test_boss_loot_with_no_survivors_consumes_no_rng(monkeypatch):
    from handlers import duel

    random_roll = Mock(side_effect=AssertionError("no survivors consumed loot roll"))
    choice = Mock(side_effect=AssertionError("no survivors selected loot"))
    add = Mock(side_effect=AssertionError("no survivors received loot"))
    monkeypatch.setattr(duel.random, "random", random_roll)
    monkeypatch.setattr(duel.random, "choice", choice)
    monkeypatch.setattr(duel, "add_duel_inventory_item", add)

    assert duel._maybe_award_boss_item(-1, battle(participant(1, alive=False))) is None
    random_roll.assert_not_called()
    choice.assert_not_called()
    add.assert_not_called()


def test_boss_loot_miss_does_not_select_survivor_or_item(monkeypatch):
    from handlers import duel

    choice = Mock(side_effect=AssertionError("boss loot miss selected value"))
    add = Mock(side_effect=AssertionError("boss loot miss added item"))
    monkeypatch.setattr(duel.random, "random", Mock(return_value=duel.BOSS_ITEM_DROP_CHANCE))
    monkeypatch.setattr(duel.random, "choice", choice)
    monkeypatch.setattr(duel, "add_duel_inventory_item", add)

    assert duel._maybe_award_boss_item(-2, battle(participant(1))) is None
    choice.assert_not_called()
    add.assert_not_called()


def test_boss_loot_hit_selects_one_living_survivor_then_full_catalog(monkeypatch):
    from handlers import duel

    living = participant(1, title="<living & dwarf>")
    dead = participant(2, alive=False, title="dead")
    catalog = ({"id": "unsafe", "name": "<loot & relic>"},)
    choices = []

    def choose(values):
        choices.append(values)
        return values[0]

    add = Mock(return_value={"id": 99})
    monkeypatch.setattr(duel, "DUEL_ITEMS", catalog)
    monkeypatch.setattr(duel.random, "random", Mock(return_value=0.0))
    monkeypatch.setattr(duel.random, "choice", choose)
    monkeypatch.setattr(duel, "add_duel_inventory_item", add)

    text = duel._maybe_award_boss_item(-3, battle(living, dead))

    assert choices == [[living], catalog]
    add.assert_called_once_with(-3, living["tg_user"].id, "unsafe")
    assert text == (
        "<b>В брюхе босса нашли:</b> &lt;loot &amp; relic&gt;\n"
        "Добычу забирает <b>&lt;living &amp; dwarf&gt;</b>."
    )


def test_boss_loot_uses_the_shared_nonempty_collectible_catalog():
    from handlers import duel, duel_items

    assert duel.DUEL_ITEMS is duel_items.DUEL_ITEMS
    assert duel.DUEL_ITEMS
    assert {"oiled_vest", "knife"}.isdisjoint(
        item["id"] for item in duel.DUEL_ITEMS
    )


@pytest.mark.asyncio
async def test_boss_report_finishes_old_presentation_before_loot_and_appends_last(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    events = []
    current_battle = {
        "participants": {},
        "message_id": 700,
    }

    def report(_battle, _victory):
        events.append("existing_report_and_rng")
        return "existing boss report"

    def loot(_chat_id, _battle):
        events.append("boss_item_stage")
        return "<b>В брюхе босса нашли:</b> Крыло Вевангела\nДобычу забирает <b>survivor</b>."

    monkeypatch.setattr(duel, "_boss_final_report", report)
    monkeypatch.setattr(duel, "_maybe_award_boss_item", loot)
    await duel._boss_send_final_report(fake_context, -4, current_battle, victory=True)

    assert events == ["existing_report_and_rng", "boss_item_stage"]
    output = fake_context.bot.edit_message_text.await_args.kwargs["text"]
    assert output.startswith("existing boss report\n\n")
    assert output.endswith("Добычу забирает <b>survivor</b>.")
