"""Chat-scoped, Telegram-independent read operations for ordinary duels."""

from dataclasses import dataclass

from database import (
    format_user_title_plain,
    get_duel_inventory,
    get_duel_top,
    get_duel_user_by_id,
    get_duel_user_by_username,
)
from handlers.duel_state import _get_duel_participant_ineligibility


@dataclass(frozen=True)
class DuelProfile:
    user: dict
    inventory: list[dict]
    ineligibility: str | None


@dataclass(frozen=True)
class DuelOpponent:
    user_id: int
    username: str
    title: str


@dataclass(frozen=True)
class DuelOpponentList:
    ineligibility: str | None
    opponents: list[DuelOpponent]


def get_duel_profile(chat_id: int, user_id: int) -> DuelProfile | None:
    """Return an existing participant's state and inventory in this chat only."""
    user = get_duel_user_by_id(chat_id, user_id)
    if user is None:
        return None
    return DuelProfile(
        user=user,
        inventory=get_duel_inventory(chat_id, user_id),
        ineligibility=_get_duel_participant_ineligibility(user),
    )


def list_duel_opponents(chat_id: int, user_id: int, limit: int = 20) -> DuelOpponentList:
    """Build the same eligible top-list used by /duel, without starting a fight."""
    initiator = get_duel_user_by_id(chat_id, user_id)
    if initiator is None:
        return DuelOpponentList("not_registered", [])

    ineligibility = _get_duel_participant_ineligibility(initiator)
    if ineligibility is not None:
        return DuelOpponentList(ineligibility, [])

    opponents = []
    for username, display_name, *_ in get_duel_top(
        chat_id=chat_id, limit=limit, include_dwarf_name=True,
    ):
        if not username:
            continue
        if initiator["username"] and initiator["username"].lower() == username.lower():
            continue

        opponent = get_duel_user_by_username(username, chat_id)
        if opponent is None or _get_duel_participant_ineligibility(opponent) is not None:
            continue

        title = format_user_title_plain({
            "display_name": (display_name or username).lstrip("@"),
            "dwarf_name": opponent.get("dwarf_name"),
        })
        opponents.append(DuelOpponent(opponent["user_id"], username, title))

    return DuelOpponentList(None, opponents)
