import handlers.duel_text as duel_text
from handlers.duel_text import (
    ATTACK_PHRASES,
    BLOCK_PHRASES,
    HIT_PHRASES,
    MISS_PHRASES,
    SUICIDE_PHRASES,
    LOSS_TITLES,
    STOLEN_DICKS_TITLES,
    TARGET_NAMES,
    _boss_phase_status,
    _build_duel_block_text,
    _build_duel_miss_text,
    _plural_rounds,
    get_round_flavor_text,
)
from text_resources import get_text_list


def test_duel_phrase_catalog_keeps_yaml_order():
    assert ATTACK_PHRASES == get_text_list("duel.phrases.attack")
    assert ATTACK_PHRASES[0] == "замахивается засапожным свинорезом"
    assert ATTACK_PHRASES[-1] == "крутит подлый финт короткой гномьей заточкой"
    assert HIT_PHRASES == get_text_list("duel.phrases.hit")
    assert BLOCK_PHRASES == get_text_list("duel.phrases.block")
    assert MISS_PHRASES == get_text_list("duel.phrases.miss")
    assert SUICIDE_PHRASES == get_text_list("duel.phrases.suicide")
    assert [len(get_text_list(f"duel.round_flavor.{key}")) for key in ("one", "few", "several", "many")] == [13, 13, 13, 13]


def test_duel_yaml_preserves_sensitive_unicode_codepoints():
    assert TARGET_NAMES["body"] == "Торс 🛡️"
    assert LOSS_TITLES[900] == "☠️ Легенда Поражений"
    assert STOLEN_DICKS_TITLES[40] == "🦹‍♀️ Похититель Причиндалов"
    assert STOLEN_DICKS_TITLES[60] == "☠️ Пират Мошонки"

    assert [ord(char) for char in TARGET_NAMES["body"][-2:]] == [0x1F6E1, 0xFE0F]
    assert [ord(char) for char in STOLEN_DICKS_TITLES[40][:4]] == [
        0x1F9B9,
        0x200D,
        0x2640,
        0xFE0F,
    ]


def test_duel_text_static_templates_preserve_current_values():
    assert [_plural_rounds(value) for value in (1, 2, 5)] == ["раунд", "раунда", "раундов"]
    assert _boss_phase_status({"alive": False}, "attack") == "💀 погиб"
    assert _boss_phase_status({"alive": True, "attack": "head"}, "attack") == "🟢 выбрал"
    assert _boss_phase_status({"alive": True}, "block") == "🟡 выбирает"


def test_round_flavor_uses_one_choice_from_the_preserved_catalog(monkeypatch):
    selected_catalogs = []

    def choose_first(catalog):
        selected_catalogs.append(catalog)
        return catalog[0]

    monkeypatch.setattr(duel_text.random, "choice", choose_first)

    assert get_round_flavor_text(1) == get_text_list("duel.round_flavor.one")[0]
    assert get_round_flavor_text(4) == get_text_list("duel.round_flavor.few")[0]
    assert get_round_flavor_text(8) == get_text_list("duel.round_flavor.several")[0]
    assert get_round_flavor_text(9) == get_text_list("duel.round_flavor.many")[0]
    assert selected_catalogs == [
        get_text_list("duel.round_flavor.one"),
        get_text_list("duel.round_flavor.few"),
        get_text_list("duel.round_flavor.several"),
        get_text_list("duel.round_flavor.many"),
    ]


def test_build_duel_miss_text_composes_current_html():
    assert _build_duel_miss_text(
        "Первый",
        "делает замах",
        "head",
        "промахивается мимо цели!",
        "Второй",
        "Первый",
        30,
    ) == (
        "💨 <b>ПРОМАХ!</b>\n"
        "<b>Первый</b> делает замах "
        f"в зону ({TARGET_NAMES['head']}), "
        "но промахивается мимо цели!\n\n"
        "🔄 <b>Смена ролей!</b>\n"
        "⚔️ Атакует: <b>Второй</b>\n"
        "🛡️ Защищается: <b>Первый</b>\n\n"
        "⏳ У <b>Второй</b> есть 30 секунд на удар:"
    )


def test_build_duel_block_text_composes_current_html():
    assert _build_duel_block_text(
        "Первый",
        "Второй",
        "делает замах",
        "body",
        "ставит надёжный блок!",
        "Второй",
        "Первый",
        30,
    ) == (
        "🛡️ <b>БЛОК СРАБОТАЛ!</b>\n"
        "<b>Первый</b> делает замах "
        f"в зону ({TARGET_NAMES['body']}), "
        "но <b>Второй</b> ставит надёжный блок!\n\n"
        "🔄 <b>Инициатива переходит!</b>\n"
        "⚔️ Атакует: <b>Второй</b>\n"
        "🛡️ Защищается: <b>Первый</b>\n\n"
        "⏳ У <b>Второй</b> есть 30 секунд на удар:"
    )
