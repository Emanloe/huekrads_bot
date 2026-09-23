"""Durable publication and post-publication pocket drop; Telegram is not cut over."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database
from duel_outbox_repository import (
    claim_duel_publication, create_duel_publication_in_transaction, get_duel_publication,
    list_retryable_duel_publications, list_retryable_duel_publication_chat_ids,
    release_duel_publication,
)
from duel_session_repository import (
    create_duel_session, get_duel_session, list_due_duel_sessions,
    list_ready_pocket_duel_sessions, list_terminal_pending_duel_sessions,
)
from handlers import duel_service
from handlers.persistent_duel_publisher import publish_persistent_duel_outbox


CHAT = -9501
OTHER_CHAT = -9502
NOW = 1_800_000_000_000


class TraceRng:
    def __init__(self, rolls=()):
        self.rolls = iter(rolls)
        self.trace = []

    def random(self):
        value = next(self.rolls)
        self.trace.append(("random", value))
        return value

    def choice(self, values):
        self.trace.append(("choice", tuple(
            value.get("item_id", value.get("user_id", "dict"))
            if isinstance(value, dict) else value for value in values
        )))
        return values[0]


def install_rng(monkeypatch, rolls=()):
    trace = TraceRng(rolls)
    monkeypatch.setattr(duel_service, "random", trace)
    return trace


def register(chat_id=CHAT):
    for user_id in (1, 2, 3):
        database.get_or_create_duel_user(
            SimpleNamespace(id=user_id, username=f"player{user_id}", first_name="Player"),
            chat_id,
        )


def snapshot(user_id):
    return {
        "user_id": user_id, "username": f"player{user_id}",
        "display_name": f"player{user_id}", "dwarf_name": None,
        "points": 20, "daily_wins": 0,
    }


def terminal_session(chat_id=CHAT):
    checkpoint = {
        "kind": "terminal_resolution", "outcome": "hit",
        "outcome_phrase": "Итог.", "attack_phrase": "Удар.",
        "strike_zone": "head", "block_zone": "body",
        "attacker_user_id": 1, "defender_user_id": 2,
        "round_no": 2, "resolved_turn_id": 2,
        "winner_user_id": 1, "loser_user_id": 2,
    }
    return create_duel_session(
        chat_id, 1, 2, snapshot(1), snapshot(2), 1, 2,
        status="publishing", phase="block", attack_zone="head",
        round_no=2, turn_id=3, result=checkpoint, now_ms=NOW,
    )


def outbox(db_path, chat_id=CHAT):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT id, kind, turn_id, status FROM duel_outbox "
            "WHERE chat_id = ? ORDER BY id", (chat_id,),
        ).fetchall()


def event(db_path, chat_id=CHAT):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT event_id, item_id, message_id, claimed FROM duel_item_events "
            "WHERE chat_id = ? ORDER BY event_id", (chat_id,),
        ).fetchall()


def inventory(db_path, chat_id=CHAT):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT id, user_id, item_id FROM duel_inventory "
            "WHERE chat_id = ? ORDER BY id", (chat_id,),
        ).fetchall()


def finish_and_get_final(temp_database, monkeypatch, chat_id=CHAT):
    register(chat_id)
    session = terminal_session(chat_id)
    trace = install_rng(monkeypatch, [0.99, 0.99, 0.99])
    result = duel_service.finalize_persistent_duel(chat_id, session["id"], now_ms=NOW + 1)
    final_pub = outbox(temp_database, chat_id)[0]
    return result, final_pub, trace


def mark_final_published(chat_id, final_pub, *, message_id=500):
    claimed = claim_duel_publication(chat_id, final_pub[0], now_ms=NOW + 2)
    assert claimed is not None
    ack = duel_service.acknowledge_persistent_duel_publication(
        chat_id, final_pub[0], claimed["attempt_count"], message_id, NOW + 3,
    )
    assert ack.reason == "delivered"
    return ack


def test_outbox_schema_and_unique_keys_are_idempotent(temp_database):
    database.init_db()
    database.init_db()
    with sqlite3.connect(temp_database) as conn:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(duel_outbox)")}
        assert {"idx_duel_outbox_turn", "idx_duel_outbox_once",
                "idx_duel_outbox_retry"} <= indexes
    session = terminal_session()
    with sqlite3.connect(temp_database) as conn:
        create_duel_publication_in_transaction(
            conn.cursor(), CHAT, session["id"], "final_result", 3,
            {"text": "stored"}, NOW,
        )
    database.init_db()
    assert get_duel_publication(CHAT, outbox(temp_database)[0][0])["payload"]["text"] == "stored"


def test_duplicate_logical_publication_key_is_rejected_by_sqlite(temp_database):
    session = terminal_session()
    with sqlite3.connect(temp_database) as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        create_duel_publication_in_transaction(
            cursor, CHAT, session["id"], "block_prompt", 3, {"text": "x"}, NOW,
        )
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(temp_database) as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            create_duel_publication_in_transaction(
                cursor, CHAT, session["id"], "block_prompt", 3, {"text": "x"}, NOW,
            )
    assert len(outbox(temp_database)) == 1
    with sqlite3.connect(temp_database) as conn:
        create_duel_publication_in_transaction(
            conn.cursor(), CHAT, session["id"], "final_result", 3, {"text": "y"}, NOW,
        )
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(temp_database) as conn:
            create_duel_publication_in_transaction(
                conn.cursor(), CHAT, session["id"], "final_result", 4,
                {"text": "duplicate final"}, NOW,
            )


def test_start_and_rejected_start_create_one_initial_intent(temp_database, monkeypatch):
    register()
    trace = install_rng(monkeypatch)
    first = duel_service.start_persistent_duel(CHAT, 1, 2, now_ms=NOW)
    assert first.success
    assert [kind for _, kind, _, _ in outbox(temp_database)] == ["attack_prompt"]
    pub = get_duel_publication(CHAT, outbox(temp_database)[0][0])
    assert pub["payload"]["phase"] == "attack"
    assert pub["payload"]["text"]
    assert first.session["status"] == "publishing"
    assert trace.trace == [("choice", (True, False))]
    assert duel_service.start_persistent_duel(CHAT, 1, 2).reason == "active_duel"
    assert len(outbox(temp_database)) == 1
    assert len(trace.trace) == 1


def test_attack_and_nonterminal_block_each_create_one_prompt_intent(
    temp_database, monkeypatch,
):
    register()
    rng = install_rng(monkeypatch, [0.5, 0.5])
    start = duel_service.start_persistent_duel(CHAT, 1, 2, now_ms=NOW)
    duel_id = start.session["id"]
    assert duel_service.activate_persistent_duel_turn(CHAT, duel_id, 1, 100, NOW).reason == "success"
    attack = duel_service.submit_persistent_duel_attack(CHAT, duel_id, 1, 1, "head", now_ms=NOW + 1)
    assert attack.reason == "success"
    assert attack.session["phase"] == "block"
    assert [kind for _, kind, _, _ in outbox(temp_database)] == [
        "attack_prompt", "block_prompt",
    ]
    assert duel_service.activate_persistent_duel_turn(CHAT, duel_id, 2, 100, NOW + 2).reason == "success"
    block = duel_service.submit_persistent_duel_block(CHAT, duel_id, 2, 2, "head", now_ms=NOW + 3)
    assert block.reason == "success"
    assert block.session["phase"] == "attack"
    assert [kind for _, kind, _, _ in outbox(temp_database)] == [
        "attack_prompt", "block_prompt", "attack_prompt",
    ]
    assert [turn for _, _, turn, _ in outbox(temp_database)] == [1, 2, 3]
    assert get_duel_publication(CHAT, outbox(temp_database)[2][0])["payload"]["text"]
    assert duel_service.submit_persistent_duel_block(CHAT, duel_id, 2, 2, "head").reason == "stale_turn"
    assert len(outbox(temp_database)) == 3
    assert len([call for call in rng.trace if call[0] == "random"]) == 2


def test_finalization_creates_one_final_intent_and_retry_does_not_duplicate(
    temp_database, monkeypatch,
):
    result, final_pub, rng = finish_and_get_final(temp_database, monkeypatch)
    publication = get_duel_publication(CHAT, final_pub[0])
    assert final_pub[1:] == ("final_result", 3, "pending")
    assert publication["payload"]["text"] == result.result["final_text"]
    before = list(rng.trace)
    assert duel_service.finalize_persistent_duel(CHAT, result.session["id"]).reason == "already_finished"
    assert outbox(temp_database) == [final_pub]
    assert rng.trace == before


def test_outbox_survives_reopen_and_is_chat_scoped(temp_database, monkeypatch):
    _, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    assert get_duel_publication(OTHER_CHAT, final_pub[0]) is None
    assert claim_duel_publication(OTHER_CHAT, final_pub[0], now_ms=NOW + 2) is None
    assert [row["id"] for row in list_retryable_duel_publications(CHAT, NOW + 2)] == [final_pub[0]]
    assert list_retryable_duel_publications(OTHER_CHAT, NOW + 2) == []
    assert list_retryable_duel_publication_chat_ids(NOW + 2) == [CHAT]
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM duel_outbox").fetchone()[0] == 1
    assert get_duel_publication(CHAT, final_pub[0])["payload"]["text"]


def test_lease_prevents_parallel_send_and_expiry_recovers(temp_database, monkeypatch):
    _, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    barrier = Barrier(2)

    def claim():
        barrier.wait()
        return claim_duel_publication(CHAT, final_pub[0], now_ms=NOW + 2, lease_ms=100)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))
    assert sum(result is not None for result in claims) == 1
    assert list_retryable_duel_publications(CHAT, NOW + 101) == []
    assert len(list_retryable_duel_publications(CHAT, NOW + 102)) == 1
    second = claim_duel_publication(CHAT, final_pub[0], now_ms=NOW + 102)
    assert second["attempt_count"] == 2
    assert not release_duel_publication(CHAT, final_pub[0], 1, now_ms=NOW + 103)
    assert release_duel_publication(CHAT, final_pub[0], 2, now_ms=NOW + 103)
    assert len(list_retryable_duel_publications(CHAT, NOW + 103)) == 1


@pytest.mark.asyncio
async def test_prompt_publication_activates_once_and_arms_from_send_time(
    temp_database, monkeypatch, fake_context,
):
    register()
    rng = install_rng(monkeypatch)
    start = duel_service.start_persistent_duel(CHAT, 1, 2, now_ms=NOW)
    pub_id = outbox(temp_database)[0][0]
    sent = await publish_persistent_duel_outbox(
        CHAT, pub_id, fake_context.bot, claim_time_ms=NOW + 1,
        published_at_ms=NOW + 50,
    )
    assert sent.reason == "delivered"
    session = get_duel_session(CHAT, start.session["id"])
    assert session["status"] == "active"
    assert session["deadline_at"] == NOW + 10_050
    assert session["turn_id"] == 1
    assert session["message_id"] == 101
    assert fake_context.bot.send_message.call_args.kwargs["reply_markup"] is not None
    assert (await publish_persistent_duel_outbox(CHAT, pub_id, fake_context.bot)).reason == "not_retryable"
    assert get_duel_session(CHAT, session["id"])["deadline_at"] == NOW + 10_050
    assert rng.trace == [("choice", (True, False))]
    assert len(fake_context.bot.send_message.call_args_list) == 1


@pytest.mark.asyncio
async def test_previously_active_prompt_ack_does_not_extend_deadline(
    temp_database, monkeypatch, fake_context,
):
    register()
    install_rng(monkeypatch)
    session = duel_service.start_persistent_duel(CHAT, 1, 2, now_ms=NOW).session
    pub_id = outbox(temp_database)[0][0]
    duel_service.activate_persistent_duel_turn(CHAT, session["id"], 1, 100, NOW + 1)
    original_deadline = get_duel_session(CHAT, session["id"])["deadline_at"]
    result = await publish_persistent_duel_outbox(
        CHAT, pub_id, fake_context.bot,
        claim_time_ms=NOW + 2, published_at_ms=NOW + 100,
    )
    assert result.reason == "already_active"
    assert get_duel_session(CHAT, session["id"])["deadline_at"] == original_deadline
    assert get_duel_session(CHAT, session["id"])["message_id"] == 100
    assert get_duel_publication(CHAT, pub_id)["status"] == "delivered"


@pytest.mark.asyncio
async def test_prompt_edit_fallback_binds_new_message_id(
    temp_database, monkeypatch, fake_context,
):
    register()
    install_rng(monkeypatch)
    start = duel_service.start_persistent_duel(CHAT, 1, 2, now_ms=NOW)
    initial_pub = outbox(temp_database)[0][0]
    await publish_persistent_duel_outbox(
        CHAT, initial_pub, fake_context.bot,
        claim_time_ms=NOW + 1, published_at_ms=NOW + 2,
    )
    duel_service.submit_persistent_duel_attack(CHAT, start.session["id"], 1, 1, "body", now_ms=NOW + 3)
    block_pub = outbox(temp_database)[1][0]
    fake_context.bot.edit_message_text.side_effect = RuntimeError("message deleted")
    fake_context.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=202))
    result = await publish_persistent_duel_outbox(
        CHAT, block_pub, fake_context.bot,
        claim_time_ms=NOW + 4, published_at_ms=NOW + 5,
    )
    assert result.reason == "delivered"
    session = get_duel_session(CHAT, start.session["id"])
    assert session["message_id"] == 202
    assert session["deadline_at"] == NOW + 10_005
    assert get_duel_publication(CHAT, block_pub)["message_id"] == 202


@pytest.mark.asyncio
async def test_send_failure_is_retryable_and_does_not_change_gameplay(
    temp_database, monkeypatch, fake_context,
):
    _, final_pub, rng = finish_and_get_final(temp_database, monkeypatch)
    before = list(rng.trace)
    fake_context.bot.send_message.side_effect = RuntimeError("Telegram temporary failure")
    result = await publish_persistent_duel_outbox(
        CHAT, final_pub[0], fake_context.bot, claim_time_ms=NOW + 2,
    )
    assert result.reason == "send_failed"
    assert get_duel_publication(CHAT, final_pub[0])["status"] == "pending"
    assert get_duel_session(CHAT, result.publication["duel_id"])["status"] == "finished"
    assert rng.trace == before


@pytest.mark.asyncio
async def test_final_send_ack_enables_pocket_without_rng(
    temp_database, monkeypatch, fake_context,
):
    finished, final_pub, rng = finish_and_get_final(temp_database, monkeypatch)
    before = list(rng.trace)
    sent = await publish_persistent_duel_outbox(
        CHAT, final_pub[0], fake_context.bot,
        claim_time_ms=NOW + 2, published_at_ms=NOW + 3,
    )
    assert sent.reason == "delivered"
    assert get_duel_publication(CHAT, final_pub[0])["delivered_at"] == NOW + 3
    assert fake_context.bot.send_message.call_args.kwargs["text"] == finished.result["final_text"]
    assert rng.trace == before
    again = duel_service.acknowledge_persistent_duel_publication(CHAT, final_pub[0], 1, 600, NOW + 100)
    assert again.reason == "already_delivered"
    assert get_duel_publication(CHAT, final_pub[0])["delivered_at"] == NOW + 3


def test_pocket_before_final_ack_rejected_without_rng(temp_database, monkeypatch):
    finished, final_pub, trace = finish_and_get_final(temp_database, monkeypatch)
    trace.trace.clear()
    result = duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"])
    assert result.reason == "not_published"
    assert trace.trace == []
    assert get_duel_session(CHAT, finished.session["id"])["pocket_done_at"] is None
    assert outbox(temp_database) == [final_pub]


def test_recovery_lists_terminal_checkpoint_and_published_unprocessed_pocket(
    temp_database, monkeypatch,
):
    register()
    session = terminal_session()
    assert [s["id"] for s in list_terminal_pending_duel_sessions(CHAT)] == [session["id"]]
    assert list_terminal_pending_duel_sessions(OTHER_CHAT) == []
    install_rng(monkeypatch, [0.99, 0.99, 0.99])
    duel_service.finalize_persistent_duel(CHAT, session["id"], now_ms=NOW + 1)
    assert list_terminal_pending_duel_sessions(CHAT) == []
    assert list_ready_pocket_duel_sessions(CHAT) == []
    final_pub = outbox(temp_database)[0]
    mark_final_published(CHAT, final_pub)
    assert [s["id"] for s in list_ready_pocket_duel_sessions(CHAT)] == [session["id"]]
    assert list_ready_pocket_duel_sessions(OTHER_CHAT) == []
    # Reopening the DB is enough to discover this interrupted post-publication stage.
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute("SELECT pocket_done_at FROM duel_sessions WHERE id = ?",
                            (session["id"],)).fetchone()[0] is None
    duel_service.process_persistent_duel_pocket_drop(CHAT, session["id"])
    assert list_ready_pocket_duel_sessions(CHAT) == []


@pytest.mark.parametrize("include_base", [False, True])
def test_empty_or_base_only_pocket_marks_done_without_rng(
    temp_database, monkeypatch, include_base,
):
    finished, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    if include_base:
        database.add_duel_inventory_item(CHAT, 2, "knife")
    mark_final_published(CHAT, final_pub)
    trace = install_rng(monkeypatch)
    result = duel_service.process_persistent_duel_pocket_drop(
        CHAT, finished.session["id"], now_ms=NOW + 4,
    )
    assert result.reason == "empty_inventory"
    assert result.session["pocket_done_at"] == NOW + 4
    assert trace.trace == []
    assert duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"]).reason == "already_done"
    assert trace.trace == []


def test_pocket_miss_spends_one_roll_and_checkpoints(temp_database, monkeypatch):
    finished, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    item = database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    mark_final_published(CHAT, final_pub)
    trace = install_rng(monkeypatch, [0.99])
    result = duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"])
    assert result.reason == "miss"
    assert trace.trace == [("random", 0.99)]
    assert inventory(temp_database) == [(item["id"], 2, item["item_id"])]
    assert event(temp_database) == []
    assert duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"]).reason == "already_done"
    assert trace.trace == [("random", 0.99)]


def test_pocket_hit_moves_exact_instance_and_creates_one_drop_intent(
    temp_database, monkeypatch,
):
    finished, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    first = database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    second = database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    mark_final_published(CHAT, final_pub)
    trace = install_rng(monkeypatch, [0.0])
    # Choose the second concrete instance without changing the chance roll.
    trace.choice = lambda values: trace.trace.append(("choice", tuple(i["id"] for i in values))) or values[1]
    result = duel_service.process_persistent_duel_pocket_drop(
        CHAT, finished.session["id"], now_ms=NOW + 4,
    )
    assert result.reason == "dropped"
    assert result.drop["instance_id"] == second["id"]
    assert result.drop["item_id"] == second["item_id"]
    assert inventory(temp_database) == [(first["id"], 2, first["item_id"])]
    assert event(temp_database) == [(result.drop["event_id"], second["item_id"], None, 0)]
    assert result.publication["kind"] == "pocket_drop"
    assert result.publication["payload"]["drop"] == result.drop
    assert result.publication["status"] == "pending"
    assert trace.trace == [("random", 0.0), ("choice", (first["id"], second["id"]))]
    assert database.create_duel_item_event(CHAT) is None
    assert duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"]).reason == "already_done"
    assert len(outbox(temp_database)) == 2


def test_occupied_slot_spends_existing_roll_and_choice_but_keeps_item(
    temp_database, monkeypatch,
):
    finished, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    item = database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    old_event = database.create_duel_item_event(CHAT)
    mark_final_published(CHAT, final_pub)
    trace = install_rng(monkeypatch, [0.0])
    result = duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"])
    assert result.reason == "unavailable_slot_or_instance"
    assert [call[0] for call in trace.trace] == ["random", "choice"]
    assert inventory(temp_database) == [(item["id"], 2, item["item_id"])]
    assert event(temp_database)[0][0] == old_event
    assert len(outbox(temp_database)) == 1
    assert result.session["pocket_done_at"] is not None
    assert duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"]).reason == "already_done"
    assert len(trace.trace) == 2


def test_concurrent_pocket_drops_once(temp_database, monkeypatch):
    finished, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    mark_final_published(CHAT, final_pub)
    trace = install_rng(monkeypatch, [0.0])
    barrier = Barrier(2)

    def pocket():
        barrier.wait()
        return duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: pocket(), range(2)))
    assert {result.reason for result in results} == {"dropped", "already_done"}
    assert [call[0] for call in trace.trace] == ["random", "choice"]
    assert len(event(temp_database)) == 1
    assert len(outbox(temp_database)) == 2


def test_failed_drop_outbox_insert_rolls_back_inventory_event_and_checkpoint(
    temp_database, monkeypatch,
):
    finished, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    item = database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    mark_final_published(CHAT, final_pub)
    trace = install_rng(monkeypatch, [0.0])

    def fail_outbox(*args):
        raise RuntimeError("outbox insert failed")

    monkeypatch.setattr(duel_service, "create_duel_publication_in_transaction", fail_outbox)
    with pytest.raises(RuntimeError, match="outbox insert failed"):
        duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"])
    assert [call[0] for call in trace.trace] == ["random", "choice"]
    assert inventory(temp_database) == [(item["id"], 2, item["item_id"])]
    assert event(temp_database) == []
    assert get_duel_session(CHAT, finished.session["id"])["pocket_done_at"] is None
    assert len(outbox(temp_database)) == 1


@pytest.mark.asyncio
async def test_drop_outbox_recovers_after_reopen_and_uses_shared_pickup_callback(
    temp_database, monkeypatch, fake_context,
):
    finished, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    item = database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    mark_final_published(CHAT, final_pub)
    install_rng(monkeypatch, [0.0])
    dropped = duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"])
    publication_id = dropped.publication["id"]
    assert inventory(temp_database) == []
    assert list_retryable_duel_publications(CHAT, NOW + 100)[0]["id"] == publication_id
    # A simulated restart simply reopens SQLite; no in-memory outbox state is needed.
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM duel_outbox WHERE status = 'pending'").fetchone()[0] == 1
    sent = await publish_persistent_duel_outbox(
        CHAT, publication_id, fake_context.bot,
        claim_time_ms=NOW + 5, published_at_ms=NOW + 6,
    )
    assert sent.reason == "delivered"
    assert event(temp_database)[0][2] == 101
    markup = fake_context.bot.send_message.call_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == f"duel_item_claim_{dropped.drop['event_id']}"
    assert markup.inline_keyboard[0][0].text == "Подобрать"
    assert (await publish_persistent_duel_outbox(CHAT, publication_id, fake_context.bot)).reason == "not_retryable"
    status, claimed = database.claim_duel_item_event(
        dropped.drop["event_id"], CHAT, 3, lambda: (_ for _ in ()).throw(AssertionError("reroll")),
    )
    assert status == "claimed"
    assert claimed["item_id"] == item["item_id"]
    assert inventory(temp_database)[0][1] == 3


@pytest.mark.asyncio
async def test_drop_send_failure_preserves_event_and_retry_intent(
    temp_database, monkeypatch, fake_context,
):
    finished, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    item = database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    mark_final_published(CHAT, final_pub)
    trace = install_rng(monkeypatch, [0.0])
    dropped = duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"])
    before = list(trace.trace)
    fake_context.bot.send_message.side_effect = RuntimeError("temporary outage")
    sent = await publish_persistent_duel_outbox(
        CHAT, dropped.publication["id"], fake_context.bot,
        claim_time_ms=NOW + 5,
    )
    assert sent.reason == "send_failed"
    assert event(temp_database) == [(dropped.drop["event_id"], item["item_id"], None, 0)]
    assert inventory(temp_database) == []
    assert get_duel_publication(CHAT, dropped.publication["id"])["status"] == "pending"
    assert trace.trace == before


def test_explicit_compensation_returns_same_instance_without_rng(temp_database, monkeypatch):
    finished, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    item = database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    mark_final_published(CHAT, final_pub)
    trace = install_rng(monkeypatch, [0.0])
    dropped = duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"])
    before = list(trace.trace)
    compensated = duel_service.compensate_persistent_duel_drop(CHAT, finished.session["id"])
    assert compensated.reason == "compensated"
    assert inventory(temp_database) == [(item["id"], 2, item["item_id"])]
    assert event(temp_database) == []
    assert get_duel_publication(CHAT, dropped.publication["id"])["status"] == "cancelled"
    assert duel_service.compensate_persistent_duel_drop(CHAT, finished.session["id"]).reason == "already_compensated"
    assert trace.trace == before


def test_compensation_rejects_in_flight_or_claimed_event(temp_database, monkeypatch):
    finished, final_pub, _ = finish_and_get_final(temp_database, monkeypatch)
    database.add_duel_inventory_item(CHAT, 2, "po_lochki")
    mark_final_published(CHAT, final_pub)
    install_rng(monkeypatch, [0.0])
    dropped = duel_service.process_persistent_duel_pocket_drop(CHAT, finished.session["id"])
    pub_id = dropped.publication["id"]
    claimed = claim_duel_publication(CHAT, pub_id, now_ms=NOW + 5)
    assert duel_service.compensate_persistent_duel_drop(CHAT, finished.session["id"]).reason == "in_flight"
    assert release_duel_publication(CHAT, pub_id, claimed["attempt_count"], now_ms=NOW + 6)
    leased = claim_duel_publication(CHAT, pub_id, now_ms=NOW + 7)
    duel_service.acknowledge_persistent_duel_publication(
        CHAT, pub_id, leased["attempt_count"], 700, NOW + 8,
    )
    status, _ = database.claim_duel_item_event(dropped.drop["event_id"], CHAT, 3, lambda: None)
    assert status == "claimed"
    assert duel_service.compensate_persistent_duel_drop(CHAT, finished.session["id"]).reason == "event_unavailable"


def test_cross_chat_pocket_and_compensation_rejected(temp_database, monkeypatch):
    finished, final_pub, rng = finish_and_get_final(temp_database, monkeypatch)
    mark_final_published(CHAT, final_pub)
    rng.trace.clear()
    assert duel_service.process_persistent_duel_pocket_drop(OTHER_CHAT, finished.session["id"]).reason == "not_found"
    assert duel_service.compensate_persistent_duel_drop(OTHER_CHAT, finished.session["id"]).reason == "not_found"
    assert rng.trace == []
    assert get_duel_session(OTHER_CHAT, finished.session["id"]) is None


def test_due_sessions_remain_recoverable_separately_from_outbox(temp_database, monkeypatch):
    register()
    install_rng(monkeypatch)
    session = duel_service.start_persistent_duel(CHAT, 1, 2, now_ms=NOW).session
    pub = outbox(temp_database)[0]
    assert list_retryable_duel_publications(CHAT, NOW)[0]["id"] == pub[0]
    assert list_due_duel_sessions(CHAT, NOW + 20_000) == []
    duel_service.activate_persistent_duel_turn(CHAT, session["id"], 1, 100, NOW)
    assert [due["id"] for due in list_due_duel_sessions(CHAT, NOW + 10_000)] == [session["id"]]
