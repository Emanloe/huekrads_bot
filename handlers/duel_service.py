"""Chat-scoped, Telegram-independent operations for ordinary duels."""

import random
from dataclasses import dataclass

from database import (
    format_user_title_plain,
    get_db,
    get_duel_inventory,
    get_duel_top,
    get_duel_user_by_id,
    get_duel_user_by_id_in_transaction,
    get_duel_user_by_username,
)
from duel_session_repository import create_duel_session, has_current_duel_session
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


DUEL_PARTICIPANT_SNAPSHOT_FIELDS = (
    "user_id", "username", "display_name", "dwarf_name", "points", "daily_wins",
)


@dataclass(frozen=True)
class DuelParticipantSnapshot:
    """Only the duel-user values that the ordinary duel reads after start."""

    user_id: int
    username: str | None
    display_name: str
    dwarf_name: str | None
    points: int
    daily_wins: int

    @classmethod
    def from_duel_user(cls, user: dict) -> "DuelParticipantSnapshot":
        return cls.from_storage({key: user[key] for key in DUEL_PARTICIPANT_SNAPSHOT_FIELDS})

    @classmethod
    def from_storage(cls, values: dict) -> "DuelParticipantSnapshot":
        """Validate and deserialize one JSON-decoded participant snapshot."""
        if not isinstance(values, dict) or set(values) != set(DUEL_PARTICIPANT_SNAPSHOT_FIELDS):
            raise ValueError("Invalid duel participant snapshot fields")
        if any(type(values[key]) is not int for key in ("user_id", "points", "daily_wins")):
            raise ValueError("Invalid duel participant snapshot integer")
        if not isinstance(values["display_name"], str) or any(
            values[key] is not None and not isinstance(values[key], str)
            for key in ("username", "dwarf_name")
        ):
            raise ValueError("Invalid duel participant snapshot name")
        return cls(**values)

    def to_storage(self) -> dict:
        """Return a fresh JSON-ready dict with the canonical field set."""
        return {key: getattr(self, key) for key in DUEL_PARTICIPANT_SNAPSHOT_FIELDS}


@dataclass(frozen=True)
class PersistentDuelStartResult:
    """Domain outcome; a successful session is not yet published or active."""

    reason: str | None
    session: dict | None

    @property
    def success(self) -> bool:
        return self.session is not None


def start_persistent_duel(
    chat_id: int,
    initiator_user_id: int,
    opponent_user_id: int,
    *,
    now_ms: int | None = None,
) -> PersistentDuelStartResult:
    """Reserve a chat's duel slot and snapshot two eligible registered users.

    This operation is not wired to Telegram. SQLite serializes admission and
    insertion, so a competing start sees the occupied slot before doing RNG.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")

        if has_current_duel_session(chat_id, cursor=cursor):
            return PersistentDuelStartResult("active_duel", None)

        # The Telegram path checks same-username self-targeting before admission.
        if initiator_user_id == opponent_user_id:
            return PersistentDuelStartResult("self_target", None)

        initiator = get_duel_user_by_id_in_transaction(cursor, chat_id, initiator_user_id)
        if initiator is None:
            return PersistentDuelStartResult("initiator_not_registered", None)
        ineligibility = _get_duel_participant_ineligibility(initiator)
        if ineligibility is not None:
            return PersistentDuelStartResult(f"initiator_{ineligibility}", None)

        opponent = get_duel_user_by_id_in_transaction(cursor, chat_id, opponent_user_id)
        if opponent is None:
            return PersistentDuelStartResult("opponent_not_registered", None)
        ineligibility = _get_duel_participant_ineligibility(opponent)
        if ineligibility is not None:
            return PersistentDuelStartResult(f"opponent_{ineligibility}", None)

        player1 = DuelParticipantSnapshot.from_duel_user(initiator)
        player2 = DuelParticipantSnapshot.from_duel_user(opponent)
        if random.choice([True, False]):
            attacker_user_id, defender_user_id = initiator_user_id, opponent_user_id
        else:
            attacker_user_id, defender_user_id = opponent_user_id, initiator_user_id

        session = create_duel_session(
            chat_id,
            initiator_user_id,
            opponent_user_id,
            player1.to_storage(),
            player2.to_storage(),
            attacker_user_id,
            defender_user_id,
            status="publishing",
            phase="attack",
            now_ms=now_ms,
            cursor=cursor,
        )
        return PersistentDuelStartResult(None, session)


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
