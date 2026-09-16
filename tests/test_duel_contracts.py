import re
from types import SimpleNamespace


def callback_data(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_duel_keyboard_callback_data_contract():
    from handlers import duel

    assert [
        button.text
        for row in duel._get_strike_keyboard(7).inline_keyboard
        for button in row
    ] == ["🎯 Голова", "🛡️ Торс", "🍆 Хуй"]
    assert callback_data(duel._get_strike_keyboard(7)) == [
        "duel_strike_head_7", "duel_strike_body_7", "duel_strike_dick_7"
    ]
    assert [
        button.text
        for row in duel._get_block_keyboard(8).inline_keyboard
        for button in row
    ] == ["🛡️ Голова", "🛡️ Торс", "🛡️ Хуй"]
    assert callback_data(duel._get_block_keyboard(8)) == [
        "duel_block_head_8", "duel_block_body_8", "duel_block_dick_8"
    ]
    for value in callback_data(duel._get_strike_keyboard(7)) + callback_data(duel._get_block_keyboard(8)):
        assert re.fullmatch(r"duel_(strike|block)_(head|body|dick)_\d+", value)


def test_duel_public_callback_alias_and_pure_helpers():
    from handlers import duel
    from handlers import duel_input
    from handlers import duel_text

    assert duel.duel_action_callback is duel.duel_strike_callback
    assert duel.get_huyanie_title is duel_text.get_huyanie_title
    assert duel._plural_rounds is duel_text._plural_rounds
    assert duel._legacy_plural_rounds is duel_text._legacy_plural_rounds
    assert duel._legacy_plural_rounds_early is duel_text._legacy_plural_rounds_early
    assert duel.TARGET_NAMES is duel_text.TARGET_NAMES
    assert duel.ATTACK_PHRASES is duel_text.ATTACK_PHRASES
    assert duel.HIT_PHRASES is duel_text.HIT_PHRASES
    assert duel.BLOCK_PHRASES is duel_text.BLOCK_PHRASES
    assert duel.MISS_PHRASES is duel_text.MISS_PHRASES
    assert duel.SUICIDE_PHRASES is duel_text.SUICIDE_PHRASES
    assert duel.get_round_flavor_text is duel_text.get_round_flavor_text
    assert duel._extract_username is duel_input.extract_username
    for count in (0, 9, 10, 57, 100, 101):
        assert duel.get_huyanie_title(count) == duel._legacy_get_huyanie_title(count)
    for value in (0, 1, 2, 4, 5, 11, 12, 21, 25):
        assert duel._plural_rounds(value) == duel._legacy_plural_rounds(value)
    assert duel._extract_username(
        SimpleNamespace(message=SimpleNamespace(text="/duel @Somebody")),
        SimpleNamespace(args=[]),
    ) == "Somebody"
    assert duel._extract_username(
        SimpleNamespace(message=SimpleNamespace(text="/duel")),
        SimpleNamespace(args=[]),
    ) is None
    assert duel.get_huyanie_title(0)
    assert duel.get_round_flavor_text(1)


def test_round_flavor_text_keeps_all_round_boundaries(monkeypatch):
    from handlers import duel
    from handlers import duel_text

    monkeypatch.setattr(duel_text.random, "choice", lambda phrases: phrases[0])
    assert len({
        duel.get_round_flavor_text(1),
        duel.get_round_flavor_text(4),
        duel.get_round_flavor_text(8),
        duel.get_round_flavor_text(9),
    }) == 4
