"""Real equipped weapons, conservative Russian forms, and durable duel wording."""

from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database as db
from handlers import duel_service, duel_text
from handlers.duel_state import resolve_duel_round
from loot.grammar import weapon_case_forms
from tests.test_equipment import award, register
from tests.test_persistent_duel_transitions import CHAT_A, PUBLISHED_AT, TraceRng, make_session


AXE = "Качественный карминовый балансный топор поглощения"
AXE_INSTRUMENTAL = "качественным карминовым балансным топором поглощения"


@pytest.mark.parametrize("name,instrumental,accusative", [
    (AXE, AXE_INSTRUMENTAL, "качественный карминовый балансный топор поглощения"),
    ("меч", "мечом", "меч"), ("топор", "топором", "топор"),
    ("копьё", "копьём", "копьё"), ("булава", "булавой", "булаву"),
    ("тесак", "тесаком", "тесак"), ("лук", "луком", "лук"),
    ("арбалет", "арбалетом", "арбалет"), ("посох", "посохом", "посох"),
    ("трезубец", "трезубцем", "трезубец"), ("сабля", "саблей", "саблю"),
    ("Реликтовый острый двуручный меч силы", "реликтовым острым двуручным мечом силы",
     "реликтовый острый двуручный меч силы"),
    ("Качественная дребезжащая булава поглощения", "качественной дребезжащей булавой поглощения",
     "качественную дребезжащую булаву поглощения"),
    ("Реликтовое разваливающееся короткое копьё силы", "реликтовым разваливающимся коротким копьём силы",
     "реликтовое разваливающееся короткое копьё силы"),
    ("Поганый бывший в употреблении балансный нож поглощения",
     "поганым бывшим в употреблении балансным ножом поглощения",
     "поганый бывший в употреблении балансный нож поглощения"),
    ("Качественный бережно хранимый с 90-х боевой топор поглощения",
     "качественным бережно хранимым с 90-х боевым топором поглощения",
     "качественный бережно хранимый с 90-х боевой топор поглощения"),
    ("Ебейший подозрительно тёплый карминовый тесак поглощения",
     "ебейшим подозрительно тёплым карминовым тесаком поглощения",
     "ебейший подозрительно тёплый карминовый тесак поглощения"),
    ("охотничий лук", "охотничьим луком", "охотничий лук"),
])
def test_real_catalog_weapon_forms(name, instrumental, accusative):
    assert weapon_case_forms(name) == {
        "weapon_instrumental": instrumental, "weapon_accusative": accusative,
    }


@pytest.mark.parametrize("name", [
    "Неопознанный артефакт", "Качественный карминовый вирт'с лег поглощения",
    "Качественное карминовое военный копьё поглощения", "Меч <script>&",
])
def test_unknown_or_mismatched_names_keep_the_full_original_in_quotes(name):
    assert weapon_case_forms(name) == {
        "weapon_instrumental": f"оружием «{name}»",
        "weapon_accusative": f"оружие «{name}»",
    }
    text = duel_text.format_duel_weapon_phrase("атакует {weapon_instrumental}", name)
    assert escape(name) in text
    assert "<script>" not in text


def test_empty_weapon_uses_knife():
    assert weapon_case_forms(None) == {
        "weapon_instrumental": "ножом", "weapon_accusative": "нож",
    }


@pytest.mark.parametrize("catalog", [
    duel_text.ATTACK_PHRASES, duel_text.HIT_PHRASES, duel_text.MISS_PHRASES,
    duel_text.BLOCK_PHRASES, duel_text.SUICIDE_PHRASES,
])
def test_all_round_templates_resolve_without_fictional_weapons(catalog):
    for phrase in catalog:
        text = duel_text.format_duel_weapon_phrase(phrase, AXE)
        assert "{" not in text and "}" not in text
        assert not any(word in text for word in ("нож", "свинорез", "скальпель", "джамбий", "заточк"))
        if "{weapon_" in phrase:
            assert "карминов" in text and "топор" in text and "поглощения" in text


def test_weapon_read_uses_only_the_owned_slot_and_never_free_inventory(temp_database):
    register(1, CHAT_A)
    register(2, CHAT_A)
    register(1, CHAT_A - 1)
    equipped = award("equipped_axe", AXE, "оружие", chat_id=CHAT_A)
    award("free_sword", "Реликтовый острый меч силы", "оружие", chat_id=CHAT_A)
    award("other_player", "булава", "оружие", user_id=2, chat_id=CHAT_A)
    award("other_chat", "копьё", "оружие", chat_id=CHAT_A - 1)
    assert db.change_generated_equipment(CHAT_A, 1, "weapon", equipped["id"]) == "equipped"
    assert db.get_equipped_duel_weapon_name(CHAT_A, 1) == AXE
    assert db.get_equipped_duel_weapon_name(CHAT_A, 2) == "булава"
    assert db.get_equipped_duel_weapon_name(CHAT_A - 1, 1) == "копьё"
    assert db.change_generated_equipment(CHAT_A, 1, "weapon") == "unequipped"
    before = db.get_duel_inventory(CHAT_A, 1)
    assert db.get_equipped_duel_weapon_name(CHAT_A, 1) is None
    assert db.get_duel_inventory(CHAT_A, 1) == before
    assert "ножом" in resolve_duel_round("head", "body", TraceRng((0.5, 0.5))).attack_phrase


@pytest.mark.parametrize("outcome,rolls,block_zone", [
    ("hit", (0.5, 0.5), "body"), ("miss", (0.5, 0.0), "body"),
    ("block", (0.5, 0.5), "head"), ("suicide", (0.0,), "body"),
    ("absorbed", (0.5, 0.5), "body"),
])
def test_persistent_outcomes_use_attacker_weapon_and_preserve_game_effects(
    temp_database, monkeypatch, outcome, rolls, block_zone,
):
    register(1, CHAT_A)
    register(2, CHAT_A)
    award("attacker_axe", AXE, "оружие", chat_id=CHAT_A)
    award("defender_spear", "копьё", "оружие", user_id=2, chat_id=CHAT_A)
    if outcome == "absorbed":
        award("defender_hat", "Шапка", "головной убор", user_id=2, chat_id=CHAT_A)
    session = make_session(status="active", phase="block", attack_zone="head", turn_id=2)
    rng = TraceRng(rolls)
    monkeypatch.setattr(duel_service, "random", rng)
    inventory_before = db.get_duel_inventory(CHAT_A, 1)
    points_before = [db.get_duel_user_by_id(CHAT_A, uid)["points"] for uid in (1, 2)]
    result = duel_service.submit_persistent_duel_block(
        CHAT_A, session["id"], 2, 2, block_zone, now_ms=PUBLISHED_AT + 1,
    )
    assert result.resolution["outcome"] == outcome
    assert AXE_INSTRUMENTAL in result.resolution["presentation_html"]
    assert "копьём" not in result.resolution["presentation_html"]
    assert db.get_duel_inventory(CHAT_A, 1) == inventory_before
    assert [db.get_duel_user_by_id(CHAT_A, uid)["points"] for uid in (1, 2)] == points_before
    assert "head" not in db.get_duel_equipment(CHAT_A, 2)
    assert db.get_equipped_duel_weapon_name(CHAT_A, 2) == "копьё"
    expected_outcome_catalog = "hit" if outcome == "absorbed" else outcome
    expected_trace = [("random", rolls[0])]
    if outcome != "suicide":
        expected_trace.append(("random", rolls[1]))
    expected_trace.append(("choice", expected_outcome_catalog))
    if outcome != "suicide":
        expected_trace.append(("choice", "attack"))
    assert rng.trace == expected_trace
    if outcome in ("hit", "suicide"):
        assert result.resolution["winner_user_id"] == (2 if outcome == "suicide" else 1)
    # Re-rendering a saved round must not read new equipment or reroll the wording.
    with db.get_db() as conn:
        conn.execute("UPDATE generated_item_names SET name = 'Другое имя' WHERE item_id = 'attacker_axe'")
    if outcome not in ("hit", "suicide"):
        assert AXE_INSTRUMENTAL in duel_service._prompt_payload(result.session)["text"]
    else:
        assert AXE_INSTRUMENTAL in duel_service._terminal_custom_text(
            result.resolution, session["player1_snapshot"], session["player2_snapshot"],
        )
    assert rng.trace == expected_trace


@pytest.mark.parametrize("remove_weapon", [False, True])
def test_swapped_attacker_uses_current_weapon_after_protection_break(
    temp_database, monkeypatch, remove_weapon,
):
    register(1, CHAT_A)
    register(2, CHAT_A)
    award("first_axe", AXE, "оружие", chat_id=CHAT_A)
    spear = award("second_spear", "копьё", "оружие", user_id=2, chat_id=CHAT_A)
    award("second_hat", "Шапка", "головной убор", user_id=2, chat_id=CHAT_A)
    session = make_session(status="active", phase="block", attack_zone="head", turn_id=2)
    monkeypatch.setattr(duel_service, "random", TraceRng((0.5, 0.5, 0.5, 0.0)))
    first = duel_service.submit_persistent_duel_block(
        CHAT_A, session["id"], 2, 2, "body", now_ms=PUBLISHED_AT + 1,
    )
    assert first.resolution["outcome"] == "absorbed"
    assert AXE_INSTRUMENTAL in first.resolution["presentation_html"]
    assert "head" not in db.get_duel_equipment(CHAT_A, 2)
    if remove_weapon:
        with db.get_db() as conn:
            conn.execute("DELETE FROM duel_inventory WHERE id = ?", (spear["id"],))
    for turn_id, phase in ((3, "attack"), (4, "block")):
        activated = duel_service.activate_persistent_duel_turn(
            CHAT_A, session["id"], turn_id, 555, PUBLISHED_AT + turn_id * 3,
        )
        assert activated.reason == "success"
        if phase == "attack":
            assert duel_service.submit_persistent_duel_attack(
                CHAT_A, session["id"], 2, turn_id, "head", now_ms=PUBLISHED_AT + 10,
            ).reason == "success"
    second = duel_service.submit_persistent_duel_block(
        CHAT_A, session["id"], 1, 4, "body", now_ms=PUBLISHED_AT + 20,
    )
    assert second.resolution["outcome"] == "miss"
    assert ("ножом" if remove_weapon else "копьём") in second.resolution["attack_phrase"]
    assert "карминов" not in second.resolution["presentation_html"]
    assert second.resolution["attacker_user_id"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,rolls", [("hit", (0.5, 0.5)), ("suicide", (0.0,))])
async def test_legacy_duel_uses_the_same_equipped_weapon(
    temp_database, fake_context, monkeypatch, outcome, rolls,
):
    from handlers import duel
    from tests.test_duel_flow import make_block_phase_duel

    register(1, CHAT_A)
    register(2, CHAT_A)
    award("legacy_attacker_axe", AXE, "оружие", chat_id=CHAT_A)
    state = make_block_phase_duel(strike_zone="head", turn_id=2)
    state["attacker_data"] = db.get_duel_user_by_id(CHAT_A, 1)
    state["defender_data"] = db.get_duel_user_by_id(CHAT_A, 2)
    monkeypatch.setitem(duel.ACTIVE_DUELS, CHAT_A, state)
    monkeypatch.setattr(duel, "random", TraceRng(rolls))
    finish = AsyncMock()
    monkeypatch.setattr(duel, "_finish_duel", finish)
    await duel._process_block_choice(fake_context, CHAT_A, "body")
    finish.assert_awaited_once()
    assert AXE_INSTRUMENTAL in finish.await_args.kwargs["custom_text"]
    assert finish.await_args.kwargs["winner"]["user_id"] == (2 if outcome == "suicide" else 1)


def test_final_result_and_miniapp_keep_the_resolved_weapon_and_points(temp_database, monkeypatch):
    from miniapp_api import _round_read_model

    register(1, CHAT_A)
    register(2, CHAT_A)
    award("final_axe", AXE, "оружие", chat_id=CHAT_A)
    session = make_session(status="active", phase="block", attack_zone="head", turn_id=2)
    inventory = db.get_duel_inventory(CHAT_A, 1)
    monkeypatch.setattr(duel_service, "random", TraceRng((0.5, 0.5)))
    resolved = duel_service.submit_persistent_duel_block(
        CHAT_A, session["id"], 2, 2, "body", now_ms=PUBLISHED_AT + 1,
    )
    monkeypatch.setattr(duel_service, "random", SimpleNamespace(
        random=lambda: 0.99, choice=lambda values: values[0],
    ))
    final = duel_service.finalize_persistent_duel(CHAT_A, session["id"], now_ms=PUBLISHED_AT + 2)
    assert final.result["winner_points_delta"] == 10
    assert final.result["loser_points_delta"] == -5
    assert AXE_INSTRUMENTAL in final.result["final_text"]
    assert db.get_duel_inventory(CHAT_A, 1) == inventory
    model = _round_read_model(final.session, resolved.resolution)
    assert AXE_INSTRUMENTAL in model["presentation_text"]
