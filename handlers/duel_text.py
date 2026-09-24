import random
from html import escape

from text_resources import get_text, get_text_list, get_text_mapping

"""Pure presentation helpers shared by duel and boss battle output."""


HUYANIE_TITLES = get_text_mapping("duel.titles.huyanie")
WIN_TITLES = get_text_mapping("duel.titles.wins")
LOSS_TITLES = get_text_mapping("duel.titles.losses")
STOLEN_DICKS_TITLES = get_text_mapping("duel.titles.stolen_dicks")
TARGET_NAMES = get_text_mapping("duel.targets")
ATTACK_PHRASES = get_text_list("duel.phrases.attack")
HIT_PHRASES = get_text_list("duel.phrases.hit")
BLOCK_PHRASES = get_text_list("duel.phrases.block")
MISS_PHRASES = get_text_list("duel.phrases.miss")
SUICIDE_PHRASES = get_text_list("duel.phrases.suicide")
BERSERK_TRIGGERS = get_text_list("duel.berserk.triggers")
BERSERK_RESULTS = get_text_list("duel.berserk.results")
BERSERK_ALREADY_STOLEN = get_text_list("duel.berserk.already_stolen")
_BOSS_PHASE_STATUSES = get_text_mapping("duel.boss.phase_status")


def get_huyanie_title(stolen_dicks_count: int) -> str:
    count = int(stolen_dicks_count or 0)
    if count < 10:
        return get_text("duel.titles.none")
    level = min((count // 10) * 10, 100)
    return HUYANIE_TITLES[level]


def _get_highest_title(value: int, titles: dict[int, str]) -> str | None:
    value = int(value or 0)
    reached = [threshold for threshold in titles if value >= threshold]

    if not reached:
        return None

    return titles[max(reached)]


def get_win_title(wins: int) -> str | None:
    return _get_highest_title(wins, WIN_TITLES)


def get_loss_title(losses: int) -> str | None:
    return _get_highest_title(losses, LOSS_TITLES)


def get_stolen_dicks_title(stolen_dicks_count: int) -> str | None:
    return _get_highest_title(stolen_dicks_count, STOLEN_DICKS_TITLES)


def get_duel_title_read_model(user: dict) -> dict:
    """The three independent /duel_stats titles with their source counts."""
    return {
        "wins": {"text": get_win_title(user["wins"]), "count": user["wins"]},
        "losses": {"text": get_loss_title(user["losses"]), "count": user["losses"]},
        "stolen_dicks": {
            "text": get_stolen_dicks_title(user["stolen_dicks_count"]),
            "count": user["stolen_dicks_count"],
        },
    }


def _plural_rounds(value):
    value = int(value)
    if value % 10 == 1 and value % 100 != 11:
        return get_text("duel.plural_rounds.one")
    if 2 <= value % 10 <= 4 and not 12 <= value % 100 <= 14:
        return get_text("duel.plural_rounds.few")
    return get_text("duel.plural_rounds.many")


def _legacy_plural_rounds_early(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return get_text("duel.plural_rounds.one")
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return get_text("duel.plural_rounds.few")
    return get_text("duel.plural_rounds.many")


def _legacy_plural_rounds(value):
    value = int(value)
    if value % 10 == 1 and value % 100 != 11:
        return get_text("duel.plural_rounds.one")
    if 2 <= value % 10 <= 4 and not 12 <= value % 100 <= 14:
        return get_text("duel.plural_rounds.few")
    return get_text("duel.plural_rounds.many")


def _boss_alive_players(battle):
    return [participant for participant in battle["participants"].values() if participant["alive"]]


def _boss_all_alive_chosen(battle, field):
    alive = _boss_alive_players(battle)
    if not alive:
        return False
    return all(participant.get(field) is not None for participant in alive)


def _boss_phase_status(participant, phase):
    if not participant["alive"]:
        return _BOSS_PHASE_STATUSES["dead"]
    if phase == "attack":
        return _BOSS_PHASE_STATUSES["attack_selected"] if participant.get("attack") is not None else _BOSS_PHASE_STATUSES["attack_selecting"]
    if phase == "block":
        return _BOSS_PHASE_STATUSES["block_selected"] if participant.get("block") is not None else _BOSS_PHASE_STATUSES["block_selecting"]
    return ""


def _boss_battle_hero(participants):
    alive = [participant for participant in participants if participant["alive"]]
    if not participants:
        return None
    return max(
        participants,
        key=lambda participant: (
            participant.get("hits", 0),
            participant.get("blocks", 0),
            participant.get("rounds_survived", 0),
            1 if participant in alive else 0,
        ),
    )


def _build_duel_miss_text(
    attacker_title: str,
    attack_phrase: str,
    strike_zone: str,
    miss_phrase: str,
    next_attacker_title: str,
    next_defender_title: str,
    move_timeout: int,
    *,
    round_presentation: str | None = None,
) -> str:
    presentation = round_presentation or get_duel_round_presentation(
        {"outcome": "miss", "attack_phrase": attack_phrase,
         "outcome_phrase": miss_phrase, "strike_zone": strike_zone},
        attacker_title, "",
    )
    return get_text(
        "duel.templates.miss",
        round_presentation=presentation,
        next_attacker_title=next_attacker_title,
        next_defender_title=next_defender_title,
        move_timeout=move_timeout,
    )


def _build_duel_block_text(
    attacker_title: str,
    defender_title: str,
    attack_phrase: str,
    strike_zone: str,
    block_phrase: str,
    next_attacker_title: str,
    next_defender_title: str,
    move_timeout: int,
    *,
    round_presentation: str | None = None,
) -> str:
    presentation = round_presentation or get_duel_round_presentation(
        {"outcome": "block", "attack_phrase": attack_phrase,
         "outcome_phrase": block_phrase, "strike_zone": strike_zone},
        attacker_title, defender_title,
    )
    return get_text(
        "duel.templates.block",
        round_presentation=presentation,
        next_attacker_title=next_attacker_title,
        next_defender_title=next_defender_title,
        move_timeout=move_timeout,
    )


def get_duel_round_presentation(
    resolution: dict, attacker_title: str, defender_title: str,
) -> str:
    """Compose Telegram HTML from already selected round phrases, without RNG."""
    outcome = resolution["outcome"]
    if outcome == "suicide":
        return get_text(
            "duel.live.outcomes.suicide", attacker_title=attacker_title,
            suicide_phrase=resolution["outcome_phrase"],
            defender_title=defender_title,
        )
    if outcome == "hit":
        return get_text(
            "duel.live.outcomes.hit", attacker_title=attacker_title,
            attack_phrase=resolution["attack_phrase"],
            strike_target=TARGET_NAMES[resolution["strike_zone"]],
            defender_title=defender_title,
            block_target=TARGET_NAMES[resolution["block_zone"]],
            hit_phrase=resolution["outcome_phrase"],
        )
    if outcome not in ("miss", "block"):
        raise ValueError("Unknown resolved duel outcome")
    key = "duel.round_presentation.miss" if outcome == "miss" else "duel.round_presentation.block"
    return get_text(
        key, attacker_title=attacker_title,
        defender_title=defender_title,
        attack_phrase=resolution["attack_phrase"],
        target_name=TARGET_NAMES[resolution["strike_zone"]],
        miss_phrase=resolution["outcome_phrase"],
        block_phrase=resolution["outcome_phrase"],
    )


def get_round_flavor_text(rounds_count: int, rng=None) -> str:
    if rounds_count <= 1:
        phrases = get_text_list("duel.round_flavor.one")
    elif rounds_count <= 4:
        phrases = get_text_list("duel.round_flavor.few")
    elif rounds_count <= 8:
        phrases = get_text_list("duel.round_flavor.several")
    else:
        phrases = get_text_list("duel.round_flavor.many")
    return (rng or random).choice(phrases)


def _build_berserk_text(
    berserker_title: str,
    victim_title: str,
    already_stolen: bool,
    rng=None,
) -> str:
    berserker_title = escape(berserker_title)
    victim_title = escape(victim_title)
    picker = rng or random
    trigger = picker.choice(BERSERK_TRIGGERS)
    result_catalog = BERSERK_ALREADY_STOLEN if already_stolen else BERSERK_RESULTS
    result = picker.choice(result_catalog)
    return get_text(
        "duel.berserk.block",
        header=get_text("duel.berserk.header"),
        trigger=trigger.format(berserker=berserker_title),
        result=result.format(
            berserker=berserker_title,
            victim=victim_title,
        ),
    )
