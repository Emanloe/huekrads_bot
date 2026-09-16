"""Formatting adapters used by duel and boss output."""

from database import format_user_title
from handlers.duel_text import _boss_alive_players, _boss_phase_status
from text_resources import get_text


def boss_player_title(participant: dict) -> str:
    """Format the stored user data of a boss participant for display."""
    return format_user_title(participant["data"])


def _boss_players_status_text(battle):
    lines = []
    for participant in battle["participants"].values():
        title = boss_player_title(participant)
        status = _boss_phase_status(participant, battle["phase"])
        lines.append(
            get_text("boss.phase.player_status", title=title, status=status)
        )
    return "\n".join(lines)


def _boss_phase_text(battle, required_hits: int):
    boss = battle["boss"]
    alive_count = len(_boss_alive_players(battle))
    total_count = len(battle["participants"])

    if battle["phase"] == "attack":
        return get_text(
            "boss.phase.attack",
            boss_name=boss["name"],
            round=battle["round"],
            hits=battle["hits"],
            required_hits=required_hits,
            alive_count=alive_count,
            total_count=total_count,
            players_status=_boss_players_status_text(battle),
        )

    return get_text(
        "boss.phase.block",
        boss_name=boss["name"],
        round=battle["round"],
        hits=battle["hits"],
        required_hits=required_hits,
        alive_count=alive_count,
        total_count=total_count,
        players_status=_boss_players_status_text(battle),
    )
