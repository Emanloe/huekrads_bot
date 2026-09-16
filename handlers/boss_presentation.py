"""Pure text composition for boss battle results."""

import random

from handlers.duel_formatting import boss_player_title as _boss_player_title
from handlers.duel_text import _boss_battle_hero, _plural_rounds
from text_resources import get_text, get_text_list, get_text_mapping


BOSS_REQUIRED_HITS = 5

BOSS_ZONE_NAMES = get_text_mapping("boss.zones.display")
_BOSS_UNKNOWN_ZONE = get_text("boss.zones.unknown")


def _boss_death_epitaph(participant, boss_name):
    title = _boss_player_title(participant)
    attack_zone = BOSS_ZONE_NAMES.get(
        participant.get("death_attack_zone"),
        _BOSS_UNKNOWN_ZONE,
    )
    death_zone = BOSS_ZONE_NAMES.get(
        participant.get("death_by_zone"),
        _BOSS_UNKNOWN_ZONE,
    )
    defended_zone = BOSS_ZONE_NAMES.get(
        participant.get("death_defended_zone"),
        _BOSS_UNKNOWN_ZONE,
    )
    round_num = participant.get("death_round") or get_text(
        "boss.death.round_fallback"
    )

    phrase = random.choice(get_text_list("boss.death_epitaphs"))
    return phrase.format(
        title=title,
        round_num=round_num,
        attack_zone=attack_zone,
        defended_zone=defended_zone,
        boss_name=boss_name,
        death_zone=death_zone,
    )


def _boss_survivor_epitaph(participant):
    title = _boss_player_title(participant)
    hits = participant.get("hits", 0)
    misses = participant.get("misses", 0)
    blocks = participant.get("blocks", 0)
    survived = participant.get("rounds_survived", 0)

    if hits >= 2 and blocks >= 2:
        phrase = get_text("boss.survivor.phrases.terminator")
    elif hits >= 2:
        phrase = get_text("boss.survivor.phrases.target")
    elif blocks >= 2:
        phrase = get_text("boss.survivor.phrases.evasive")
    elif hits:
        phrase = get_text("boss.survivor.phrases.marked")
    else:
        phrase = get_text("boss.survivor.phrases.brazen")

    return get_text(
        "boss.survivor.summary",
        title=title,
        phrase=phrase,
        hits=hits,
        misses=misses,
        blocks=blocks,
        survived=survived,
    )

def _boss_final_report(battle, victory: bool):
    participants = list(battle["participants"].values())
    boss_name = battle["boss"]["name"]
    total = len(participants)
    survivors = [
        p for p in participants
        if p["alive"]
    ]
    dead = [
        p for p in participants
        if not p["alive"]
    ]
    hero = _boss_battle_hero(participants)

    lines = []

    if victory:
        lines.extend([
            get_text("boss.report.victory.title"),
            "",
            get_text("boss.report.victory.boss_defeated", boss_name=boss_name, hits=battle["hits"]),
            get_text("boss.report.victory.duration", round=battle["round"], round_word=_plural_rounds(battle["round"])),
            get_text("boss.report.victory.team", total=total, survivors=len(survivors), dead=len(dead)),
            "",
            get_text("boss.report.victory.survivors_header"),
        ])

        lines.extend(
            _boss_survivor_epitaph(p)
            for p in survivors
        )

        if dead:
            lines.extend([
                "",
                get_text("boss.report.victory.dead_header"),
            ])
            lines.extend(
                _boss_death_epitaph(p, boss_name)
                for p in dead
            )

        if hero:
            hero_title = _boss_player_title(hero)
            lines.extend([
                "",
                get_text("boss.report.victory.hero", hero_title=hero_title),
                get_text(
                    "boss.report.victory.hero_stats",
                    hits=hero.get("hits", 0),
                    blocks=hero.get("blocks", 0),
                    rounds_survived=hero.get("rounds_survived", 0),
                    round_word=_plural_rounds(hero.get("rounds_survived", 0)),
                ),
            ])

        lines.extend([
            "",
            get_text("boss.report.victory.reward_points"),
            get_text("boss.report.victory.reward_dick"),
            "",
            get_text("boss.report.victory.decree"),
        ])

    else:
        lines.extend([
            get_text("boss.report.defeat.title"),
            "",
            get_text("boss.report.defeat.boss_standing", boss_name=boss_name),
            get_text("boss.report.defeat.hits", hits=battle["hits"], required_hits=BOSS_REQUIRED_HITS),
            get_text("boss.report.defeat.duration", round=battle["round"], round_word=_plural_rounds(battle["round"])),
            get_text("boss.report.defeat.team", total=total),
            "",
            get_text("boss.report.defeat.dead_header"),
        ])

        lines.extend(
            _boss_death_epitaph(p, boss_name)
            for p in dead
        )

        if hero:
            hero_title = _boss_player_title(hero)
            lines.extend([
                "",
                get_text("boss.report.defeat.hero", hero_title=hero_title),
                get_text(
                    "boss.report.defeat.hero_stats",
                    hits=hero.get("hits", 0),
                    blocks=hero.get("blocks", 0),
                ),
            ])

        lines.extend([
            "",
            get_text("boss.report.defeat.verdict"),
        ])

    return "\n".join(lines)
