"""Exactly-once terminal effects for dormant persistent ordinary duels."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

import database
from duel_session_repository import create_duel_session, get_duel_session
from handlers import duel, duel_service
from text_resources import get_text_list


CHAT = -9401
OTHER_CHAT = -9402
NOW = 1_800_000_000_000


def user(user_id):
    return SimpleNamespace(id=user_id, username=f"g{user_id}", first_name=f"G{user_id}")


def snapshot(user_id, *, points=20, daily_wins=0, dwarf_name=None):
    return {
        "user_id": user_id, "username": f"g{user_id}",
        "display_name": f"g{user_id}", "dwarf_name": dwarf_name,
        "points": points, "daily_wins": daily_wins,
    }


def terminal_session(chat_id=CHAT, *, outcome="hit", winner_points=20,
                     loser_points=20, loser_daily_wins=0, dwarf_name=None):
    winner_id, loser_id = ((1, 2) if outcome == "hit" else (2, 1))
    checkpoint = {
        "kind": "terminal_resolution", "outcome": outcome,
        "outcome_phrase": "Конец.",
        "attack_phrase": "Удар.",
        "strike_zone": "head", "block_zone": "body",
        "attacker_user_id": 1, "defender_user_id": 2,
        "round_no": 2, "resolved_turn_id": 2,
        "winner_user_id": winner_id, "loser_user_id": loser_id,
    }
    snapshots = {
        winner_id: snapshot(winner_id, points=winner_points, dwarf_name=dwarf_name),
        loser_id: snapshot(loser_id, points=loser_points, daily_wins=loser_daily_wins),
    }
    return create_duel_session(
        chat_id, 1, 2, snapshots[1], snapshots[2], 1, 2,
        status="publishing", phase="block", attack_zone="head",
        round_no=2, turn_id=3, result=checkpoint, now_ms=NOW,
    )


def register(chat_id=CHAT):
    for user_id in (1, 2):
        database.get_or_create_duel_user(user(user_id), chat_id)


def row(db_path, chat_id, user_id):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT points, wins, losses, stolen_dicks_count, dick_stolen_count, "
            "dick_stolen_today, last_stolen_by FROM duel_users "
            "WHERE chat_id = ? AND user_id = ?", (chat_id, user_id),
        ).fetchone()


def inventory(db_path, chat_id=CHAT):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT id, user_id, item_id FROM duel_inventory "
            "WHERE chat_id = ? ORDER BY id", (chat_id,),
        ).fetchall()


def events(db_path, chat_id=CHAT):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT 1 FROM duel_item_events WHERE chat_id = ?", (chat_id,),
        ).fetchall()


class TraceRng:
    def __init__(self, rolls=(), *, indexes=()):
        self.rolls = iter(rolls)
        self.indexes = iter(indexes)
        self.trace = []

    def random(self):
        value = next(self.rolls)
        self.trace.append(("random", value))
        return value

    def choice(self, values):
        if values and isinstance(values[0], dict) and "item_id" in values[0]:
            signature = ("inventory", tuple(value["item_id"] for value in values))
        elif values and isinstance(values[0], dict) and "user_id" in values[0]:
            signature = ("participants", tuple(value["user_id"] for value in values))
        else:
            signature = ("catalog", tuple(values))
        self.trace.append(("choice", signature))
        return values[next(self.indexes, 0)]


def rng(monkeypatch, rolls, *, indexes=()):
    trace = TraceRng(rolls, indexes=indexes)
    monkeypatch.setattr(duel_service, "random", trace)
    return trace


@pytest.mark.parametrize("outcome,winner,loser", [("hit", 1, 2), ("suicide", 2, 1)])
def test_terminal_finish_commits_once_and_replays_without_rng(
    temp_database, monkeypatch, outcome, winner, loser,
):
    register()
    session = terminal_session(outcome=outcome)
    trace = rng(monkeypatch, [0.99, 0.99, 0.99])
    result = duel_service.finalize_persistent_duel(CHAT, session["id"], now_ms=NOW + 10)

    assert result.reason == "finalized"
    assert result.session["status"] == "finished"
    assert result.session["finished_at"] == NOW + 10
    assert result.session["deadline_at"] is None
    assert result.session["pocket_done_at"] is None
    assert result.result["kind"] == "finalized"
    assert result.result["terminal_resolution"] == session["result"]
    assert (result.result["winner_user_id"], result.result["loser_user_id"]) == (winner, loser)
    assert result.result["dwarf_fact"] is None
    assert result.result["post_message"] is None
    assert result.result["round_flavor"] in get_text_list("duel.round_flavor.few")
    assert result.result["round_flavor"] in result.result["final_text"]
    assert row(temp_database, CHAT, winner)[:3] == (30, 1, 0)
    assert row(temp_database, CHAT, loser)[:3] == (15, 0, 1)
    month = database.moscow_month_key()
    assert database.get_monthly_chat_stats(CHAT, month) == {
        "dicks_stolen": 0, "duels": 1, "bosses_killed": 0,
    }
    assert events(temp_database) == []
    assert [entry[0] for entry in trace.trace] == ["random", "choice", "random", "random"]

    before = list(trace.trace)
    replay = duel_service.finalize_persistent_duel(CHAT, session["id"])
    assert replay.reason == "already_finished"
    assert replay.result == result.result
    assert trace.trace == before
    assert database.get_monthly_chat_stats(CHAT, month)["duels"] == 1


@pytest.mark.parametrize("mode,expected", [
    ("foreign", "not_found"), ("missing", "not_found"),
    ("attack", "invalid_state"), ("plain_block", "not_terminal"),
])
def test_rejected_finish_has_zero_rng(temp_database, monkeypatch, mode, expected):
    register()
    session = terminal_session()
    trace = rng(monkeypatch, [])
    chat_id, duel_id = CHAT, session["id"]
    if mode == "foreign":
        chat_id = OTHER_CHAT
    elif mode == "missing":
        duel_id += 999
    elif mode == "attack":
        with sqlite3.connect(temp_database) as conn:
            conn.execute(
                "UPDATE duel_sessions SET phase = 'attack', attack_zone = NULL, "
                "result_json = NULL WHERE id = ?", (duel_id,),
            )
    elif mode == "plain_block":
        with sqlite3.connect(temp_database) as conn:
            conn.execute("UPDATE duel_sessions SET result_json = NULL WHERE id = ?", (duel_id,))
    assert duel_service.finalize_persistent_duel(chat_id, duel_id).reason == expected
    assert trace.trace == []
    assert row(temp_database, CHAT, 1)[1] == 0


def test_snapshot_points_names_and_daily_wins_are_immutable(
    temp_database, monkeypatch,
):
    register()
    session = terminal_session(winner_points=80, loser_points=7, loser_daily_wins=3,
                               dwarf_name="Старое имя")
    with sqlite3.connect(temp_database) as conn:
        conn.execute(
            "UPDATE duel_users SET points = 1, daily_wins = 99, dwarf_name = 'Новое имя' "
            "WHERE chat_id = ?", (CHAT,),
        )
    seen = []
    monkeypatch.setattr(duel_service, "get_dick_steal_chance",
                        lambda daily_wins: seen.append(daily_wins) or 0.0)
    rng(monkeypatch, [0.99, 0.99, 0.99])
    result = duel_service.finalize_persistent_duel(CHAT, session["id"])
    assert seen == [3]
    assert row(temp_database, CHAT, 1)[0] == 90
    assert row(temp_database, CHAT, 2)[0] == 2
    assert "Старое имя" in result.result["final_text"]
    assert "Новое имя" not in result.result["final_text"]


def test_dick_and_exact_collectible_steal_with_one_monthly_increment(
    temp_database, monkeypatch,
):
    register()
    base = database.add_duel_inventory_item(CHAT, 2, "knife")
    first = database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    second = database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    session = terminal_session()
    trace = rng(monkeypatch, [0.0, 0.0, 0.99, 0.99], indexes=[1])
    result = duel_service.finalize_persistent_duel(CHAT, session["id"])

    assert result.result["is_dick_stolen"] is True
    assert result.result["stolen_item"]["instance_id"] == second["id"]
    assert result.result["stolen_item"]["item_id"] == second["item_id"]
    assert result.result["dwarf_fact"] == duel.DWARFS_FACTS[0]
    assert inventory(temp_database) == [
        (base["id"], 2, "knife"), (first["id"], 2, "po_lochki"),
        (second["id"] + 1, 1, "po_lochki"),
    ]
    assert row(temp_database, CHAT, 1)[3] == 1
    assert row(temp_database, CHAT, 2)[4:6] == (1, 1)
    assert database.get_monthly_chat_stats(CHAT, database.moscow_month_key()) == {
        "dicks_stolen": 1, "duels": 1, "bosses_killed": 0,
    }
    assert len([call for call in trace.trace if call[0] == "choice" and
                call[1][0] == "inventory"]) == 1
    assert events(temp_database) == []
    assert duel_service.finalize_persistent_duel(CHAT, session["id"]).reason == "already_finished"
    assert database.get_monthly_chat_stats(CHAT, database.moscow_month_key())["duels"] == 1


def test_zero_point_snapshot_guarantees_steal_without_decision_rng(
    temp_database, monkeypatch,
):
    from unittest.mock import Mock

    register()
    item = database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    session = terminal_session(winner_points=0, loser_points=0, loser_daily_wins=4)
    with sqlite3.connect(temp_database) as conn:
        conn.execute("UPDATE duel_users SET points = 35, daily_wins = 99 "
                     "WHERE chat_id = ? AND user_id = 2", (CHAT,))
    chance = Mock(side_effect=AssertionError("guaranteed steal computed chance"))
    monkeypatch.setattr(duel_service, "get_dick_steal_chance", chance)
    trace = rng(monkeypatch, [0.0, 0.0, 0.0])

    finalized = duel_service.finalize_persistent_duel(CHAT, session["id"])

    assert finalized.reason == "finalized"
    result = finalized.result
    assert result["is_dick_stolen"] is True
    assert (result["winner_points"], result["loser_points"]) == (10, 0)
    assert result["stolen_item"]["instance_id"] == item["id"]
    assert result["dwarf_fact"] == duel_service.DWARFS_FACTS[0]
    assert result["berserk"] is not None
    assert result["berserk"]["applied"] is False
    assert result["post_message"] is not None
    assert [call[0] for call in trace.trace] == [
        "random", "choice", "choice", "choice", "random",
        "choice", "choice", "choice", "random", "choice",
    ]
    assert trace.trace[1][1][0] == "inventory"
    assert trace.trace[3][1] == ("catalog", tuple(duel_service.DWARFS_FACTS))
    assert trace.trace[5][1][0] == "participants"
    assert trace.trace[-1][1] == ("catalog", tuple(duel_service.DUEL_POST_MESSAGES))
    chance.assert_not_called()
    assert row(temp_database, CHAT, 1)[:4] == (10, 1, 0, 1)
    assert row(temp_database, CHAT, 2) == (0, 0, 1, 0, 1, 1, "g1")
    assert inventory(temp_database) == [(item["id"] + 1, 1, "po_lochki")]
    month = database.moscow_month_key()
    assert database.get_monthly_chat_stats(CHAT, month) == {
        "dicks_stolen": 1, "duels": 1, "bosses_killed": 0,
    }
    before = list(trace.trace)
    replay = duel_service.finalize_persistent_duel(CHAT, session["id"])
    assert replay.reason == "already_finished" and replay.result == result
    assert trace.trace == before
    chance.assert_not_called()
    assert database.get_monthly_chat_stats(CHAT, month)["dicks_stolen"] == 1


def test_persistent_result_floor_covers_one_point_loser_and_legacy_negative_winner(
    temp_database, monkeypatch,
):
    register()
    session = terminal_session(winner_points=-20, loser_points=1)
    trace = rng(monkeypatch, [0.99, 0.99, 0.99])
    result = duel_service.finalize_persistent_duel(CHAT, session["id"])
    assert result.reason == "finalized"
    assert result.result["is_dick_stolen"] is False
    assert (result.result["winner_points"], result.result["loser_points"]) == (0, 0)
    assert row(temp_database, CHAT, 1)[0] == row(temp_database, CHAT, 2)[0] == 0
    assert len([call for call in trace.trace if call[0] == "random"]) == 3


@pytest.mark.parametrize("already_dickless,applied", [(False, True), (True, False)])
def test_berserk_applies_once_with_current_fallback(
    temp_database, monkeypatch, already_dickless, applied,
):
    register()
    if already_dickless:
        with sqlite3.connect(temp_database) as conn:
            conn.execute(
                "UPDATE duel_users SET dick_stolen_today = 1 WHERE chat_id = ? AND user_id = 2",
                (CHAT,),
            )
    session = terminal_session()
    trace = rng(monkeypatch, [0.99, 0.0, 0.99])
    result = duel_service.finalize_persistent_duel(CHAT, session["id"])
    assert result.result["berserk"]["applied"] is applied
    assert result.result["berserk"]["berserker_user_id"] == 1
    assert result.result["berserk"]["victim_user_id"] == 2
    assert result.result["berserk"]["text"] in result.result["final_text"]
    assert row(temp_database, CHAT, 2)[4] == int(applied)
    assert row(temp_database, CHAT, 1)[3] == int(applied)
    assert len([call for call in trace.trace if call[0] == "choice"]) == 4
    assert events(temp_database) == []


def test_post_message_saved_and_no_pocket_rng(temp_database, monkeypatch):
    register()
    session = terminal_session()
    trace = rng(monkeypatch, [0.99, 0.99, 0.0])
    result = duel_service.finalize_persistent_duel(CHAT, session["id"])
    assert result.result["post_message"] == duel.DUEL_POST_MESSAGES[0]
    assert result.result["post_message"] in result.result["final_text"]
    assert len([call for call in trace.trace if call[0] == "random"]) == 3
    assert events(temp_database) == []


def test_optional_item_transfer_failure_is_not_rerolled(temp_database, monkeypatch):
    register()
    database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    session = terminal_session()
    trace = rng(monkeypatch, [0.0, 0.0, 0.99, 0.99])
    monkeypatch.setattr(duel_service, "transfer_duel_inventory_item_in_transaction",
                        lambda *args: False)
    result = duel_service.finalize_persistent_duel(CHAT, session["id"])
    assert result.result["stolen_item"] is None
    assert len(inventory(temp_database)) == 1
    assert len([call for call in trace.trace if call[0] == "choice" and
                call[1][0] == "inventory"]) == 1


def test_item_steal_miss_and_only_base_items_consume_correct_rng(temp_database, monkeypatch):
    register()
    database.add_duel_inventory_item(CHAT, 2, "knife")
    base_only = terminal_session()
    trace = rng(monkeypatch, [0.0, 0.99, 0.99])
    result = duel_service.finalize_persistent_duel(CHAT, base_only["id"])
    assert result.result["stolen_item"] is None
    assert [call[1] for call in trace.trace if call[0] == "random"] == [0.0, 0.99, 0.99]
    assert inventory(temp_database)[0][2] == "knife"

    database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    next_session = terminal_session()
    trace2 = rng(monkeypatch, [0.0, 0.99, 0.99, 0.99])
    result2 = duel_service.finalize_persistent_duel(CHAT, next_session["id"])
    assert result2.result["stolen_item"] is None
    assert [call[1] for call in trace2.trace if call[0] == "random"] == [
        0.0, 0.99, 0.99, 0.99,
    ]
    assert all(call[1][0] != "inventory" for call in trace2.trace if call[0] == "choice")


def test_optional_transfer_exception_rolls_back_only_item_steal(
    temp_database, monkeypatch,
):
    register()
    item = database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    session = terminal_session()
    original = duel_service.transfer_duel_inventory_item_in_transaction

    def fail_after_transfer(*args):
        assert original(*args)
        raise RuntimeError("item transfer failed after mutation")

    monkeypatch.setattr(duel_service, "transfer_duel_inventory_item_in_transaction",
                        fail_after_transfer)
    trace = rng(monkeypatch, [0.0, 0.0, 0.99, 0.99])
    result = duel_service.finalize_persistent_duel(CHAT, session["id"])
    assert result.reason == "finalized"
    assert result.result["stolen_item"] is None
    assert inventory(temp_database) == [(item["id"], 2, "po_lochki")]
    assert database.get_monthly_chat_stats(CHAT, database.moscow_month_key())["duels"] == 1
    assert len([call for call in trace.trace if call[0] == "choice" and
                call[1][0] == "inventory"]) == 1


def test_berserk_database_error_skips_only_berserk_text_and_rng(
    temp_database, monkeypatch,
):
    register()
    session = terminal_session()

    def fail_berserk(*args):
        raise RuntimeError("injected berserk failure")

    monkeypatch.setattr(duel_service, "apply_duel_berserk_in_transaction", fail_berserk)
    trace = rng(monkeypatch, [0.99, 0.0, 0.99])
    result = duel_service.finalize_persistent_duel(CHAT, session["id"])
    assert result.reason == "finalized"
    assert result.result["berserk"] is None
    assert len([call for call in trace.trace if call[0] == "choice"]) == 2
    assert row(temp_database, CHAT, 2)[4] == 0


def test_mandatory_failure_rolls_back_all_effects(temp_database, monkeypatch):
    register()
    session = terminal_session()
    original = duel_service.apply_duel_result_plan_in_transaction

    def fail_after_plan(cursor, chat_id, plan):
        original(cursor, chat_id, plan)
        raise RuntimeError("injected failure")

    monkeypatch.setattr(duel_service, "apply_duel_result_plan_in_transaction", fail_after_plan)
    trace = rng(monkeypatch, [0.99])
    with pytest.raises(RuntimeError, match="injected failure"):
        duel_service.finalize_persistent_duel(CHAT, session["id"])
    assert trace.trace == [("random", 0.99)]
    assert get_duel_session(CHAT, session["id"])["result"] == session["result"]
    assert get_duel_session(CHAT, session["id"])["status"] == "publishing"
    assert row(temp_database, CHAT, 1)[:3] == (20, 0, 0)
    assert database.get_monthly_chat_stats(CHAT, database.moscow_month_key())["duels"] == 0


def test_late_failure_rolls_back_stats_inventory_berserk_and_session(
    temp_database, monkeypatch,
):
    register()
    item = database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    session = terminal_session()
    trace = rng(monkeypatch, [0.0, 0.0, 0.0, 0.99])

    def fail_final_session_write(*args, **kwargs):
        raise RuntimeError("late session failure")

    monkeypatch.setattr(duel_service, "finish_duel_session_in_transaction",
                        fail_final_session_write)
    with pytest.raises(RuntimeError, match="late session failure"):
        duel_service.finalize_persistent_duel(CHAT, session["id"])
    assert trace.trace
    assert get_duel_session(CHAT, session["id"])["status"] == "publishing"
    assert get_duel_session(CHAT, session["id"])["result"] == session["result"]
    assert row(temp_database, CHAT, 1)[:3] == (20, 0, 0)
    assert row(temp_database, CHAT, 2)[4] == 0
    assert inventory(temp_database) == [(item["id"], 2, "po_lochki")]
    assert database.get_monthly_chat_stats(CHAT, database.moscow_month_key())["duels"] == 0


def test_concurrent_finalize_performs_one_rng_and_one_commit(temp_database, monkeypatch):
    register()
    session = terminal_session()
    trace = rng(monkeypatch, [0.99, 0.99, 0.99])
    barrier = Barrier(2)

    def finish():
        barrier.wait()
        return duel_service.finalize_persistent_duel(CHAT, session["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: finish(), range(2)))
    assert {result.reason for result in results} == {"finalized", "already_finished"}
    assert results[0].result == results[1].result
    assert len([call for call in trace.trace if call[0] == "random"]) == 3
    assert row(temp_database, CHAT, 1)[:3] == (30, 1, 0)
    assert database.get_monthly_chat_stats(CHAT, database.moscow_month_key())["duels"] == 1


@pytest.mark.parametrize("loser_points", (20, 0))
def test_concurrent_finalize_with_optional_effects_still_applies_once(
    temp_database, monkeypatch, loser_points,
):
    register()
    item = database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    session = terminal_session(loser_points=loser_points)
    trace = rng(monkeypatch, [0.0, 0.0, 0.0, 0.99] if loser_points else [0.0, 0.0, 0.99])
    barrier = Barrier(2)

    def finish():
        barrier.wait()
        return duel_service.finalize_persistent_duel(CHAT, session["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: finish(), range(2)))
    assert {result.reason for result in results} == {"finalized", "already_finished"}
    assert results[0].result == results[1].result
    assert len([call for call in trace.trace if call[0] == "random"]) == (4 if loser_points else 3)
    assert len([call for call in trace.trace if call[0] == "choice" and
                call[1][0] == "inventory"]) == 1
    assert len(inventory(temp_database)) == 1
    assert inventory(temp_database)[0][0] != item["id"]
    assert inventory(temp_database)[0][1] == 1
    assert row(temp_database, CHAT, 1)[3] == 1
    assert row(temp_database, CHAT, 2)[4] == 1
    assert database.get_monthly_chat_stats(CHAT, database.moscow_month_key()) == {
        "dicks_stolen": 1, "duels": 1, "bosses_killed": 0,
    }
    before = list(trace.trace)
    assert duel_service.finalize_persistent_duel(CHAT, session["id"]).reason == "already_finished"
    assert trace.trace == before


def test_corrupt_checkpoint_rejected_before_rng(temp_database, monkeypatch):
    register()
    session = terminal_session()
    with sqlite3.connect(temp_database) as conn:
        conn.execute(
            "UPDATE duel_sessions SET result_json = replace(result_json, "
            "'\"winner_user_id\": 1', '\"winner_user_id\": 99') WHERE id = ?",
            (session["id"],),
        )
    trace = rng(monkeypatch, [])
    result = duel_service.finalize_persistent_duel(CHAT, session["id"])
    assert result.reason == "invalid_state"
    assert trace.trace == []
    assert row(temp_database, CHAT, 1)[1] == 0


def test_same_user_ids_in_other_chat_untouched(temp_database, monkeypatch):
    register(CHAT)
    register(OTHER_CHAT)
    session = terminal_session()
    rng(monkeypatch, [0.99, 0.99, 0.99])
    duel_service.finalize_persistent_duel(CHAT, session["id"])
    assert row(temp_database, OTHER_CHAT, 1)[:3] == (20, 0, 0)
    assert row(temp_database, OTHER_CHAT, 2)[:3] == (20, 0, 0)
    assert database.get_monthly_chat_stats(OTHER_CHAT, database.moscow_month_key())["duels"] == 0


@pytest.mark.parametrize("loser_points", (20, 0))
@pytest.mark.asyncio
async def test_persistent_finish_rng_trace_matches_telegram_prefix(
    temp_database, monkeypatch, fake_context, loser_points,
):
    register()
    session = terminal_session(loser_points=loser_points)
    persistent_rng = rng(monkeypatch, [0.0, 0.99, 0.99, 0.0]
                         if loser_points else [0.99, 0.99, 0.0])
    persistent = duel_service.finalize_persistent_duel(CHAT, session["id"])
    persistent_trace = list(persistent_rng.trace)

    other = OTHER_CHAT
    register(other)
    with sqlite3.connect(temp_database) as conn:
        conn.execute("UPDATE duel_users SET points = ? WHERE chat_id = ? AND user_id = 2",
                     (loser_points, other))
    winner = database.get_duel_user_by_id(other, 1)
    loser = database.get_duel_user_by_id(other, 2)
    duel.ACTIVE_DUELS[other] = {"round": 2}
    telegram_rng = TraceRng([0.0, 0.99, 0.99, 0.0, 0.99]
                            if loser_points else [0.99, 0.99, 0.0, 0.99])
    monkeypatch.setattr(duel, "random", telegram_rng)
    # duel_text helpers use their module-level RNG in the unchanged Telegram path.
    from handlers import duel_text
    monkeypatch.setattr(duel_text, "random", telegram_rng)
    checkpoint = session["result"]
    custom_text = duel_service._terminal_custom_text(checkpoint, winner, loser)
    await duel._finish_duel(fake_context, other, winner, loser, custom_text)
    duel.ACTIVE_DUELS.pop(other, None)
    assert telegram_rng.trace[:len(persistent_trace)] == persistent_trace
    assert persistent.result["final_text"] == fake_context.bot.send_message.call_args_list[0].kwargs["text"]
    assert telegram_rng.trace == persistent_trace  # no inventory means no pocket roll


@pytest.mark.asyncio
async def test_full_optional_rng_trace_matches_telegram_before_pocket(
    temp_database, monkeypatch, fake_context,
):
    register()
    for _ in range(2):
        database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    session = terminal_session()
    persistent_rng = rng(monkeypatch, [0.0, 0.0, 0.0, 0.0])
    result = duel_service.finalize_persistent_duel(CHAT, session["id"])
    prefix = list(persistent_rng.trace)
    assert result.result["stolen_item"] is not None
    assert result.result["berserk"] is not None
    assert result.result["post_message"] is not None

    register(OTHER_CHAT)
    for _ in range(2):
        database.add_duel_inventory_item(OTHER_CHAT, 2, "po_lochki")
    winner = database.get_duel_user_by_id(OTHER_CHAT, 1)
    loser = database.get_duel_user_by_id(OTHER_CHAT, 2)
    duel.ACTIVE_DUELS[OTHER_CHAT] = {"round": 2}
    telegram_rng = TraceRng([0.0, 0.0, 0.0, 0.0, 0.99])
    monkeypatch.setattr(duel, "random", telegram_rng)
    from handlers import duel_text
    monkeypatch.setattr(duel_text, "random", telegram_rng)
    custom_text = duel_service._terminal_custom_text(session["result"], winner, loser)
    await duel._finish_duel(fake_context, OTHER_CHAT, winner, loser, custom_text)
    duel.ACTIVE_DUELS.pop(OTHER_CHAT, None)
    assert telegram_rng.trace[:-1] == prefix
    assert telegram_rng.trace[-1] == ("random", 0.99)  # post-publication pocket roll
    assert fake_context.bot.send_message.call_args_list[0].kwargs["text"] == result.result["final_text"]
