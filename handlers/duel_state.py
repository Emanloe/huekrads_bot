"""Shared ordinary-duel rules and state transitions."""

from dataclasses import dataclass

from config import BERSERK_CHANCE, DUEL_ITEM_STEAL_CHANCE, DUEL_POST_MESSAGE_CHANCE
from handlers.duel_text import (
    ATTACK_PHRASES,
    BLOCK_PHRASES,
    HIT_PHRASES,
    MISS_PHRASES,
    SUICIDE_PHRASES,
)

DUEL_MOVE_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class DuelRoundResolution:
    outcome: str
    outcome_phrase: str
    attack_phrase: str | None = None


def _is_suicide_roll(suicide_roll: float) -> bool:
    return suicide_roll < 0.01


def _is_miss_roll(miss_roll: float) -> bool:
    return miss_roll < 0.05


def _is_berserk_roll(berserk_roll: float) -> bool:
    return berserk_roll < BERSERK_CHANCE


def _is_duel_post_message_roll(post_message_roll: float) -> bool:
    return post_message_roll < DUEL_POST_MESSAGE_CHANCE


def _is_duel_item_steal_roll(item_steal_roll: float) -> bool:
    return item_steal_roll < DUEL_ITEM_STEAL_CHANCE


def choose_duel_item_to_steal(collectible_inventory: list[dict], rng) -> dict | None:
    """Use the ordinary duel item-steal roll and one concrete instance choice."""
    if not collectible_inventory or not _is_duel_item_steal_roll(rng.random()):
        return None
    return rng.choice(collectible_inventory)


def _resolve_zone_outcome(strike_zone: str, block_zone: str) -> str:
    return "block" if strike_zone == block_zone else "hit"


def resolve_duel_round(strike_zone: str, block_zone: str, rng) -> DuelRoundResolution:
    """Resolve one block using the existing RNG calls in their original order.

    ``rng`` is the caller's random module/object so the Telegram path retains
    its existing RNG hook and the future service uses the same rules.
    """
    if _is_suicide_roll(rng.random()):
        return DuelRoundResolution("suicide", rng.choice(SUICIDE_PHRASES))
    if _is_miss_roll(rng.random()):
        return DuelRoundResolution(
            "miss", rng.choice(MISS_PHRASES), rng.choice(ATTACK_PHRASES),
        )
    if _resolve_zone_outcome(strike_zone, block_zone) == "block":
        return DuelRoundResolution(
            "block", rng.choice(BLOCK_PHRASES), rng.choice(ATTACK_PHRASES),
        )
    return DuelRoundResolution(
        "hit", rng.choice(HIT_PHRASES), rng.choice(ATTACK_PHRASES),
    )


def _get_duel_participant_ineligibility(user: dict) -> str | None:
    if user["dick_stolen_today"]:
        return "no_dick"
    return None


def _build_duel_result_plan(
    winner: dict,
    loser: dict,
    is_dick_stolen: bool,
    winner_title: str,
    max_daily_points: int,
) -> dict:
    winner_points = max(0, min(max_daily_points, winner["points"] + 10))
    loser_points = max(0, loser["points"] - 5)
    result_plan = {
        "is_dick_stolen": is_dick_stolen,
        "winner_reached_max": (
            winner["points"] < max_daily_points
            and winner_points >= max_daily_points
        ),
        "winner": {
            "user_id": winner["user_id"],
            "points": winner_points,
            "wins_increment": 1,
            "daily_wins_increment": 1,
            "stolen_dicks_count_increment": 1 if is_dick_stolen else 0,
        },
        "loser": {
            "user_id": loser["user_id"],
            "points": loser_points,
            "losses_increment": 1,
        },
    }

    if is_dick_stolen:
        result_plan["loser"].update({
            "dick_stolen_count_increment": 1,
            "dick_stolen_today": 1,
            "last_stolen_by": winner_title,
        })

    return result_plan


def _set_attack_choice(duel_state: dict, strike_zone: str) -> None:
    duel_state["attack_zone"] = strike_zone
    duel_state["phase"] = "block"
    duel_state["turn_id"] += 1


def _advance_duel_round(duel_state: dict) -> None:
    duel_state["attacker_tg"], duel_state["defender_tg"] = (
        duel_state["defender_tg"],
        duel_state["attacker_tg"],
    )
    duel_state["attacker_data"], duel_state["defender_data"] = (
        duel_state["defender_data"],
        duel_state["attacker_data"],
    )
    duel_state["phase"] = "attack"
    duel_state["attack_zone"] = None
    duel_state["round"] += 1
    duel_state["turn_id"] += 1
