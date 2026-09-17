from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import re

from text_resources import get_text


def callback_data(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def button_labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def test_boss_keyboard_callback_data_contract():
    from handlers import duel

    attack_markup = duel._boss_attack_keyboard(3)
    block_markup = duel._boss_block_keyboard(3)
    attack = callback_data(attack_markup)
    block = callback_data(block_markup)

    assert [len(row) for row in attack_markup.inline_keyboard] == [3]
    assert [len(row) for row in block_markup.inline_keyboard] == [3]
    assert button_labels(attack_markup) == ["⚔️ Голова", "⚔️ Торс", "⚔️ Хуй"]
    assert button_labels(block_markup) == ["🛡 Голова", "🛡 Торс", "🛡 Хуй"]
    assert attack == ["boss_attack_head_3", "boss_attack_body_3", "boss_attack_dick_3"]
    assert block == ["boss_block_head_3", "boss_block_body_3", "boss_block_dick_3"]
    for value in attack + block:
        assert re.fullmatch(r"boss_(attack|block)_(head|body|dick)_\d+", value)


def test_boss_runtime_constants_and_state_contract():
    from handlers import duel

    assert duel.BOSS_JOIN_TIMEOUT == 30
    assert duel.BOSS_REQUIRED_HITS == 5
    state = duel.ACTIVE_BOSS_BATTLES
    state.clear()
    state[-99] = {"phase": "join"}
    assert duel.ACTIVE_BOSS_BATTLES is state
    assert duel.ACTIVE_BOSS_BATTLES[-99] == {"phase": "join"}
    state.clear()


def test_boss_catalog_has_stable_order_shape_and_yaml_backed_presentation():
    from handlers import duel

    catalog_keys = [
        "deep_snouted_baron",
        "dick_crusher_face_eater",
        "prince_of_underground_chaos",
        "great_knife_beard",
        "dick_devourer",
    ]

    assert len(duel.BOSSES) == 5
    assert all(set(boss) == {"name", "emoji", "description"} for boss in duel.BOSSES)
    assert duel.BOSSES == [
        {
            "name": get_text(f"boss.catalog.{key}.name"),
            "emoji": get_text(f"boss.catalog.{key}.emoji"),
            "description": get_text(f"boss.catalog.{key}.description"),
        }
        for key in catalog_keys
    ]


def test_boss_registration_uses_isolated_database(tmp_path, monkeypatch, tg_user):
    from handlers import duel
    from handlers import boss_registration

    monkeypatch.setattr(boss_registration, "_BOSS_REG_DB_PATH", tmp_path / "registrations.db")
    monkeypatch.setattr(boss_registration, "_boss_today", lambda: "2025-01-01")
    assert duel._boss_register_user is boss_registration._boss_register_user
    assert duel._boss_get_registered_users is boss_registration._boss_get_registered_users
    assert duel._boss_clear_registrations is boss_registration._boss_clear_registrations
    assert duel._boss_get_registered_chat_ids is boss_registration._boss_get_registered_chat_ids
    assert duel._boss_register_user(-99, tg_user) is True
    assert duel._boss_register_user(-99, tg_user) is False
    assert duel._boss_get_registered_users(-99) == [(1001, "tester", "Tester", "User")]
    assert duel._boss_get_registered_chat_ids() == {-99}
    duel._boss_clear_registrations(-99)
    assert duel._boss_get_registered_users(-99) == []


@pytest.mark.asyncio
async def test_boss_registration_messages_use_current_participant_count(
    tmp_path,
    monkeypatch,
    tg_user,
):
    from handlers import boss_registration
    from handlers import duel

    chat_id = -994
    context = object()
    send = AsyncMock()
    second_user = SimpleNamespace(
        id=1002,
        username="second",
        first_name="Second",
        last_name="User",
    )
    monkeypatch.setattr(boss_registration, "_BOSS_REG_DB_PATH", tmp_path / "registrations.db")
    monkeypatch.setattr(boss_registration, "_boss_today", lambda: "2025-01-01")
    monkeypatch.setattr(duel, "_boss_registration_is_open", lambda: True)
    monkeypatch.setattr(duel, "set_boss_enabled", lambda *_: None)
    monkeypatch.setattr(duel, "send_and_schedule", send)
    duel.ACTIVE_BOSS_BATTLES.clear()

    for user, expected_count in ((tg_user, 1), (second_user, 2), (tg_user, 2)):
        update = SimpleNamespace(
            message=SimpleNamespace(
                from_user=user,
                chat=SimpleNamespace(id=chat_id, type="group"),
            )
        )

        await duel.boss_reg_command(update, context)

        sent = send.await_args_list[-1]
        assert sent.args[2].endswith(
            f"\n\n Записано участников: <b>{expected_count}</b>"
        )
        assert sent.kwargs == {"parse_mode": "HTML"}

    assert [row[0] for row in duel._boss_get_registered_users(chat_id)] == [1001, 1002]
    assert "Ты уже записан" in send.await_args_list[-1].args[2]
    duel.ACTIVE_BOSS_BATTLES.clear()


@pytest.mark.asyncio
async def test_boss_registration_closed_message_preserves_current_text(monkeypatch, tg_user):
    from handlers import duel

    send = AsyncMock()
    context = object()
    update = SimpleNamespace(
        message=SimpleNamespace(
            from_user=tg_user,
            chat=SimpleNamespace(id=-99, type="group"),
        )
    )
    monkeypatch.setattr(duel, "_boss_registration_is_open", lambda: False)
    monkeypatch.setattr(
        duel,
        "_boss_get_registered_users",
        lambda *_: pytest.fail("closed registration must not read participants"),
    )
    monkeypatch.setattr(duel, "send_and_schedule", send)

    await duel.boss_reg_command(update, context)

    send.assert_awaited_once_with(
        update,
        context,
        "Извинитесь. Битва уже была, запишитесь завтра до 18:00",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_type", "registration_open", "active", "added", "expected", "parse_mode"),
    [
        ("private", True, False, True, "⚔️ Записываться на гномью бойню можно только в группе.", None),
        ("group", False, False, True, "Извинитесь. Битва уже была, запишитесь завтра до 18:00", None),
        ("group", True, True, True, "⚔️ Битва уже идёт. На неё запись закрыта.", None),
        ("group", True, False, True, "⚔️ <b>Гном записан на сегодняшнюю бойню.</b>\n\nВ 18:00 твоя борода сама окажется на арене. Нож бери с собой.\n\n Записано участников: <b>1</b>", "HTML"),
        ("group", True, False, False, "🍺 Ты уже записан на сегодняшнюю бойню.\n\nВ 18:00 просто приходи рубиться.\n\n Записано участников: <b>1</b>", "HTML"),
    ],
)
async def test_boss_registration_presentation_branches_are_exact(
    monkeypatch,
    tg_user,
    chat_type,
    registration_open,
    active,
    added,
    expected,
    parse_mode,
):
    from handlers import duel

    chat_id = -992
    update = SimpleNamespace(
        message=SimpleNamespace(
            from_user=tg_user,
            chat=SimpleNamespace(id=chat_id, type=chat_type),
        )
    )
    send = AsyncMock()
    context = object()
    monkeypatch.setattr(duel, "send_and_schedule", send)
    monkeypatch.setattr(duel, "_boss_registration_is_open", lambda: registration_open)
    monkeypatch.setattr(duel, "set_boss_enabled", lambda *_: None)
    monkeypatch.setattr(duel, "_boss_register_user", lambda *_: added)
    monkeypatch.setattr(duel, "_boss_get_registered_users", lambda *_: [object()])
    duel.ACTIVE_BOSS_BATTLES.clear()
    if active:
        duel.ACTIVE_BOSS_BATTLES[chat_id] = {"phase": "join"}

    await duel.boss_reg_command(update, context)

    kwargs = {"parse_mode": parse_mode} if parse_mode else {}
    send.assert_awaited_once_with(update, context, expected, **kwargs)
    duel.ACTIVE_BOSS_BATTLES.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "chat_id", "active", "expected"),
    [
        (0, -993, False, "⛔ Недостаточно прав."),
        (1, 993, False, "👹 Босс запускается только в групповом чате."),
        (1, -993, True, "👹 В этом чате уже идет битва с боссом."),
    ],
)
async def test_boss_command_presentation_branches_are_exact(
    monkeypatch,
    user_id,
    chat_id,
    active,
    expected,
):
    from handlers import duel

    admin_id = 1
    monkeypatch.setattr(duel, "ADMIN_IDS", {admin_id})
    update = SimpleNamespace(
        message=SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            chat=SimpleNamespace(id=chat_id),
            chat_id=chat_id,
        )
    )
    send = AsyncMock()
    context = object()
    monkeypatch.setattr(duel, "send_and_schedule", send)
    duel.ACTIVE_BOSS_BATTLES.clear()
    if active:
        duel.ACTIVE_BOSS_BATTLES[chat_id] = {"phase": "join"}

    await duel.boss_command(update, context)

    send.assert_awaited_once_with(update, context, expected)
    duel.ACTIVE_BOSS_BATTLES.clear()


def test_boss_state_helpers():
    from handlers import duel
    from handlers import duel_formatting
    from handlers import duel_text

    battle = {"participants": {1: {"alive": True}, 2: {"alive": False}}}
    assert duel._boss_alive_players is duel_text._boss_alive_players
    assert duel._boss_all_alive_chosen is duel_text._boss_all_alive_chosen
    assert duel._boss_phase_status is duel_text._boss_phase_status
    assert duel._boss_battle_hero is duel_text._boss_battle_hero
    assert duel._boss_player_title is duel_formatting.boss_player_title
    assert duel._boss_players_status_text is duel_formatting._boss_players_status_text
    assert duel._boss_phase_text is duel_formatting._boss_phase_text
    assert duel._boss_alive_players(battle) == [{"alive": True}]
    assert duel._boss_all_alive_chosen({"participants": {1: {"alive": True, "attack": "head"}}}, "attack")
    participant = {"alive": True, "attack": "head", "block": None}
    assert duel._boss_phase_status(participant, "attack") == duel._legacy_boss_phase_status(participant, "attack")
    players = [
        {"alive": False, "hits": 2, "blocks": 0, "rounds_survived": 1},
        {"alive": True, "hits": 2, "blocks": 0, "rounds_survived": 1},
    ]
    assert duel._boss_battle_hero(players) == duel._legacy_boss_battle_hero(players)
    assert duel._boss_player_title({"data": {"username": "@boss_tester"}}) == "boss_tester"
    phase_battle = {
        "boss": {"name": "Тестер"}, "round": 2, "hits": 1, "phase": "attack",
        "participants": {1: {"alive": True, "data": {"username": "@alive"}, "attack": "head"}, 2: {"alive": False, "data": {"display_name": "Dead"}}},
    }
    assert f"<b>1 / {duel.BOSS_REQUIRED_HITS}</b>" in duel._boss_phase_text(
        phase_battle, duel.BOSS_REQUIRED_HITS
    )
    assert "<b>alive</b> — 🟢 выбрал" in duel._boss_players_status_text(phase_battle)
    phase_battle["phase"] = "block"
    assert "<b>ФАЗА ЗАЩИТЫ</b>" in duel._boss_phase_text(
        phase_battle, duel.BOSS_REQUIRED_HITS
    )
