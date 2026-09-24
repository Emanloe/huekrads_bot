"""The Telegram item claim owns the one Huegryz roll."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

import database as db
from config import HUEGRYZ_CHANCE
from handlers import duel_items
from tests.test_duel_items import item_event_update, make_user


CHAT_ID = -771


def _event(source="scheduled"):
    if source == "pocket":
        owner = make_user(99, "owner")
        db.get_or_create_duel_user(owner, CHAT_ID)
        item = db.add_duel_inventory_item(CHAT_ID, owner.id, "rat_knuckle")
        event_id = db.create_duel_item_event_from_inventory(
            CHAT_ID, owner.id, item["id"]
        )["event_id"]
    else:
        event_id = db.create_duel_item_event(CHAT_ID)
        if source == "dig":
            with db.get_db() as conn:
                conn.execute("UPDATE duel_item_events SET item_id = ? WHERE event_id = ?",
                             ("rat_knuckle", event_id))
    assert db.set_duel_item_event_message(event_id, 777)
    return event_id


def _state(user_id):
    with db.get_db() as conn:
        return conn.execute(
            """SELECT points, wins, losses, daily_wins, stolen_dicks_count,
                      dick_stolen_count, dick_stolen_today, last_stolen_by
               FROM duel_users WHERE chat_id = ? AND user_id = ?""",
            (CHAT_ID, user_id),
        ).fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ("scheduled", "dig", "pocket"))
async def test_pickup_miss_keeps_item_and_dick_across_sources(
    source, temp_database, fake_context, monkeypatch,
):
    claimant = make_user(1, "claimant")
    db.get_or_create_duel_user(claimant, CHAT_ID)
    event_id = _event(source)
    before = _state(claimant.id)
    roll = Mock(return_value=HUEGRYZ_CHANCE)
    monkeypatch.setattr(duel_items.random, "random", roll)
    monkeypatch.setattr(duel_items.random, "choice", Mock(return_value=duel_items.DUEL_ITEMS[0]))

    await duel_items.duel_item_event_callback(
        item_event_update(CHAT_ID, claimant, event_id)[0], fake_context
    )

    roll.assert_called_once_with()
    assert len(db.get_duel_inventory(CHAT_ID, claimant.id)) == 1
    assert _state(claimant.id) == before
    assert "хуегрыз" not in fake_context.bot.edit_message_text.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_hit_changes_only_personal_dick_loss_and_keeps_item(
    temp_database, fake_context, monkeypatch,
):
    claimant, other = make_user(1, "claimant"), make_user(2, "other")
    for user in (claimant, other):
        db.get_or_create_duel_user(user, CHAT_ID)
    event_id = _event()
    before, other_before = _state(claimant.id), _state(other.id)
    monthly_before = db.get_monthly_chat_stats(CHAT_ID, db.moscow_month_key())
    roll = Mock(return_value=0.0)
    monkeypatch.setattr(duel_items.random, "random", roll)
    monkeypatch.setattr(duel_items.random, "choice", Mock(return_value=duel_items.DUEL_ITEMS[0]))
    update, _ = item_event_update(CHAT_ID, claimant, event_id)

    await duel_items.duel_item_event_callback(update, fake_context)

    roll.assert_called_once_with()
    assert len(db.get_duel_inventory(CHAT_ID, claimant.id)) == 1
    assert _state(claimant.id) == before[:5] + (before[5] + 1, 1, None)
    assert _state(other.id) == other_before
    assert db.get_monthly_chat_stats(CHAT_ID, db.moscow_month_key()) == monthly_before
    text = fake_context.bot.edit_message_text.await_args.kwargs["text"]
    assert "Найдено:" in text and "хуегрыз" in text and "хуй" in text

    await duel_items.duel_item_event_callback(update, fake_context)
    roll.assert_called_once_with()
    assert _state(claimant.id)[5] == before[5] + 1


@pytest.mark.asyncio
async def test_hit_on_dickless_claimant_is_sad_noop(
    temp_database, fake_context, monkeypatch,
):
    claimant = make_user(1, "claimant")
    db.get_or_create_duel_user(claimant, CHAT_ID)
    with db.get_db() as conn:
        conn.execute("UPDATE duel_users SET dick_stolen_today = 1, last_stolen_by = ? "
                     "WHERE chat_id = ? AND user_id = ?",
                     ("previous_thief", CHAT_ID, claimant.id))
    before = _state(claimant.id)
    event_id = _event()
    roll = Mock(return_value=0.0)
    monkeypatch.setattr(duel_items.random, "random", roll)
    monkeypatch.setattr(duel_items.random, "choice", Mock(return_value=duel_items.DUEL_ITEMS[0]))

    await duel_items.duel_item_event_callback(
        item_event_update(CHAT_ID, claimant, event_id)[0], fake_context
    )

    roll.assert_called_once_with()
    assert _state(claimant.id) == before
    assert len(db.get_duel_inventory(CHAT_ID, claimant.id)) == 1
    text = fake_context.bot.edit_message_text.await_args.kwargs["text"]
    assert "хуегрыз" in text and "грустно" in text


@pytest.mark.asyncio
async def test_unregistered_loser_and_repeated_claim_have_no_huegryz_rng(
    temp_database, fake_context, monkeypatch,
):
    winner, loser, outsider = (make_user(i, name) for i, name in
                              ((1, "winner"), (2, "loser"), (3, "outsider")))
    for user in (winner, loser):
        db.get_or_create_duel_user(user, CHAT_ID)
    event_id = _event()
    roll = Mock(return_value=1.0)
    monkeypatch.setattr(duel_items.random, "random", roll)
    monkeypatch.setattr(duel_items.random, "choice", Mock(return_value=duel_items.DUEL_ITEMS[0]))

    await duel_items.duel_item_event_callback(item_event_update(CHAT_ID, outsider, event_id)[0], fake_context)
    roll.assert_not_called()
    await duel_items.duel_item_event_callback(item_event_update(CHAT_ID, winner, event_id)[0], fake_context)
    roll.assert_called_once_with()
    await duel_items.duel_item_event_callback(item_event_update(CHAT_ID, loser, event_id)[0], fake_context)
    await duel_items.duel_item_event_callback(item_event_update(CHAT_ID, winner, event_id)[0], fake_context)
    roll.assert_called_once_with()
    assert db.get_duel_inventory(CHAT_ID, loser.id) == []


def test_concurrent_claim_has_one_item_and_one_huegryz_roll(temp_database):
    for user_id in (1, 2):
        db.get_or_create_duel_user(make_user(user_id, f"user{user_id}"), CHAT_ID)
    event_id = _event()
    roll = Mock(return_value=True)

    def claim(user_id):
        return db.claim_duel_item_event(event_id, CHAT_ID, user_id,
                                        lambda: "rat_knuckle", roll)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (1, 2)))

    assert sorted(result[0] for result in results) == ["already_claimed", "claimed"]
    roll.assert_called_once_with()
    winner = next(result[1]["user_id"] for result in results if result[0] == "claimed")
    loser = 1 if winner == 2 else 2
    assert _state(winner)[5:7] == (1, 1)
    assert _state(loser)[5:7] == (0, 0)
    assert len(db.get_duel_inventory(CHAT_ID, winner)) == 1
    assert db.get_duel_inventory(CHAT_ID, loser) == []


def test_failed_inventory_insert_rolls_back_claim_without_huegryz_roll(temp_database):
    claimant = make_user(1, "claimant")
    db.get_or_create_duel_user(claimant, CHAT_ID)
    event_id = _event()
    with db.get_db() as conn:
        conn.execute("""CREATE TRIGGER reject_item BEFORE INSERT ON duel_inventory
                        BEGIN SELECT RAISE(ABORT, 'blocked'); END""")
    roll = Mock(return_value=True)

    with pytest.raises(Exception, match="blocked"):
        db.claim_duel_item_event(event_id, CHAT_ID, claimant.id,
                                 lambda: "rat_knuckle", roll)

    roll.assert_not_called()
    assert db.get_duel_item_event(event_id)["claimed"] is False
    assert db.get_duel_inventory(CHAT_ID, claimant.id) == []
    assert _state(claimant.id)[5:7] == (0, 0)


def test_failed_huegryz_update_rolls_back_item_and_claim(temp_database):
    claimant = make_user(1, "claimant")
    db.get_or_create_duel_user(claimant, CHAT_ID)
    event_id = _event()
    with db.get_db() as conn:
        conn.execute("""CREATE TRIGGER reject_bite BEFORE UPDATE OF dick_stolen_today
                        ON duel_users BEGIN SELECT RAISE(ABORT, 'blocked'); END""")
    roll = Mock(return_value=True)

    with pytest.raises(Exception, match="blocked"):
        db.claim_duel_item_event(event_id, CHAT_ID, claimant.id,
                                 lambda: "rat_knuckle", roll)

    roll.assert_called_once_with()
    assert db.get_duel_item_event(event_id)["claimed"] is False
    assert db.get_duel_inventory(CHAT_ID, claimant.id) == []
    assert _state(claimant.id)[5:7] == (0, 0)
