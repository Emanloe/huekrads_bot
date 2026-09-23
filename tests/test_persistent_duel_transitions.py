"""Dormant persistent duel turns: no Telegram cutover or terminal DB effects."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

import database
from duel_session_repository import create_duel_session, get_duel_session
from handlers import duel_service, duel_text


CHAT_A = -9201
CHAT_B = -9202
PUBLISHED_AT = 1_800_000_000_000
DEADLINE = PUBLISHED_AT + 10_000


def snapshot(user_id):
    return {
        "user_id": user_id, "username": f"player{user_id}",
        "display_name": f"player{user_id}", "dwarf_name": None,
        "points": 20, "daily_wins": 0,
    }


def make_session(
    chat_id=CHAT_A, *, status="publishing", phase="attack",
    attack_zone=None, turn_id=1, round_no=1, deadline_at=None,
):
    if status == "active" and deadline_at is None:
        deadline_at = DEADLINE
    return create_duel_session(
        chat_id, 1, 2, snapshot(1), snapshot(2), 1, 2,
        status=status, phase=phase, attack_zone=attack_zone,
        turn_id=turn_id, round_no=round_no, deadline_at=deadline_at,
        now_ms=PUBLISHED_AT,
    )


def attack_session():
    return make_session(status="active")


def block_session():
    return make_session(
        status="active", phase="block", attack_zone="head", turn_id=2,
        round_no=3,
    )


class TraceRng:
    def __init__(self, rolls=(), timeout_zone="head"):
        self.rolls = iter(rolls)
        self.timeout_zone = timeout_zone
        self.trace = []

    def random(self):
        value = next(self.rolls)
        self.trace.append(("random", value))
        return value

    def choice(self, values):
        catalogs = (
            (duel_text.SUICIDE_PHRASES, "suicide"),
            (duel_text.MISS_PHRASES, "miss"),
            (duel_text.BLOCK_PHRASES, "block"),
            (duel_text.HIT_PHRASES, "hit"),
            (duel_text.ATTACK_PHRASES, "attack"),
        )
        if values == ["head", "body", "dick"]:
            self.trace.append(("choice", "zone"))
            return self.timeout_zone
        for catalog, label in catalogs:
            if values is catalog:
                self.trace.append(("choice", label))
                return values[0]
        raise AssertionError(f"Unexpected random.choice catalog: {values!r}")


def install_rng(monkeypatch, rolls=(), timeout_zone="head"):
    rng = TraceRng(rolls, timeout_zone)
    monkeypatch.setattr(duel_service, "random", rng)
    return rng


def register_users():
    for user_id in (1, 2):
        database.get_or_create_duel_user(
            SimpleNamespace(
                id=user_id, username=f"player{user_id}", first_name=f"player{user_id}",
            ),
            CHAT_A,
        )


def test_activation_arms_ten_seconds_only_after_publication(temp_database, monkeypatch):
    session = make_session()
    rng = install_rng(monkeypatch)

    result = duel_service.activate_persistent_duel_turn(
        CHAT_A, session["id"], 1, 501, PUBLISHED_AT,
    )

    assert result.reason == "success" and result.accepted
    assert result.session["status"] == "active"
    assert result.session["phase"] == "attack"
    assert result.session["message_id"] == 501
    assert result.session["deadline_at"] == DEADLINE
    assert result.session["turn_id"] == 1
    assert result.session["updated_at"] == PUBLISHED_AT
    assert rng.trace == []


def test_activation_rejects_wrong_chat_turn_and_double_publish(temp_database, monkeypatch):
    session = make_session()
    rng = install_rng(monkeypatch)

    assert duel_service.activate_persistent_duel_turn(
        CHAT_B, session["id"], 1, 501, PUBLISHED_AT,
    ).reason == "not_found"
    assert duel_service.activate_persistent_duel_turn(
        CHAT_A, session["id"], 2, 501, PUBLISHED_AT,
    ).reason == "stale_turn"
    assert duel_service.activate_persistent_duel_turn(
        CHAT_A, session["id"], 1, 0, PUBLISHED_AT,
    ).reason == "invalid_message_id"
    assert get_duel_session(CHAT_A, session["id"])["status"] == "publishing"
    assert duel_service.activate_persistent_duel_turn(
        CHAT_A, session["id"], 1, 501, PUBLISHED_AT,
    ).reason == "success"
    assert duel_service.activate_persistent_duel_turn(
        CHAT_A, session["id"], 1, 502, PUBLISHED_AT + 1,
    ).reason == "not_publishing"
    assert get_duel_session(CHAT_A, session["id"])["message_id"] == 501
    assert rng.trace == []


@pytest.mark.parametrize("zone", ["head", "body", "dick"])
def test_attack_transitions_exactly_once_without_rng(temp_database, monkeypatch, zone):
    session = attack_session()
    rng = install_rng(monkeypatch)

    result = duel_service.submit_persistent_duel_attack(
        CHAT_A, session["id"], 1, 1, zone, now_ms=PUBLISHED_AT + 1,
    )

    assert result.reason == "success" and result.accepted
    state = result.session
    assert state["chat_id"] == CHAT_A
    assert state["status"] == "publishing"
    assert state["phase"] == "block"
    assert state["attack_zone"] == zone
    assert state["turn_id"] == 2
    assert state["round_no"] == 1
    assert (state["attacker_user_id"], state["defender_user_id"]) == (1, 2)
    assert state["deadline_at"] is None
    assert state["result"] is None
    assert rng.trace == []


@pytest.mark.parametrize(
    "operation,expected",
    [
        ("wrong_chat", "not_found"),
        ("wrong_duel", "not_found"),
        ("stale", "stale_turn"),
        ("defender", "wrong_actor"),
        ("invalid_zone", "invalid_zone"),
        ("wrong_phase", "wrong_phase"),
        ("publishing", "publishing"),
    ],
)
def test_attack_rejections_leave_state_and_rng_untouched(
    temp_database, monkeypatch, operation, expected,
):
    session = (
        make_session(status="active", phase="block", attack_zone="head")
        if operation == "wrong_phase" else
        make_session() if operation == "publishing" else attack_session()
    )
    rng = install_rng(monkeypatch)
    args = [CHAT_A, session["id"], 1, 1, "head"]
    if operation == "wrong_chat":
        args[0] = CHAT_B
    elif operation == "wrong_duel":
        args[1] += 1000
    elif operation == "stale":
        args[3] = 0
    elif operation == "defender":
        args[2] = 2
    elif operation == "invalid_zone":
        args[4] = "leg"

    before = get_duel_session(CHAT_A, session["id"])
    result = duel_service.submit_persistent_duel_attack(*args, now_ms=PUBLISHED_AT + 1)

    assert result.reason == expected
    assert result.session is None
    assert get_duel_session(CHAT_A, session["id"]) == before
    assert rng.trace == []


@pytest.mark.parametrize("status", ["cancelled", "finished"])
def test_inactive_session_rejects_actions_and_timeout_without_rng(
    temp_database, monkeypatch, status,
):
    session = create_duel_session(
        CHAT_A, 1, 2, snapshot(1), snapshot(2), 1, 2,
        status=status, result={} if status == "finished" else None,
        finished_at=PUBLISHED_AT if status == "finished" else None,
        now_ms=PUBLISHED_AT,
    )
    rng = install_rng(monkeypatch)

    assert duel_service.submit_persistent_duel_attack(
        CHAT_A, session["id"], 1, 1, "head", now_ms=PUBLISHED_AT,
    ).reason == "not_active"
    assert duel_service.resolve_persistent_duel_timeout(
        CHAT_A, session["id"], 1, DEADLINE,
    ).reason == "not_active"
    assert rng.trace == []


@pytest.mark.parametrize(
    "outcome,rolls,block_zone,expected_trace",
    [
        ("miss", (0.5, 0.0), "body", [
            ("random", 0.5), ("random", 0.0),
            ("choice", "miss"), ("choice", "attack"),
        ]),
        ("block", (0.5, 0.5), "head", [
            ("random", 0.5), ("random", 0.5),
            ("choice", "block"), ("choice", "attack"),
        ]),
    ],
)
def test_nonterminal_block_swaps_roles_and_checkpoints_rng(
    temp_database, monkeypatch, outcome, rolls, block_zone, expected_trace,
):
    session = block_session()
    rng = install_rng(monkeypatch, rolls)

    result = duel_service.submit_persistent_duel_block(
        CHAT_A, session["id"], 2, 2, block_zone, now_ms=PUBLISHED_AT + 1,
    )

    assert result.reason == "success" and result.accepted
    state = result.session
    assert state["status"] == "publishing"
    assert state["phase"] == "attack"
    assert state["attack_zone"] is None
    assert (state["attacker_user_id"], state["defender_user_id"]) == (2, 1)
    assert state["round_no"] == 4
    assert state["turn_id"] == 3
    assert state["deadline_at"] is None
    assert state["result"] == result.resolution
    assert result.resolution["kind"] == "round_resolution"
    assert result.resolution["outcome"] == outcome
    assert result.resolution["round_no"] == 3
    assert result.resolution["resolved_turn_id"] == 2
    assert rng.trace == expected_trace

    retry = duel_service.submit_persistent_duel_block(
        CHAT_A, session["id"], 2, 2, block_zone, now_ms=PUBLISHED_AT + 2,
    )
    assert retry.reason == "stale_turn"
    assert rng.trace == expected_trace


@pytest.mark.parametrize(
    "outcome,rolls,block_zone,expected_trace,winner,loser",
    [
        ("suicide", (0.0,), "body", [
            ("random", 0.0), ("choice", "suicide"),
        ], 2, 1),
        ("hit", (0.5, 0.5), "body", [
            ("random", 0.5), ("random", 0.5),
            ("choice", "hit"), ("choice", "attack"),
        ], 1, 2),
    ],
)
def test_terminal_checkpoint_is_persistent_once_without_game_effects(
    temp_database, monkeypatch, outcome, rolls, block_zone, expected_trace, winner, loser,
):
    register_users()
    database.add_duel_inventory_item(CHAT_A, 1, "po_lochki")
    session = block_session()
    rng = install_rng(monkeypatch, rolls)
    with sqlite3.connect(temp_database) as conn:
        users_before = conn.execute(
            "SELECT * "
            "FROM duel_users WHERE chat_id = ? ORDER BY user_id", (CHAT_A,),
        ).fetchall()
        inventory_before = conn.execute(
            "SELECT id, chat_id, user_id, item_id FROM duel_inventory "
            "WHERE chat_id = ?", (CHAT_A,),
        ).fetchall()

    result = duel_service.submit_persistent_duel_block(
        CHAT_A, session["id"], 2, 2, block_zone, now_ms=PUBLISHED_AT + 1,
    )

    assert result.reason == "terminal_pending" and result.accepted
    state = get_duel_session(CHAT_A, session["id"])
    assert state["status"] == "publishing"
    assert state["phase"] == "block"
    assert state["attack_zone"] == "head"
    assert state["round_no"] == 3
    assert state["turn_id"] == 3
    assert state["deadline_at"] is None
    assert state["finished_at"] is None
    assert state["result"] == result.resolution
    assert result.resolution["kind"] == "terminal_resolution"
    assert result.resolution["outcome"] == outcome
    assert (result.resolution["winner_user_id"], result.resolution["loser_user_id"]) == (
        winner, loser,
    )
    assert rng.trace == expected_trace

    retry = duel_service.submit_persistent_duel_block(
        CHAT_A, session["id"], 2, 2, block_zone, now_ms=PUBLISHED_AT + 2,
    )
    assert retry.reason == "terminal_pending"
    assert duel_service.activate_persistent_duel_turn(
        CHAT_A, session["id"], 3, 502, PUBLISHED_AT + 2,
    ).reason == "terminal_pending"
    assert rng.trace == expected_trace
    with sqlite3.connect(temp_database) as conn:
        assert conn.execute(
            "SELECT * "
            "FROM duel_users WHERE chat_id = ? ORDER BY user_id", (CHAT_A,),
        ).fetchall() == users_before
        assert conn.execute(
            "SELECT id, chat_id, user_id, item_id FROM duel_inventory "
            "WHERE chat_id = ?", (CHAT_A,),
        ).fetchall() == inventory_before
        assert conn.execute(
            "SELECT COUNT(*) FROM monthly_chat_stats WHERE chat_id = ?", (CHAT_A,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "operation,expected",
    [
        ("wrong_chat", "not_found"),
        ("wrong_duel", "not_found"),
        ("stale", "stale_turn"),
        ("attacker", "wrong_actor"),
        ("invalid_zone", "invalid_zone"),
        ("wrong_phase", "wrong_phase"),
        ("publishing", "publishing"),
    ],
)
def test_block_rejections_consume_zero_rng(
    temp_database, monkeypatch, operation, expected,
):
    session = (
        attack_session() if operation == "wrong_phase" else
        make_session(phase="block", attack_zone="head", turn_id=2)
        if operation == "publishing" else block_session()
    )
    rng = install_rng(monkeypatch)
    args = [CHAT_A, session["id"], 2, 2, "head"]
    if operation == "wrong_chat":
        args[0] = CHAT_B
    elif operation == "wrong_duel":
        args[1] += 1000
    elif operation == "stale":
        args[3] -= 1
    elif operation == "attacker":
        args[2] = 1
    elif operation == "invalid_zone":
        args[4] = "leg"
    elif operation == "wrong_phase":
        args[3] = 1

    before = get_duel_session(CHAT_A, session["id"])
    result = duel_service.submit_persistent_duel_block(*args, now_ms=PUBLISHED_AT + 1)

    assert result.reason == expected
    assert result.session is None
    assert get_duel_session(CHAT_A, session["id"]) == before
    assert rng.trace == []


def test_nonterminal_prompt_can_be_activated_without_new_turn_or_rng(
    temp_database, monkeypatch,
):
    session = block_session()
    rng = install_rng(monkeypatch, (0.5, 0.5))
    result = duel_service.submit_persistent_duel_block(
        CHAT_A, session["id"], 2, 2, "head", now_ms=PUBLISHED_AT + 1,
    )
    before_activation_trace = list(rng.trace)

    activated = duel_service.activate_persistent_duel_turn(
        CHAT_A, session["id"], result.session["turn_id"], 502, PUBLISHED_AT + 50,
    )

    assert activated.reason == "success"
    assert activated.session["status"] == "active"
    assert activated.session["turn_id"] == 3
    assert activated.session["deadline_at"] == PUBLISHED_AT + 50 + 10_000
    assert activated.session["message_id"] == 502
    assert rng.trace == before_activation_trace


def test_timeout_not_due_and_exact_boundary_for_attack(temp_database, monkeypatch):
    session = attack_session()
    rng = install_rng(monkeypatch, timeout_zone="dick")

    early = duel_service.resolve_persistent_duel_timeout(
        CHAT_A, session["id"], 1, DEADLINE - 1,
    )
    assert early.reason == "not_due"
    assert rng.trace == []

    due = duel_service.resolve_persistent_duel_timeout(
        CHAT_A, session["id"], 1, DEADLINE,
    )
    assert due.reason == "success"
    assert due.timeout_zone == "dick"
    assert due.session["phase"] == "block"
    assert due.session["attack_zone"] == "dick"
    assert due.session["status"] == "publishing"
    assert due.session["turn_id"] == 2
    assert rng.trace == [("choice", "zone")]

    duplicate = duel_service.resolve_persistent_duel_timeout(
        CHAT_A, session["id"], 1, DEADLINE + 1,
    )
    assert duplicate.reason == "stale_turn"
    assert rng.trace == [("choice", "zone")]


def test_block_timeout_uses_zone_then_same_round_resolution(temp_database, monkeypatch):
    session = block_session()
    rng = install_rng(monkeypatch, (0.5, 0.0), timeout_zone="body")

    result = duel_service.resolve_persistent_duel_timeout(
        CHAT_A, session["id"], 2, DEADLINE,
    )

    assert result.reason == "success"
    assert result.timeout_zone == "body"
    assert result.resolution["outcome"] == "miss"
    assert result.session["phase"] == "attack"
    assert rng.trace == [
        ("choice", "zone"),
        ("random", 0.5), ("random", 0.0),
        ("choice", "miss"), ("choice", "attack"),
    ]


def test_terminal_timeout_checkpoints_once_without_retry_rng(temp_database, monkeypatch):
    session = block_session()
    rng = install_rng(monkeypatch, (0.0,), timeout_zone="body")

    first = duel_service.resolve_persistent_duel_timeout(
        CHAT_A, session["id"], 2, DEADLINE,
    )

    assert first.reason == "terminal_pending"
    assert first.resolution["outcome"] == "suicide"
    assert first.timeout_zone == "body"
    assert first.session["status"] == "publishing"
    assert first.session["deadline_at"] is None
    assert rng.trace == [
        ("choice", "zone"), ("random", 0.0), ("choice", "suicide"),
    ]
    assert duel_service.resolve_persistent_duel_timeout(
        CHAT_A, session["id"], 2, DEADLINE + 1,
    ).reason == "terminal_pending"
    assert rng.trace == [
        ("choice", "zone"), ("random", 0.0), ("choice", "suicide"),
    ]


def test_publishing_timeout_is_rejected_before_rng(temp_database, monkeypatch):
    session = make_session()
    rng = install_rng(monkeypatch)
    assert duel_service.resolve_persistent_duel_timeout(
        CHAT_A, session["id"], 1, DEADLINE,
    ).reason == "publishing"
    assert rng.trace == []


def test_timeout_rejections_and_user_action_after_timeout_use_no_extra_rng(
    temp_database, monkeypatch,
):
    session = attack_session()
    rng = install_rng(monkeypatch, timeout_zone="head")
    assert duel_service.resolve_persistent_duel_timeout(
        CHAT_B, session["id"], 1, DEADLINE,
    ).reason == "not_found"
    assert duel_service.resolve_persistent_duel_timeout(
        CHAT_A, session["id"], 0, DEADLINE,
    ).reason == "stale_turn"
    assert rng.trace == []

    assert duel_service.resolve_persistent_duel_timeout(
        CHAT_A, session["id"], 1, DEADLINE,
    ).reason == "success"
    assert duel_service.submit_persistent_duel_attack(
        CHAT_A, session["id"], 1, 1, "body", now_ms=DEADLINE + 1,
    ).reason == "stale_turn"
    assert rng.trace == [("choice", "zone")]


def test_user_action_winning_before_timeout_makes_timeout_rng_free(temp_database, monkeypatch):
    session = attack_session()
    rng = install_rng(monkeypatch)
    assert duel_service.submit_persistent_duel_attack(
        CHAT_A, session["id"], 1, 1, "body", now_ms=DEADLINE,
    ).reason == "success"
    assert duel_service.resolve_persistent_duel_timeout(
        CHAT_A, session["id"], 1, DEADLINE,
    ).reason == "stale_turn"
    assert rng.trace == []


@pytest.mark.parametrize("phase", ["attack", "block"])
def test_timeout_vs_user_action_race_has_one_transition_and_one_rng_trace(
    temp_database, monkeypatch, phase,
):
    session = attack_session() if phase == "attack" else block_session()
    rng = install_rng(monkeypatch, (0.5, 0.5), timeout_zone="head")
    barrier = Barrier(2)
    expected_turn = session["turn_id"]

    def timeout_action():
        barrier.wait()
        return duel_service.resolve_persistent_duel_timeout(
            CHAT_A, session["id"], expected_turn, DEADLINE,
        )

    def user_action():
        barrier.wait()
        if phase == "attack":
            return duel_service.submit_persistent_duel_attack(
                CHAT_A, session["id"], 1, expected_turn, "head", now_ms=DEADLINE,
            )
        return duel_service.submit_persistent_duel_block(
            CHAT_A, session["id"], 2, expected_turn, "head", now_ms=DEADLINE,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        timeout_future = pool.submit(timeout_action)
        user_future = pool.submit(user_action)
        timeout_result = timeout_future.result()
        user_result = user_future.result()

    assert sum(result.accepted for result in (timeout_result, user_result)) == 1
    assert get_duel_session(CHAT_A, session["id"])["turn_id"] == expected_turn + 1
    assert get_duel_session(CHAT_A, session["id"])["status"] == "publishing"
    if phase == "attack":
        assert rng.trace == (
            [("choice", "zone")] if timeout_result.accepted else []
        )
    else:
        expected_resolution_trace = [
            ("random", 0.5), ("random", 0.5),
            ("choice", "block"), ("choice", "attack"),
        ]
        assert rng.trace == (
            [("choice", "zone")] if timeout_result.accepted else []
        ) + expected_resolution_trace
