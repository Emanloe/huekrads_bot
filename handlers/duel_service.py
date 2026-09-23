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
from duel_session_repository import (
    activate_duel_session_in_transaction,
    create_duel_session,
    get_duel_session_in_transaction,
    has_current_duel_session,
    save_duel_attack_in_transaction,
    save_duel_block_resolution_in_transaction,
    utc_unix_milliseconds,
)
from handlers.duel_state import (
    DUEL_MOVE_TIMEOUT_SECONDS,
    _get_duel_participant_ineligibility,
    resolve_duel_round,
)
from handlers.duel_text import TARGET_NAMES


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


@dataclass(frozen=True)
class PersistentDuelActionResult:
    """One server-side transition or a domain-level rejection."""

    reason: str
    session: dict | None = None
    resolution: dict | None = None
    timeout_zone: str | None = None

    @property
    def accepted(self) -> bool:
        return self.reason in ("success", "terminal_pending") and self.session is not None


def _terminal_resolution_pending(session: dict) -> bool:
    result = session["result"]
    return isinstance(result, dict) and result.get("kind") == "terminal_resolution"


def _validated_active_turn(
    session: dict | None, expected_turn_id: int,
) -> PersistentDuelActionResult | None:
    if session is None:
        return PersistentDuelActionResult("not_found")
    if _terminal_resolution_pending(session):
        return PersistentDuelActionResult("terminal_pending")
    if session["turn_id"] != expected_turn_id:
        return PersistentDuelActionResult("stale_turn")
    if session["status"] == "publishing":
        return PersistentDuelActionResult("publishing")
    if session["status"] != "active":
        return PersistentDuelActionResult("not_active")
    return None


def activate_persistent_duel_turn(
    chat_id: int,
    duel_id: int,
    turn_id: int,
    message_id: int,
    publication_time_ms: int,
) -> PersistentDuelActionResult:
    """Arm ten seconds only after a trusted publisher has shown this prompt."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        session = get_duel_session_in_transaction(chat_id, duel_id, cursor=cursor)
        if session is None:
            return PersistentDuelActionResult("not_found")
        if _terminal_resolution_pending(session):
            return PersistentDuelActionResult("terminal_pending")
        if session["turn_id"] != turn_id:
            return PersistentDuelActionResult("stale_turn")
        if session["status"] != "publishing":
            return PersistentDuelActionResult("not_publishing")
        if type(message_id) is not int or message_id <= 0:
            return PersistentDuelActionResult("invalid_message_id")

        deadline_at = publication_time_ms + DUEL_MOVE_TIMEOUT_SECONDS * 1000
        if not activate_duel_session_in_transaction(
            chat_id, duel_id, turn_id, message_id, deadline_at,
            publication_time_ms, cursor=cursor,
        ):
            raise RuntimeError("Validated duel prompt could not be activated")
        current = get_duel_session_in_transaction(chat_id, duel_id, cursor=cursor)
        return PersistentDuelActionResult("success", current)


def _apply_persistent_attack(
    cursor, chat_id: int, duel_id: int, session: dict, zone: str, now_ms: int,
) -> PersistentDuelActionResult:
    if not save_duel_attack_in_transaction(
        chat_id, duel_id, session["turn_id"], session["attacker_user_id"],
        zone, now_ms, cursor=cursor,
    ):
        raise RuntimeError("Validated duel attack could not be saved")
    current = get_duel_session_in_transaction(chat_id, duel_id, cursor=cursor)
    return PersistentDuelActionResult("success", current)


def _apply_persistent_block(
    cursor, chat_id: int, duel_id: int, session: dict, zone: str, now_ms: int,
) -> PersistentDuelActionResult:
    round_result = resolve_duel_round(session["attack_zone"], zone, random)
    terminal = round_result.outcome in ("suicide", "hit")
    # result_json is a server-generated, recoverable checkpoint until Stage 2B-4
    # applies a terminal result. It also preserves nonterminal flavor across send failures.
    resolution = {
        "kind": "terminal_resolution" if terminal else "round_resolution",
        "outcome": round_result.outcome,
        "outcome_phrase": round_result.outcome_phrase,
        "attack_phrase": round_result.attack_phrase,
        "strike_zone": session["attack_zone"],
        "block_zone": zone,
        "attacker_user_id": session["attacker_user_id"],
        "defender_user_id": session["defender_user_id"],
        "round_no": session["round_no"],
        "resolved_turn_id": session["turn_id"],
    }
    if terminal:
        winner = (
            session["defender_user_id"] if round_result.outcome == "suicide"
            else session["attacker_user_id"]
        )
        loser = (
            session["attacker_user_id"] if round_result.outcome == "suicide"
            else session["defender_user_id"]
        )
        resolution["winner_user_id"] = winner
        resolution["loser_user_id"] = loser

    if not save_duel_block_resolution_in_transaction(
        chat_id, duel_id, session["turn_id"], session["defender_user_id"],
        resolution, now_ms, terminal=terminal, cursor=cursor,
    ):
        raise RuntimeError("Validated duel block could not be saved")
    current = get_duel_session_in_transaction(chat_id, duel_id, cursor=cursor)
    return PersistentDuelActionResult(
        "terminal_pending" if terminal else "success", current, resolution,
    )


def submit_persistent_duel_attack(
    chat_id: int,
    duel_id: int,
    actor_user_id: int,
    turn_id: int,
    zone: str,
    *,
    now_ms: int | None = None,
) -> PersistentDuelActionResult:
    """Accept only the current attacker's action for this chat and turn."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        session = get_duel_session_in_transaction(chat_id, duel_id, cursor=cursor)
        rejection = _validated_active_turn(session, turn_id)
        if rejection is not None:
            return rejection
        if session["phase"] != "attack":
            return PersistentDuelActionResult("wrong_phase")
        if actor_user_id != session["attacker_user_id"]:
            return PersistentDuelActionResult("wrong_actor")
        if not isinstance(zone, str) or zone not in TARGET_NAMES:
            return PersistentDuelActionResult("invalid_zone")
        timestamp = utc_unix_milliseconds() if now_ms is None else now_ms
        return _apply_persistent_attack(cursor, chat_id, duel_id, session, zone, timestamp)


def submit_persistent_duel_block(
    chat_id: int,
    duel_id: int,
    actor_user_id: int,
    turn_id: int,
    zone: str,
    *,
    now_ms: int | None = None,
) -> PersistentDuelActionResult:
    """Resolve a block once, without applying any terminal DB effects."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        session = get_duel_session_in_transaction(chat_id, duel_id, cursor=cursor)
        rejection = _validated_active_turn(session, turn_id)
        if rejection is not None:
            return rejection
        if session["phase"] != "block":
            return PersistentDuelActionResult("wrong_phase")
        if actor_user_id != session["defender_user_id"]:
            return PersistentDuelActionResult("wrong_actor")
        if not isinstance(zone, str) or zone not in TARGET_NAMES:
            return PersistentDuelActionResult("invalid_zone")
        timestamp = utc_unix_milliseconds() if now_ms is None else now_ms
        return _apply_persistent_block(cursor, chat_id, duel_id, session, zone, timestamp)


def resolve_persistent_duel_timeout(
    chat_id: int,
    duel_id: int,
    turn_id: int,
    now_ms: int,
) -> PersistentDuelActionResult:
    """Let the DB-validated current phase choose exactly one timeout zone."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        session = get_duel_session_in_transaction(chat_id, duel_id, cursor=cursor)
        rejection = _validated_active_turn(session, turn_id)
        if rejection is not None:
            return rejection
        if now_ms < session["deadline_at"]:
            return PersistentDuelActionResult("not_due")

        zone = random.choice(["head", "body", "dick"])
        if session["phase"] == "attack":
            result = _apply_persistent_attack(cursor, chat_id, duel_id, session, zone, now_ms)
        else:
            result = _apply_persistent_block(cursor, chat_id, duel_id, session, zone, now_ms)
        return PersistentDuelActionResult(
            result.reason, result.session, result.resolution, timeout_zone=zone,
        )


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
