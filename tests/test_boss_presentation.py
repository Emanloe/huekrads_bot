from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from text_resources import get_text, get_text_list, get_text_mapping


def make_participant(
    username,
    *,
    alive=True,
    hits=0,
    misses=0,
    blocks=0,
    rounds_survived=0,
    death_round=None,
    death_attack_zone=None,
    death_defended_zone=None,
    death_by_zone=None,
):
    return {
        "data": {"username": username},
        "alive": alive,
        "hits": hits,
        "misses": misses,
        "blocks": blocks,
        "rounds_survived": rounds_survived,
        "death_round": death_round,
        "death_attack_zone": death_attack_zone,
        "death_defended_zone": death_defended_zone,
        "death_by_zone": death_by_zone,
    }


def test_boss_death_epitaph_uses_player_result_and_one_random_choice(monkeypatch):
    from handlers import boss_presentation

    choose = Mock(side_effect=lambda phrases: phrases[0])
    monkeypatch.setattr(boss_presentation.random, "choice", choose)
    participant = make_participant(
        "@fallen",
        alive=False,
        death_round=3,
        death_attack_zone="head",
        death_defended_zone="body",
        death_by_zone="dick",
    )

    result = boss_presentation._boss_death_epitaph(
        participant,
        "Тестовый Босс",
    )

    assert isinstance(result, str)
    assert result == (
        "💀 <b>fallen</b> пал в раунде <b>3</b>. "
        "Бил в <b>Голова</b>, защищал <b>Торс</b>, "
        "а <b>Тестовый Босс</b> пришёл в <b>Хуй</b>. "
        "Гномская разведка считает это смелостью. "
        "Гномская бухгалтерия — ошибкой."
    )
    choose.assert_called_once()
    assert len(choose.call_args.args[0]) == 4


@pytest.mark.parametrize("death_round", [None, 0, ""])
def test_boss_death_epitaph_preserves_death_round_truthiness_fallback(
    monkeypatch,
    death_round,
):
    from handlers import boss_presentation

    choose = Mock(return_value="Раунд: <b>{round_num}</b>.")
    monkeypatch.setattr(boss_presentation.random, "choice", choose)
    monkeypatch.setattr(boss_presentation, "_boss_player_title", lambda _: "fallen")

    result = boss_presentation._boss_death_epitaph(
        make_participant("@fallen", death_round=death_round),
        "Босс",
    )

    assert result == "Раунд: <b>последнем</b>."
    choose.assert_called_once_with(get_text_list("boss.death_epitaphs"))


def test_boss_death_epitaph_preserves_nonempty_death_round(monkeypatch):
    from handlers import boss_presentation

    choose = Mock(return_value="Раунд: <b>{round_num}</b>.")
    monkeypatch.setattr(boss_presentation.random, "choice", choose)
    monkeypatch.setattr(boss_presentation, "_boss_player_title", lambda _: "fallen")

    result = boss_presentation._boss_death_epitaph(
        make_participant("@fallen", death_round=3),
        "Босс",
    )

    assert result == "Раунд: <b>3</b>."
    choose.assert_called_once_with(get_text_list("boss.death_epitaphs"))


def test_boss_phase_formatting_preserves_statuses_and_exact_templates(monkeypatch):
    from handlers import duel_formatting

    monkeypatch.setattr(
        duel_formatting,
        "boss_player_title",
        lambda participant: participant["title"],
    )
    battle = {
        "boss": {"name": "Тестовый Босс"},
        "round": 4,
        "hits": 2,
        "phase": "attack",
        "participants": {
            1: {"title": "Первый", "alive": True, "attack": "head"},
            2: {"title": "Второй", "alive": True, "attack": None},
        },
    }

    assert duel_formatting._boss_players_status_text(battle) == (
        "• <b>Первый</b> — 🟢 выбрал\n"
        "• <b>Второй</b> — 🟡 выбирает"
    )
    assert duel_formatting._boss_phase_text(battle, 5) == (
        "💀 <b>Тестовый Босс — РАУНД 4</b>\n\n"
        "⚔️ <b>ФАЗА АТАКИ</b>\n"
        "Каждый живой игрок выбирает, куда ударить босса.\n\n"
        "🎯 Урон боссу: <b>2 / 5</b>\n"
        "👥 В живых: <b>2 / 2</b>\n\n"
        "<b>Игроки:</b>\n"
        "• <b>Первый</b> — 🟢 выбрал\n"
        "• <b>Второй</b> — 🟡 выбирает\n\n"
        "⚔️ Выберите зону атаки:"
    )

    battle["phase"] = "block"
    battle["participants"][1]["block"] = "body"
    assert duel_formatting._boss_phase_text(battle, 5) == (
        "💀 <b>Тестовый Босс — РАУНД 4</b>\n\n"
        "🛡 <b>ФАЗА ЗАЩИТЫ</b>\n"
        "Босс сейчас атакует. Каждый живой игрок выбирает, какую зону защищать.\n\n"
        "🎯 Урон боссу: <b>2 / 5</b>\n"
        "👥 В живых: <b>2 / 2</b>\n\n"
        "<b>Игроки:</b>\n"
        "• <b>Первый</b> — 🟢 выбрал\n"
        "• <b>Второй</b> — 🟡 выбирает\n\n"
        "🛡 Выберите зону защиты:"
    )


def test_boss_yaml_preserves_catalog_order_placeholders_and_unicode():
    epitaphs = get_text_list("boss.death_epitaphs")
    zones = get_text_mapping("boss.zones.display")

    assert len(epitaphs) == 4
    assert epitaphs[0].startswith("💀 <b>{title}</b> пал в раунде")
    assert epitaphs[-1].endswith("Совпадение? Нет. Судьба.")
    assert zones == {"head": "Голова", "body": "Торс", "dick": "Хуй"}
    assert get_text(
        "boss.report.victory.team",
        total=3,
        survivors=2,
        dead=1,
    ) == "👥 Отряд: <b>3</b> — выжило <b>2</b>, погибло <b>1</b>."
    assert get_text("boss.report.fallback") == (
        "💀 <b>БИТВА ОКОНЧЕНА</b>\\n\\n"
        "Босс больше не сражается. "
        "Гномская летопись почему-то отказалась писать подробности."
    )


@pytest.mark.parametrize(
    ("hits", "blocks", "expected_phrase"),
    [
        (2, 2, "рубился как настоящий гномий терминатор"),
        (2, 1, "превратил босса в тренировочную мишень"),
        (0, 2, "оказался подозрительно хорош в умении не умереть"),
        (1, 0, "хотя бы успел оставить на боссе несколько зарубок"),
        (0, 0, "выжил почти исключительно благодаря наглости"),
    ],
)
def test_boss_survivor_epitaph_preserves_stat_branches(
    monkeypatch,
    hits,
    blocks,
    expected_phrase,
):
    from handlers import boss_presentation

    choose = Mock(side_effect=AssertionError("survivor epitaph used RNG"))
    monkeypatch.setattr(boss_presentation.random, "choice", choose)
    participant = make_participant(
        "@survivor",
        hits=hits,
        misses=3,
        blocks=blocks,
        rounds_survived=4,
    )

    result = boss_presentation._boss_survivor_epitaph(participant)

    assert isinstance(result, str)
    assert result.startswith(f"🛡 <b>survivor</b> — выжил. {expected_phrase}.")
    assert (
        f"Попаданий: <b>{hits}</b>, промахов: <b>3</b>, "
        f"блоков: <b>{blocks}</b>, пережито раундов: <b>4</b>."
    ) in result
    choose.assert_not_called()


def test_boss_final_report_victory_preserves_sections_and_participant_order(
    monkeypatch,
):
    from handlers import boss_presentation

    choose = Mock(side_effect=lambda phrases: phrases[0])
    monkeypatch.setattr(boss_presentation.random, "choice", choose)
    first_survivor = make_participant(
        "@first_survivor", hits=1, misses=1, blocks=1, rounds_survived=3
    )
    dead = make_participant(
        "@fallen",
        alive=False,
        hits=2,
        misses=1,
        blocks=0,
        rounds_survived=2,
        death_round=3,
        death_attack_zone="head",
        death_defended_zone="body",
        death_by_zone="dick",
    )
    hero = make_participant(
        "@battle_hero", hits=3, misses=0, blocks=2, rounds_survived=4
    )
    battle = {
        "boss": {"name": "Тестовый Босс"},
        "hits": 6,
        "round": 4,
        "participants": {1: first_survivor, 2: dead, 3: hero},
    }

    report = boss_presentation._boss_final_report(battle, victory=True)

    assert report.startswith("🏆 <b>ЛЕГЕНДА БИТВЫ</b>")
    assert "👹 <b>Тестовый Босс</b> пал после <b>6</b> попаданий." in report
    assert "👥 Отряд: <b>3</b> — выжило <b>2</b>, погибло <b>1</b>." in report
    assert "👑 <b>ГЕРОЙ БИТВЫ: battle_hero</b>" in report
    assert "💯 <b>Награда:</b> всем выжившим установлено 100 очков." in report
    assert report.index("🛡 <b>ВЫЖИВШИЕ:</b>") < report.index("<b>first_survivor</b>")
    assert report.index("<b>first_survivor</b>") < report.index("<b>battle_hero</b> — выжил")
    assert report.index("<b>battle_hero</b> — выжил") < report.index("💀 <b>ПАВШИЕ ГЕРОИ:</b>")
    assert report.index("💀 <b>ПАВШИЕ ГЕРОИ:</b>") < report.index("<b>fallen</b> пал")
    choose.assert_called_once()


def test_boss_final_report_defeat_preserves_sections_and_dead_order(monkeypatch):
    from handlers import boss_presentation

    choose = Mock(side_effect=lambda phrases: phrases[0])
    monkeypatch.setattr(boss_presentation.random, "choice", choose)
    first_dead = make_participant(
        "@first_dead",
        alive=False,
        hits=1,
        blocks=1,
        rounds_survived=2,
        death_round=2,
        death_attack_zone="body",
        death_defended_zone="head",
        death_by_zone="dick",
    )
    last_hero = make_participant(
        "@last_hero",
        alive=False,
        hits=2,
        blocks=1,
        rounds_survived=3,
        death_round=3,
        death_attack_zone="head",
        death_defended_zone="body",
        death_by_zone="head",
    )
    battle = {
        "boss": {"name": "Непобедимый Босс"},
        "hits": 2,
        "round": 3,
        "participants": {1: first_dead, 2: last_hero},
    }

    report = boss_presentation._boss_final_report(battle, victory=False)

    assert report.startswith("💀 <b>ПОСМЕРТНАЯ ЛЕТОПИСЬ ОТРЯДА</b>")
    assert "👹 <b>Непобедимый Босс</b> остался стоять." in report
    assert (
        f"🎯 Гномы нанесли <b>2</b> из "
        f"<b>{boss_presentation.BOSS_REQUIRED_HITS}</b>"
    ) in report
    assert "👥 Участников: <b>2</b>. Выжили: <b>0</b>." in report
    assert "🩸 <b>ПОСЛЕДНИЙ НАСТОЯЩИЙ ГНОМ: last_hero</b>" in report
    assert "🛡 <b>ВЫЖИВШИЕ:</b>" not in report
    assert "💯 <b>Награда:</b>" not in report
    assert report.index("💀 <b>КАК ВСЕ УМЕРЛИ:</b>") < report.index("<b>first_dead</b> пал")
    assert report.index("<b>first_dead</b> пал") < report.index("<b>last_hero</b> пал")
    assert choose.call_count == 2


def test_duel_reexports_boss_presentation_functions_by_identity():
    from handlers import boss_presentation, duel

    assert duel._boss_death_epitaph is boss_presentation._boss_death_epitaph
    assert duel._boss_survivor_epitaph is boss_presentation._boss_survivor_epitaph
    assert duel._boss_final_report is boss_presentation._boss_final_report


@pytest.mark.asyncio
async def test_boss_send_final_report_edits_one_chunk_at_3900_boundary(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    text = "а" * 3900
    build_report = Mock(return_value=text)
    monkeypatch.setattr(duel, "_boss_final_report", build_report)
    battle = {"message_id": 501}

    await duel._boss_send_final_report(fake_context, -501, battle, victory=True)

    build_report.assert_called_once_with(battle, True)
    fake_context.bot.edit_message_text.assert_awaited_once_with(
        chat_id=-501,
        message_id=501,
        text=text,
        parse_mode="HTML",
    )
    fake_context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_boss_send_final_report_splits_3901_chars_and_falls_back_from_edit(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    text = "б" * 3901
    monkeypatch.setattr(duel, "_boss_final_report", Mock(return_value=text))
    fake_context.bot.edit_message_text = AsyncMock(
        side_effect=RuntimeError("edit failed")
    )
    battle = {"message_id": 502}

    await duel._boss_send_final_report(fake_context, -502, battle, victory=False)

    fake_context.bot.edit_message_text.assert_awaited_once_with(
        chat_id=-502,
        message_id=502,
        text="б" * 3900,
        parse_mode="HTML",
    )
    assert fake_context.bot.send_message.await_args_list == [
        call(chat_id=-502, text="б" * 3900, parse_mode="HTML"),
        call(chat_id=-502, text="б", parse_mode="HTML"),
    ]


@pytest.mark.asyncio
async def test_boss_send_final_report_uses_current_report_build_fallback(
    monkeypatch,
    fake_context,
):
    from handlers import duel

    monkeypatch.setattr(
        duel,
        "_boss_final_report",
        Mock(side_effect=ValueError("report failed")),
    )
    battle = {"message_id": 503}

    await duel._boss_send_final_report(fake_context, -503, battle, victory=True)

    fake_context.bot.edit_message_text.assert_awaited_once_with(
        chat_id=-503,
        message_id=503,
        text=(
            "💀 <b>БИТВА ОКОНЧЕНА</b>\\n\\n"
            "Босс больше не сражается. "
            "Гномская летопись почему-то отказалась писать подробности."
        ),
        parse_mode="HTML",
    )
    fake_context.bot.send_message.assert_not_awaited()
