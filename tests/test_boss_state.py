import copy


def make_round_participant(
    *,
    alive=True,
    attack="head",
    block="head",
    hits=0,
    misses=0,
    blocks=0,
    rounds_survived=0,
):
    return {
        "alive": alive,
        "attack": attack,
        "block": block,
        "hits": hits,
        "misses": misses,
        "blocks": blocks,
        "rounds_survived": rounds_survived,
        "death_round": None,
        "death_by_zone": None,
        "death_defended_zone": None,
        "death_attack_zone": None,
    }


def make_round_battle(participants, *, hits=0, round_num=3):
    return {
        "participants": participants,
        "hits": hits,
        "round": round_num,
        "phase": "block",
        "boss_attack": "head",
        "boss_block": "body",
    }


def test_begin_boss_round_applies_normal_transition_to_all_participants():
    from handlers.boss_state import _begin_boss_round

    battle = {
        "round": 7,
        "phase": "resolving",
        "boss_attack": "body",
        "boss_block": "head",
        "participants": {
            1: {"attack": "head", "block": "dick", "alive": True},
            2: {"attack": "body", "block": "head", "alive": False},
        },
    }

    _begin_boss_round(battle, "dick", "body")

    assert battle["round"] == 8
    assert battle["phase"] == "attack"
    assert battle["boss_attack"] == "dick"
    assert battle["boss_block"] == "body"
    assert battle["participants"][1]["attack"] is None
    assert battle["participants"][1]["block"] is None
    assert battle["participants"][2]["attack"] is None
    assert battle["participants"][2]["block"] is None


def test_begin_boss_round_mutates_in_place_and_preserves_unrelated_state():
    from handlers.boss_state import _begin_boss_round

    first = {
        "attack": "head",
        "block": "body",
        "alive": True,
        "hits": 3,
        "custom": {"marker": 1},
    }
    second = {
        "attack": "dick",
        "block": "head",
        "alive": False,
        "death_round": 4,
    }
    participants = {10: first, 20: second}
    battle = {
        "round": 4,
        "phase": "custom-phase",
        "boss_attack": "head",
        "boss_block": "dick",
        "participants": participants,
        "hits": 9,
        "boss": {"name": "Неизменный Босс"},
        "message_id": 123,
    }
    unrelated_battle = {
        key: copy.deepcopy(value)
        for key, value in battle.items()
        if key not in {
            "round",
            "phase",
            "boss_attack",
            "boss_block",
            "participants",
        }
    }
    first_unrelated = {"alive": True, "hits": 3, "custom": {"marker": 1}}
    second_unrelated = {"alive": False, "death_round": 4}
    battle_identity = id(battle)
    participants_identity = id(participants)
    participant_identities = {user_id: id(value) for user_id, value in participants.items()}

    result = _begin_boss_round(battle, "body", "head")

    assert result is None
    assert id(battle) == battle_identity
    assert id(battle["participants"]) == participants_identity
    assert {
        user_id: id(value)
        for user_id, value in battle["participants"].items()
    } == participant_identities
    assert {
        key: value
        for key, value in battle.items()
        if key not in {
            "round",
            "phase",
            "boss_attack",
            "boss_block",
            "participants",
        }
    } == unrelated_battle
    assert {
        key: value
        for key, value in first.items()
        if key not in {"attack", "block"}
    } == first_unrelated
    assert {
        key: value
        for key, value in second.items()
        if key not in {"attack", "block"}
    } == second_unrelated


def test_record_boss_attack_choice_incomplete_mutates_in_place():
    from handlers.boss_state import _record_boss_attack_choice

    first = {"alive": True, "attack": None, "block": "head", "score": 3}
    second = {"alive": True, "attack": None, "block": "body", "score": 4}
    participants = {1: first, 2: second}
    battle = {
        "phase": "attack",
        "participants": participants,
        "round": 6,
    }

    completed = _record_boss_attack_choice(battle, first, "dick")

    assert completed is False
    assert battle["phase"] == "attack"
    assert first == {
        "alive": True,
        "attack": "dick",
        "block": "head",
        "score": 3,
    }
    assert second == {
        "alive": True,
        "attack": None,
        "block": "body",
        "score": 4,
    }
    assert battle["participants"] is participants
    assert battle["participants"][1] is first
    assert battle["participants"][2] is second
    assert battle["round"] == 6


def test_record_boss_attack_choice_overwrites_existing_choice():
    from handlers.boss_state import _record_boss_attack_choice

    participant = {
        "alive": True,
        "attack": "head",
        "block": "body",
        "custom": "unchanged",
    }
    battle = {
        "phase": "attack",
        "participants": {
            1: participant,
            2: {"alive": True, "attack": None, "block": None},
        },
    }

    completed = _record_boss_attack_choice(battle, participant, "dick")

    assert completed is False
    assert participant == {
        "alive": True,
        "attack": "dick",
        "block": "body",
        "custom": "unchanged",
    }


def test_record_boss_attack_completion_and_enter_block_phase():
    from handlers.boss_state import (
        _enter_boss_block_phase,
        _record_boss_attack_choice,
    )

    first = {"alive": True, "attack": "head", "block": "head"}
    last = {"alive": True, "attack": None, "block": "body"}
    battle = {
        "phase": "attack",
        "participants": {1: first, 2: last},
    }

    completed = _record_boss_attack_choice(battle, last, "body")

    assert completed is True
    assert battle["phase"] == "attack"
    assert first["block"] == "head"
    assert last["block"] == "body"

    _enter_boss_block_phase(battle)

    assert battle["phase"] == "block"
    assert first["block"] is None
    assert last["block"] is None


def test_enter_boss_block_phase_does_not_reset_dead_participant():
    from handlers.boss_state import _enter_boss_block_phase

    alive = {"alive": True, "attack": "head", "block": "body"}
    dead = {"alive": False, "attack": "dick", "block": "dick"}
    battle = {
        "phase": "attack",
        "participants": {1: alive, 2: dead},
        "custom": {"unchanged": True},
    }

    result = _enter_boss_block_phase(battle)

    assert result is None
    assert battle["phase"] == "block"
    assert alive["block"] is None
    assert dead["block"] == "dick"
    assert dead["attack"] == "dick"
    assert battle["custom"] == {"unchanged": True}


def test_record_boss_block_choice_incomplete_keeps_phase():
    from handlers.boss_state import _record_boss_block_choice

    first = {"alive": True, "attack": "head", "block": None, "score": 3}
    second = {"alive": True, "attack": "body", "block": None, "score": 4}
    participants = {1: first, 2: second}
    battle = {
        "phase": "block",
        "participants": participants,
        "round": 8,
    }

    completed = _record_boss_block_choice(battle, first, "head")

    assert completed is False
    assert battle["phase"] == "block"
    assert first == {
        "alive": True,
        "attack": "head",
        "block": "head",
        "score": 3,
    }
    assert second["block"] is None
    assert battle["participants"] is participants
    assert battle["participants"][1] is first
    assert battle["participants"][2] is second
    assert battle["round"] == 8


def test_record_boss_block_choice_overwrites_existing_choice():
    from handlers.boss_state import _record_boss_block_choice

    participant = {
        "alive": True,
        "attack": "head",
        "block": "head",
        "custom": "unchanged",
    }
    battle = {
        "phase": "block",
        "participants": {
            1: participant,
            2: {"alive": True, "attack": "body", "block": None},
        },
    }

    completed = _record_boss_block_choice(battle, participant, "dick")

    assert completed is False
    assert battle["phase"] == "block"
    assert participant == {
        "alive": True,
        "attack": "head",
        "block": "dick",
        "custom": "unchanged",
    }


def test_record_boss_block_choice_reports_completion_without_phase_change():
    from handlers.boss_state import _record_boss_block_choice

    first = {"alive": True, "attack": "head", "block": "head"}
    last = {"alive": True, "attack": "body", "block": None}
    dead = {"alive": False, "attack": "dick", "block": None}
    battle = {
        "phase": "block",
        "participants": {1: first, 2: last, 3: dead},
    }

    completed = _record_boss_block_choice(battle, last, "body")

    assert completed is True
    assert last["block"] == "body"
    assert dead["block"] is None
    assert battle["phase"] == "block"


def test_apply_boss_round_result_handles_mixed_round():
    from handlers.boss_state import _apply_boss_round_result

    survivor = make_round_participant(attack="head", block="head")
    killed = make_round_participant(attack="body", block="dick")
    battle = make_round_battle({1: survivor, 2: killed}, hits=2)

    result = _apply_boss_round_result(battle, required_hits=5)

    assert battle["hits"] == 3
    assert survivor["hits"] == 1
    assert survivor["misses"] == 0
    assert survivor["blocks"] == 1
    assert survivor["rounds_survived"] == 1
    assert survivor["alive"] is True
    assert killed["hits"] == 0
    assert killed["misses"] == 1
    assert killed["blocks"] == 0
    assert killed["rounds_survived"] == 0
    assert killed["alive"] is False
    assert killed["death_round"] == 3
    assert killed["death_by_zone"] == "head"
    assert killed["death_defended_zone"] == "dick"
    assert killed["death_attack_zone"] == "body"
    assert battle["phase"] == "resolving"
    assert result["victory"] is False
    assert result["defeat"] is False
    assert result["outcome"] == "continue"
    assert result["alive_after"] == 1
    assert result["round_results"] == [
        {
            "participant": survivor,
            "attack": "head",
            "block": "head",
            "hit": True,
            "survived": True,
            "boss_responded": True,
        },
        {
            "participant": killed,
            "attack": "body",
            "block": "dick",
            "hit": False,
            "survived": False,
            "boss_responded": True,
        },
    ]


def test_apply_boss_round_result_counts_hit_before_same_round_death():
    from handlers.boss_state import _apply_boss_round_result

    participant = make_round_participant(attack="head", block="body")
    battle = make_round_battle({1: participant})

    result = _apply_boss_round_result(battle, required_hits=5)

    assert battle["hits"] == 1
    assert participant["hits"] == 1
    assert participant["alive"] is False
    assert participant["death_round"] == 3
    assert result["outcome"] == "defeat"


def test_apply_boss_round_result_ignores_already_dead_participants():
    from handlers.boss_state import _apply_boss_round_result

    participant = make_round_participant(
        alive=False,
        attack="head",
        block="head",
        hits=4,
        misses=3,
        blocks=2,
        rounds_survived=1,
    )
    participant["death_round"] = 2
    participant["death_by_zone"] = "dick"
    participant["death_defended_zone"] = "body"
    participant["death_attack_zone"] = "head"
    before = copy.deepcopy(participant)
    battle = make_round_battle({1: participant}, hits=2)

    result = _apply_boss_round_result(battle, required_hits=5)

    assert participant == before
    assert battle["hits"] == 2
    assert battle["phase"] == "resolving"
    assert result["round_results"] == []
    assert result["alive_after"] == 0
    assert result["outcome"] == "defeat"


def test_apply_boss_round_result_reports_victory():
    from handlers.boss_state import _apply_boss_round_result

    participant = make_round_participant(attack="head", block="head")
    battle = make_round_battle({1: participant}, hits=4)

    result = _apply_boss_round_result(battle, required_hits=5)

    assert result["victory"] is True
    assert result["defeat"] is False
    assert result["outcome"] == "victory"
    assert result["alive_after"] == 1
    assert participant["alive"] is True
    assert participant["blocks"] == 0
    assert participant["rounds_survived"] == 1
    assert participant["death_round"] is None
    assert result["round_results"][0]["boss_responded"] is False
    assert result["round_results"][0]["block"] is None


def test_apply_boss_round_result_reports_defeat():
    from handlers.boss_state import _apply_boss_round_result

    participant = make_round_participant(attack="body", block="body")
    battle = make_round_battle({1: participant})

    result = _apply_boss_round_result(battle, required_hits=5)

    assert result["victory"] is False
    assert result["defeat"] is True
    assert result["outcome"] == "defeat"


def test_lethal_hit_prevents_boss_response_and_preserves_victory_survivor_invariant():
    from handlers.boss_state import _apply_boss_round_result

    participant = make_round_participant(attack="head", block="body")
    battle = make_round_battle({1: participant}, hits=4)

    result = _apply_boss_round_result(battle, required_hits=5)

    assert battle["hits"] == 5
    survivors = [
        player
        for player in battle["participants"].values()
        if player["alive"]
    ]
    assert survivors == [participant]
    assert result["victory"] is True
    assert result["defeat"] is False
    assert result["outcome"] == "victory"
    assert result["alive_after"] == len(survivors) == 1
    assert participant["blocks"] == 0
    assert participant["rounds_survived"] == 1
    assert participant["death_round"] is None
    assert participant["death_by_zone"] is None
    assert participant["death_defended_zone"] is None
    assert participant["death_attack_zone"] is None
    assert result["round_results"] == [
        {
            "participant": participant,
            "attack": "head",
            "block": None,
            "hit": True,
            "survived": True,
            "boss_responded": False,
        }
    ]


def test_lethal_hit_keeps_earlier_deaths_and_stops_later_participant_processing():
    from handlers.boss_state import _apply_boss_round_result

    already_dead = make_round_participant(alive=False, hits=1)
    already_dead["death_round"] = 1
    already_dead["death_by_zone"] = "dick"
    finisher = make_round_participant(attack="head", block="body")
    later_survivor = make_round_participant(attack="dick", block="dick")
    later_before = copy.deepcopy(later_survivor)
    battle = make_round_battle(
        {1: already_dead, 2: finisher, 3: later_survivor},
        hits=4,
    )

    result = _apply_boss_round_result(battle, required_hits=5)

    assert result["outcome"] == "victory"
    assert result["alive_after"] == 2
    assert already_dead["alive"] is False
    assert already_dead["death_round"] == 1
    assert already_dead["death_by_zone"] == "dick"
    assert finisher["alive"] is True
    assert finisher["hits"] == 1
    assert finisher["death_round"] is None
    assert later_survivor == later_before
    assert [entry["participant"] for entry in result["round_results"]] == [finisher]


def test_apply_boss_round_result_preserves_identity_and_unrelated_fields():
    from handlers.boss_state import _apply_boss_round_result

    alive = make_round_participant(attack="head", block="head")
    alive["custom"] = {"unchanged": True}
    dead = make_round_participant(alive=False, attack="dick", block="body")
    participants = {1: alive, 2: dead}
    battle = make_round_battle(participants, hits=1, round_num=9)
    battle["message_id"] = 700
    battle["custom"] = ["unchanged"]
    battle_identity = id(battle)
    participants_identity = id(participants)
    alive_identity = id(alive)
    dead_identity = id(dead)

    result = _apply_boss_round_result(battle, required_hits=10)

    assert id(battle) == battle_identity
    assert id(battle["participants"]) == participants_identity
    assert id(battle["participants"][1]) == alive_identity
    assert id(battle["participants"][2]) == dead_identity
    assert result["round_results"][0]["participant"] is alive
    assert battle["round"] == 9
    assert battle["boss_attack"] == "head"
    assert battle["boss_block"] == "body"
    assert battle["message_id"] == 700
    assert battle["custom"] == ["unchanged"]
    assert alive["custom"] == {"unchanged": True}
