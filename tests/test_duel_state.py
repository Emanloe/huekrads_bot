from handlers.duel_state import (
    _advance_duel_round,
    _build_duel_result_plan,
    _get_duel_participant_ineligibility,
    _is_miss_roll,
    _is_berserk_roll,
    _is_duel_post_message_roll,
    _is_suicide_roll,
    _resolve_zone_outcome,
    _set_attack_choice,
)


def test_duel_participant_ineligibility_returns_none_for_eligible_user():
    assert _get_duel_participant_ineligibility({
        "dick_stolen_today": False,
        "points": 1,
    }) is None


def test_duel_participant_ineligibility_reports_no_dick():
    assert _get_duel_participant_ineligibility({
        "dick_stolen_today": True,
        "points": 20,
    }) == "no_dick"


def test_duel_participant_ineligibility_reports_no_points_at_zero():
    assert _get_duel_participant_ineligibility({
        "dick_stolen_today": False,
        "points": 0,
    }) == "no_points"


def test_duel_participant_ineligibility_reports_no_points_below_zero():
    assert _get_duel_participant_ineligibility({
        "dick_stolen_today": False,
        "points": -1,
    }) == "no_points"


def test_duel_participant_ineligibility_prioritizes_no_dick():
    assert _get_duel_participant_ineligibility({
        "dick_stolen_today": True,
        "points": 0,
    }) == "no_dick"


def test_duel_participant_ineligibility_does_not_mutate_snapshot():
    user = {
        "dick_stolen_today": False,
        "points": 20,
        "unrelated": {"preserved": True},
    }
    before = {
        **user,
        "unrelated": user["unrelated"].copy(),
    }

    _get_duel_participant_ineligibility(user)

    assert user == before


def test_build_duel_result_plan_without_steal():
    plan = _build_duel_result_plan(
        {"user_id": 1, "points": 20},
        {"user_id": 2, "points": 20},
        False,
        "Winner",
        100,
    )

    assert plan == {
        "is_dick_stolen": False,
        "winner_reached_max": False,
        "winner": {
            "user_id": 1,
            "points": 30,
            "wins_increment": 1,
            "daily_wins_increment": 1,
            "stolen_dicks_count_increment": 0,
        },
        "loser": {
            "user_id": 2,
            "points": 15,
            "losses_increment": 1,
        },
    }
    assert "dick_stolen_count_increment" not in plan["loser"]
    assert "dick_stolen_today" not in plan["loser"]
    assert "last_stolen_by" not in plan["loser"]


def test_build_duel_result_plan_caps_points_and_marks_max_transition():
    plan = _build_duel_result_plan(
        {"user_id": 1, "points": 95},
        {"user_id": 2, "points": 3},
        False,
        "Winner",
        100,
    )

    assert plan["winner"]["points"] == 100
    assert plan["loser"]["points"] == 0
    assert plan["winner_reached_max"] is True
    assert _build_duel_result_plan(
        {"user_id": 1, "points": 89},
        {"user_id": 2, "points": 20},
        False,
        "Winner",
        100,
    )["winner_reached_max"] is False
    assert _build_duel_result_plan(
        {"user_id": 1, "points": 100},
        {"user_id": 2, "points": 20},
        False,
        "Winner",
        100,
    )["winner_reached_max"] is False


def test_build_duel_result_plan_with_steal():
    plan = _build_duel_result_plan(
        {"user_id": 1, "points": 40},
        {"user_id": 2, "points": 25},
        True,
        "Prepared Winner",
        100,
    )

    assert plan["winner"]["stolen_dicks_count_increment"] == 1
    assert plan["loser"] == {
        "user_id": 2,
        "points": 20,
        "losses_increment": 1,
        "dick_stolen_count_increment": 1,
        "dick_stolen_today": 1,
        "last_stolen_by": "Prepared Winner",
    }


def test_build_duel_result_plan_does_not_mutate_snapshots():
    winner = {"user_id": 1, "points": 95, "wins": 7, "nested": {"value": 1}}
    loser = {"user_id": 2, "points": 3, "losses": 4, "nested": {"value": 2}}
    winner_before = {**winner, "nested": winner["nested"].copy()}
    loser_before = {**loser, "nested": loser["nested"].copy()}

    _build_duel_result_plan(
        winner,
        loser,
        True,
        "Winner",
        100,
    )

    assert winner == winner_before
    assert loser == loser_before


def test_suicide_roll_boundary():
    assert _is_suicide_roll(0.009999) is True
    assert _is_suicide_roll(0.01) is False


def test_miss_roll_boundary():
    assert _is_miss_roll(0.049999) is True
    assert _is_miss_roll(0.05) is False


def test_berserk_roll_boundary():
    assert _is_berserk_roll(0.000999) is True
    assert _is_berserk_roll(0.001) is False


def test_duel_post_message_chance_and_roll_boundary():
    from config import DUEL_POST_MESSAGE_CHANCE

    assert DUEL_POST_MESSAGE_CHANCE == 0.10
    assert _is_duel_post_message_roll(0.099999) is True
    assert _is_duel_post_message_roll(0.10) is False


def test_zone_outcome_distinguishes_block_and_hit():
    assert _resolve_zone_outcome("head", "head") == "block"
    assert _resolve_zone_outcome("head", "body") == "hit"


def test_set_attack_choice_mutates_state_in_place():
    duel_state = {
        "phase": "attack",
        "attack_zone": None,
        "turn_id": 7,
        "unchanged": object(),
    }
    original_state = duel_state
    unchanged = duel_state["unchanged"]

    result = _set_attack_choice(duel_state, "body")

    assert result is None
    assert duel_state is original_state
    assert duel_state == {
        "phase": "block",
        "attack_zone": "body",
        "turn_id": 8,
        "unchanged": unchanged,
    }


def test_advance_duel_round_swaps_roles_and_mutates_state_in_place():
    attacker_tg = object()
    defender_tg = object()
    attacker_data = {"user_id": 1}
    defender_data = {"user_id": 2}
    duel_state = {
        "attacker_tg": attacker_tg,
        "defender_tg": defender_tg,
        "attacker_data": attacker_data,
        "defender_data": defender_data,
        "phase": "block",
        "attack_zone": "head",
        "round": 3,
        "turn_id": 12,
    }
    original_state = duel_state

    result = _advance_duel_round(duel_state)

    assert result is None
    assert duel_state is original_state
    assert duel_state["attacker_tg"] is defender_tg
    assert duel_state["defender_tg"] is attacker_tg
    assert duel_state["attacker_data"] is defender_data
    assert duel_state["defender_data"] is attacker_data
    assert duel_state["phase"] == "attack"
    assert duel_state["attack_zone"] is None
    assert duel_state["round"] == 4
    assert duel_state["turn_id"] == 13
