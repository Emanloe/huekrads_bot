"""Armor wording follows the defender's struck slot without granting protection."""

from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database as db
from handlers import duel_service, duel_text
from loot.grammar import armor_case_forms
from miniapp_api import _plain_duel_text, _round_read_model
from tests.test_duel_weapons import AXE, AXE_INSTRUMENTAL
from tests.test_equipment import award, register
from tests.test_persistent_duel_transitions import CHAT_A, PUBLISHED_AT, TraceRng, make_session


ARMOR = {
    "head": ("головной убор", "шлем", "шлемом"),
    "body": ("одежда", "нагрудник", "нагрудником"),
    "dick": ("пах", "гульфик", "гульфиком"),
}


class ArmorRng(TraceRng):
    def choice(self, values):
        selected = super().choice(values)
        if values is duel_text.HIT_PHRASES:
            return values[1]
        if values is duel_text.BLOCK_PHRASES:
            return values[2]
        return selected


@pytest.mark.parametrize("name,instrumental,accusative,prepositional", [
    ("Качественный карминовый шлем поглощения", "качественным карминовым шлемом поглощения",
     "качественный карминовый шлем поглощения", "качественном карминовом шлеме поглощения"),
    ("Качественный карминовый нагрудник поглощения", "качественным карминовым нагрудником поглощения",
     "качественный карминовый нагрудник поглощения", "качественном карминовом нагруднике поглощения"),
    ("Реликтовая дребезжащая маска силы", "реликтовой дребезжащей маской силы",
     "реликтовую дребезжащую маску силы", "реликтовой дребезжащей маске силы"),
    ("Качественное карминовое яростное забрало поглощения", "качественным карминовым яростным забралом поглощения",
     "качественное карминовое яростное забрало поглощения", "качественном карминовом яростном забрале поглощения"),
    ("Поганые разваливающиеся трусы силы", "погаными разваливающимися трусами силы",
     "поганые разваливающиеся трусы силы", "поганых разваливающихся трусах силы"),
    ("Качественный бережно хранимый с 90-х паховый щит поглощения",
     "качественным бережно хранимым с 90-х паховым щитом поглощения",
     "качественный бережно хранимый с 90-х паховый щит поглощения",
     "качественном бережно хранимом с 90-х паховом щите поглощения"),
    ("Реликтовый карминовый панцирь «Орех» силы", "реликтовым карминовым панцирем «Орех» силы",
     "реликтовый карминовый панцирь «Орех» силы", "реликтовом карминовом панцире «Орех» силы"),
])
def test_armor_forms_preserve_all_name_components(name, instrumental, accusative, prepositional):
    assert armor_case_forms(name) == {
        "armor_nominative": name, "armor_instrumental": instrumental,
        "armor_accusative": accusative, "armor_prepositional": prepositional,
    }


def test_empty_slot_vest_has_all_required_forms():
    assert armor_case_forms(None) == {
        "armor_nominative": "промасленная жилетка",
        "armor_instrumental": "промасленной жилеткой",
        "armor_accusative": "промасленную жилетку",
        "armor_prepositional": "промасленной жилетке",
    }


def test_only_equipment_descriptions_insert_armor():
    name = "Качественный карминовый шлем поглощения"
    hit = duel_text.format_duel_weapon_phrase(duel_text.HIT_PHRASES[1], AXE, armor_name=name)
    block = duel_text.format_duel_weapon_phrase(duel_text.BLOCK_PHRASES[2], AXE, armor_name=name)
    assert hit == "пробивает качественный карминовый шлем поглощения и добирается до мяса!"
    assert block == "прикрывается качественным карминовым шлемом поглощения и ржёт противнику в лицо!"
    for phrase in (*duel_text.HIT_PHRASES, *duel_text.BLOCK_PHRASES, *duel_text.MISS_PHRASES):
        if "{armor_" not in phrase:
            assert duel_text.format_duel_weapon_phrase(phrase, AXE, armor_name=name) == (
                duel_text.format_duel_weapon_phrase(phrase, AXE))


@pytest.mark.parametrize("name", ["Непонятная защита <&>", "Качественный карминовый трилби поглощения"])
def test_unknown_armor_is_preserved_in_neutral_quotes(name):
    forms = armor_case_forms(name)
    assert forms == {
        "armor_nominative": f"предмет «{name}»", "armor_accusative": f"предмет «{name}»",
        "armor_instrumental": f"предметом «{name}»", "armor_prepositional": f"предмете «{name}»",
    }
    text = duel_text.format_duel_weapon_phrase("прикрывается {armor_instrumental}", AXE, armor_name=name)
    assert escape(name) in text
    assert "<&>" not in text


def test_armor_read_is_limited_to_owned_struck_slot(temp_database):
    register(1, CHAT_A)
    register(2, CHAT_A)
    names = {}
    for zone, (kind, noun, _) in ARMOR.items():
        name = f"Качественный карминовый {noun} поглощения"
        instance = award(f"defender_{zone}", name, kind, user_id=2, chat_id=CHAT_A)
        award(f"attacker_{zone}", f"Предмет атакующего {zone}", kind, chat_id=CHAT_A)
        names[zone] = name
        if zone == "head":
            award("free_helmet", "Свободный шлем", kind, user_id=2, chat_id=CHAT_A)
            assert db.change_generated_equipment(CHAT_A, 2, "head", instance["id"]) == "equipped"
    award("outerwear", "Кожаная броня", "верхняя одежда", user_id=2, chat_id=CHAT_A)
    assert {zone: db.get_equipped_duel_armor_name(CHAT_A, 2, zone) for zone in ARMOR} == names
    assert db.change_generated_equipment(CHAT_A, 2, "head") == "unequipped"
    assert db.change_generated_equipment(CHAT_A, 2, "clothing") == "unequipped"
    inventory = db.get_duel_inventory(CHAT_A, 2)
    assert db.get_equipped_duel_armor_name(CHAT_A, 2, "head") is None
    assert db.get_equipped_duel_armor_name(CHAT_A, 2, "body") is None
    assert db.get_equipped_duel_armor_name(CHAT_A - 1, 2, "dick") is None
    assert db.get_equipped_duel_armor_name(CHAT_A, 3, "dick") is None
    assert db.get_duel_inventory(CHAT_A, 2) == inventory


@pytest.mark.parametrize("zone", ARMOR)
def test_block_uses_defenders_struck_slot_and_keeps_armor(temp_database, monkeypatch, zone):
    register(1, CHAT_A)
    register(2, CHAT_A)
    award("axe", AXE, "оружие", chat_id=CHAT_A)
    for armor_zone, (kind, noun, _) in ARMOR.items():
        award(f"def_{armor_zone}", f"Качественный карминовый {noun} поглощения",
              kind, user_id=2, chat_id=CHAT_A)
        award(f"atk_{armor_zone}", f"Броня атакующего {armor_zone}", kind, chat_id=CHAT_A)
    session = make_session(status="active", phase="block", attack_zone=zone, turn_id=2)
    inventory = db.get_duel_inventory(CHAT_A, 2)
    rng = ArmorRng((0.5, 0.5))
    monkeypatch.setattr(duel_service, "random", rng)
    result = duel_service.submit_persistent_duel_block(
        CHAT_A, session["id"], 2, 2, zone, now_ms=PUBLISHED_AT + 1,
    )
    text = result.resolution["presentation_html"]
    assert result.resolution["outcome"] == "block"
    assert f"качественным карминовым {ARMOR[zone][2]} поглощения" in text
    assert AXE_INSTRUMENTAL in text
    assert "Броня атакующего" not in text and "кожаный наруч" not in text
    assert "промасленн" not in text
    assert db.get_duel_inventory(CHAT_A, 2) == inventory
    assert rng.trace == [("random", 0.5), ("random", 0.5), ("choice", "block"), ("choice", "attack")]
    with db.get_db() as conn:
        conn.execute("UPDATE generated_item_names SET name = 'Другое название' WHERE item_id = ?",
                     (f"def_{zone}",))
    db.init_db()
    assert text in duel_service._prompt_payload(result.session)["text"]
    assert _round_read_model(result.session, result.resolution)["presentation_text"] == _plain_duel_text(text)


@pytest.mark.parametrize("zone", ARMOR)
def test_broken_armor_is_named_once_and_next_hit_uses_text_only_vest(temp_database, monkeypatch, zone):
    register(1, CHAT_A)
    register(2, CHAT_A)
    kind, noun, _ = ARMOR[zone]
    name = f"Качественный карминовый {noun} поглощения"
    instance = award("breakable_armor", name, kind, user_id=2, chat_id=CHAT_A)
    session = make_session(status="active", phase="block", attack_zone=zone, turn_id=2)
    rng = ArmorRng((0.5, 0.5, 0.5, 0.0, 0.5, 0.5))
    monkeypatch.setattr(duel_service, "random", rng)
    wrong_zone = "body" if zone != "body" else "head"
    first = duel_service.submit_persistent_duel_block(
        CHAT_A, session["id"], 2, 2, wrong_zone, now_ms=PUBLISHED_AT + 1,
    )
    assert first.resolution["outcome"] == "absorbed"
    assert name in first.resolution["presentation_html"]
    assert "промасленн" not in first.resolution["presentation_html"]
    assert instance["id"] not in [item["id"] for item in db.get_duel_inventory(CHAT_A, 2)]
    assert db.get_equipped_duel_armor_name(CHAT_A, 2, zone) is None
    for turn, attacker, defender in ((3, 2, 1), (5, 1, 2)):
        assert duel_service.activate_persistent_duel_turn(
            CHAT_A, session["id"], turn, 500 + turn, PUBLISHED_AT + turn * 10,
        ).reason == "success"
        assert duel_service.submit_persistent_duel_attack(
            CHAT_A, session["id"], attacker, turn, zone, now_ms=PUBLISHED_AT + turn * 10 + 1,
        ).reason == "success"
        assert duel_service.activate_persistent_duel_turn(
            CHAT_A, session["id"], turn + 1, 501 + turn, PUBLISHED_AT + turn * 10 + 2,
        ).reason == "success"
        current = duel_service.submit_persistent_duel_block(
            CHAT_A, session["id"], defender, turn + 1, wrong_zone, now_ms=PUBLISHED_AT + turn * 10 + 3,
        )
    assert current.resolution["outcome"] == "hit"
    assert "пробивает промасленную жилетку" in current.resolution["presentation_html"]
    assert noun not in current.resolution["presentation_html"]
    assert db.get_duel_inventory(CHAT_A, 2) == []
    assert "ножом" in current.resolution["attack_phrase"]
    assert [event for event in rng.trace if event[0] == "random"] == [
        ("random", 0.5), ("random", 0.5), ("random", 0.5), ("random", 0.0),
        ("random", 0.5), ("random", 0.5),
    ]
    monkeypatch.setattr(duel_service, "random", SimpleNamespace(
        random=lambda: 0.99, choice=lambda values: values[0],
    ))
    final = duel_service.finalize_persistent_duel(CHAT_A, session["id"], now_ms=PUBLISHED_AT + 100)
    assert final.result["winner_user_id"] == 1 and final.result["loser_user_id"] == 2
    assert final.result["winner_points_delta"] == 10 and final.result["loser_points_delta"] == -5
    assert "пробивает промасленную жилетку" in final.result["final_text"]
    assert db.get_duel_inventory(CHAT_A, 2) == []


@pytest.mark.asyncio
async def test_legacy_block_uses_same_armor_lookup_and_forms(temp_database, monkeypatch, fake_context):
    from handlers import duel
    from tests.test_duel_flow import CHAT_ID, install_fake_tasks, make_block_phase_duel

    register(602, CHAT_ID)
    name = "Качественный карминовый нагрудник поглощения"
    award("legacy_chest", name, "одежда", user_id=602, chat_id=CHAT_ID)
    state = make_block_phase_duel(strike_zone="body")
    monkeypatch.setitem(duel.ACTIVE_DUELS, CHAT_ID, state)
    install_fake_tasks(monkeypatch, duel)
    monkeypatch.setattr(duel, "random", ArmorRng((0.5, 0.5)))
    finish = AsyncMock()
    monkeypatch.setattr(duel, "_finish_duel", finish)
    await duel._process_block_choice(fake_context, CHAT_ID, "body")
    finish.assert_not_awaited()
    assert "качественным карминовым нагрудником поглощения" in (
        fake_context.bot.edit_message_text.await_args.kwargs["text"])
    assert db.get_equipped_duel_armor_name(CHAT_ID, 602, "body") == name
