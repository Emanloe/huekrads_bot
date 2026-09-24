"""Chat-scoped, Telegram-independent operations for ordinary duels."""

import logging
import random
from dataclasses import dataclass
from html import escape

from config import DUEL_ITEM_DROP_CHANCE, MAX_DAILY_POINTS
from text_resources import get_text

from database import (
    apply_duel_berserk_in_transaction,
    apply_duel_result_plan_in_transaction,
    bind_duel_item_event_message_in_transaction,
    create_duel_item_event_from_inventory_in_transaction,
    format_user_title,
    format_user_title_plain,
    get_db,
    get_duel_inventory,
    get_duel_inventory_in_transaction,
    get_dick_steal_chance,
    get_duel_top,
    get_duel_user_by_id,
    get_duel_user_by_id_in_transaction,
    get_duel_user_by_username,
    restore_unpublished_duel_drop_in_transaction,
    transfer_duel_inventory_item_in_transaction,
)
from duel_outbox_repository import (
    cancel_duel_publication_in_transaction,
    create_duel_publication_in_transaction,
    get_duel_publication_by_kind_in_transaction,
    get_duel_publication_in_transaction,
    mark_duel_publication_delivered_in_transaction,
)
from duel_session_repository import (
    activate_duel_session_in_transaction,
    create_duel_session,
    finish_duel_session_in_transaction,
    get_duel_session_in_transaction,
    has_current_duel_session,
    mark_duel_pocket_done_in_transaction,
    save_duel_attack_in_transaction,
    save_duel_block_resolution_in_transaction,
    utc_unix_milliseconds,
)
from handlers.duel_catalog import DUEL_POST_MESSAGES, DWARFS_FACTS
from handlers.duel_items import (
    format_pocket_drop_announcement, get_droppable_duel_inventory, get_duel_item_name,
)
from handlers.duel_state import (
    DUEL_MOVE_TIMEOUT_SECONDS,
    _build_duel_result_plan,
    _get_duel_participant_ineligibility,
    _is_berserk_roll,
    _is_duel_post_message_roll,
    choose_duel_item_to_steal,
    resolve_duel_round,
)
from handlers.duel_text import (
    TARGET_NAMES, _build_berserk_text, _build_duel_block_text,
    _build_duel_miss_text, _plural_rounds, get_round_flavor_text,
)


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


@dataclass(frozen=True)
class PersistentDuelFinalizationResult:
    """A terminal DB commit, its stored replay, or a domain-level rejection."""

    reason: str
    session: dict | None = None
    result: dict | None = None


@dataclass(frozen=True)
class PersistentDuelPublicationResult:
    reason: str
    publication: dict | None = None
    session: dict | None = None


@dataclass(frozen=True)
class PersistentDuelPocketResult:
    reason: str
    session: dict | None = None
    drop: dict | None = None
    publication: dict | None = None


def _session_participants(session: dict) -> dict[int, dict]:
    player1 = DuelParticipantSnapshot.from_storage(session["player1_snapshot"]).to_storage()
    player2 = DuelParticipantSnapshot.from_storage(session["player2_snapshot"]).to_storage()
    return {player1["user_id"]: player1, player2["user_id"]: player2}


def _prompt_payload(session: dict) -> dict:
    """Render a prompt from immutable snapshots/checkpoint without any RNG."""
    players = _session_participants(session)
    attacker_title = format_user_title(players[session["attacker_user_id"]])
    defender_title = format_user_title(players[session["defender_user_id"]])
    if session["phase"] == "block":
        text = get_text(
            "duel.live.defense_transition", round=session["round_no"],
            attacker_title=attacker_title, defender_title=defender_title,
            move_timeout=DUEL_MOVE_TIMEOUT_SECONDS,
        )
    elif session["result"] is None:
        text = get_text(
            "duel.live.start", attacker_title=attacker_title,
            defender_title=defender_title, move_timeout=DUEL_MOVE_TIMEOUT_SECONDS,
        )
    else:
        resolution = session["result"]
        previous_attacker = format_user_title(players[resolution["attacker_user_id"]])
        previous_defender = format_user_title(players[resolution["defender_user_id"]])
        if resolution["outcome"] == "miss":
            text = _build_duel_miss_text(
                previous_attacker, resolution["attack_phrase"],
                resolution["strike_zone"], resolution["outcome_phrase"],
                attacker_title, defender_title, DUEL_MOVE_TIMEOUT_SECONDS,
            )
        else:
            text = _build_duel_block_text(
                previous_attacker, previous_defender, resolution["attack_phrase"],
                resolution["strike_zone"], resolution["outcome_phrase"],
                attacker_title, defender_title, DUEL_MOVE_TIMEOUT_SECONDS,
            )
    return {"text": text, "phase": session["phase"]}


def _validated_terminal_checkpoint(session: dict) -> dict | None:
    """Reject malformed or mismatched stored resolution before any finish RNG."""
    checkpoint = session["result"]
    if not isinstance(checkpoint, dict) or checkpoint.get("kind") != "terminal_resolution":
        return None
    attacker = session["attacker_user_id"]
    defender = session["defender_user_id"]
    outcome = checkpoint.get("outcome")
    if outcome not in ("hit", "suicide"):
        return None
    expected_winner, expected_loser = (
        (attacker, defender) if outcome == "hit" else (defender, attacker)
    )
    expected = {
        "attacker_user_id": attacker,
        "defender_user_id": defender,
        "winner_user_id": expected_winner,
        "loser_user_id": expected_loser,
        "strike_zone": session["attack_zone"],
        "round_no": session["round_no"],
        "resolved_turn_id": session["turn_id"] - 1,
    }
    if any(checkpoint.get(key) != value for key, value in expected.items()):
        return None
    if checkpoint.get("block_zone") not in TARGET_NAMES:
        return None
    phrase_keys = ("outcome_phrase", "attack_phrase") if outcome == "hit" else ("outcome_phrase",)
    if not all(isinstance(checkpoint.get(key), str) for key in phrase_keys):
        return None
    return checkpoint


def _terminal_custom_text(checkpoint: dict, attacker: dict, defender: dict) -> str:
    attacker_title = format_user_title(attacker)
    defender_title = format_user_title(defender)
    if checkpoint["outcome"] == "suicide":
        return get_text(
            "duel.live.outcomes.suicide",
            attacker_title=attacker_title,
            suicide_phrase=checkpoint["outcome_phrase"],
            defender_title=defender_title,
        )
    return get_text(
        "duel.live.outcomes.hit",
        attacker_title=attacker_title,
        attack_phrase=checkpoint["attack_phrase"],
        strike_target=TARGET_NAMES[checkpoint["strike_zone"]],
        defender_title=defender_title,
        block_target=TARGET_NAMES[checkpoint["block_zone"]],
        hit_phrase=checkpoint["outcome_phrase"],
    )


def _steal_persistent_item(cursor, chat_id: int, winner_id: int, loser_id: int) -> dict | None:
    """Same collectible roll/choice as Telegram, using the finalization connection."""
    inventory = get_droppable_duel_inventory(
        get_duel_inventory_in_transaction(cursor, chat_id, loser_id)
    )
    instance = choose_duel_item_to_steal(inventory, random)
    if instance is None:
        return None
    if not transfer_duel_inventory_item_in_transaction(
        cursor, chat_id, loser_id, winner_id, instance["id"],
    ):
        return None
    return {"instance_id": instance["id"], "item_id": instance["item_id"],
            "item_name": get_duel_item_name(instance["item_id"])}


def finalize_persistent_duel(
    chat_id: int, duel_id: int, *, now_ms: int | None = None,
) -> PersistentDuelFinalizationResult:
    """Apply one checkpointed terminal duel exactly once, without publication/drop."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        session = get_duel_session_in_transaction(chat_id, duel_id, cursor=cursor)
        if session is None:
            return PersistentDuelFinalizationResult("not_found")
        if session["status"] == "finished":
            stored = session["result"]
            if isinstance(stored, dict) and stored.get("kind") == "finalized":
                return PersistentDuelFinalizationResult("already_finished", session, stored)
            return PersistentDuelFinalizationResult("invalid_state")
        if session["status"] != "publishing" or session["phase"] != "block":
            return PersistentDuelFinalizationResult("invalid_state")
        if not isinstance(session["result"], dict) or session["result"].get("kind") != "terminal_resolution":
            return PersistentDuelFinalizationResult("not_terminal")
        checkpoint = _validated_terminal_checkpoint(session)
        if checkpoint is None:
            return PersistentDuelFinalizationResult("invalid_state")

        try:
            player1 = DuelParticipantSnapshot.from_storage(session["player1_snapshot"]).to_storage()
            player2 = DuelParticipantSnapshot.from_storage(session["player2_snapshot"]).to_storage()
        except (KeyError, TypeError, ValueError):
            return PersistentDuelFinalizationResult("invalid_state")
        if (player1["user_id"], player2["user_id"]) != (
            session["player1_user_id"], session["player2_user_id"],
        ):
            return PersistentDuelFinalizationResult("invalid_state")
        participants = {player1["user_id"]: player1, player2["user_id"]: player2}
        winner = participants[checkpoint["winner_user_id"]]
        loser = participants[checkpoint["loser_user_id"]]
        attacker = participants[checkpoint["attacker_user_id"]]
        defender = participants[checkpoint["defender_user_id"]]

        # Match _finish_duel's exact order. No pocket-drop RNG belongs here.
        is_dick_stolen = (
            loser["points"] == 0
            or random.random() < get_dick_steal_chance(loser["daily_wins"])
        )
        plan = _build_duel_result_plan(
            winner, loser, is_dick_stolen,
            format_user_title_plain(winner, include_dwarf_name=False),
            MAX_DAILY_POINTS,
        )
        winner_points, loser_points = apply_duel_result_plan_in_transaction(
            cursor, chat_id, plan,
        )

        stolen_item = None
        if is_dick_stolen:
            cursor.execute("SAVEPOINT persistent_item_steal")
            try:
                stolen_item = _steal_persistent_item(
                    cursor, chat_id, winner["user_id"], loser["user_id"],
                )
            except Exception:
                cursor.execute("ROLLBACK TO SAVEPOINT persistent_item_steal")
                logging.exception("Ошибка кражи предмета после дуэли в чате %s", chat_id)
            finally:
                cursor.execute("RELEASE SAVEPOINT persistent_item_steal")

        winner_title = format_user_title(winner)
        loser_title = format_user_title(loser)
        custom_text = _terminal_custom_text(checkpoint, attacker, defender)
        final_text = get_text(
            "duel.finish.result", custom_text=custom_text,
            winner_title=winner_title, loser_title=loser_title,
            winner_points=winner_points, loser_points=loser_points,
        )
        round_flavor = get_round_flavor_text(session["round_no"], rng=random)
        stats_text = get_text(
            "duel.finish.stats", rounds_count=session["round_no"],
            rounds_label=_plural_rounds(session["round_no"]),
            round_flavor=round_flavor,
        )
        dwarf_fact = None
        if is_dick_stolen:
            if stolen_item is not None:
                final_text += get_text(
                    "duel.finish.item_stolen", item_name=escape(stolen_item["item_name"]),
                )
            dwarf_fact = random.choice(DWARFS_FACTS)
            final_text += get_text(
                "duel.finish.stolen", loser_title=loser_title,
                stats_text=stats_text, fact=dwarf_fact,
            )
        else:
            final_text += stats_text

        berserk = None
        if _is_berserk_roll(random.random()):
            berserker = random.choice((winner, loser))
            victim = loser if berserker is winner else winner
            berserker_title = winner_title if berserker is winner else loser_title
            victim_title = loser_title if victim is loser else winner_title
            cursor.execute("SAVEPOINT persistent_berserk")
            try:
                applied = apply_duel_berserk_in_transaction(
                    cursor, chat_id, berserker["user_id"], victim["user_id"],
                    format_user_title_plain(berserker, include_dwarf_name=False),
                )
            except Exception:
                cursor.execute("ROLLBACK TO SAVEPOINT persistent_berserk")
                logging.exception("Не удалось применить berserk event в чате %s", chat_id)
            else:
                berserk_text = _build_berserk_text(
                    berserker_title, victim_title, already_stolen=not applied, rng=random,
                )
                final_text += berserk_text
                berserk = {
                    "berserker_user_id": berserker["user_id"],
                    "victim_user_id": victim["user_id"],
                    "applied": applied, "text": berserk_text,
                }
            finally:
                cursor.execute("RELEASE SAVEPOINT persistent_berserk")

        post_message = None
        if DUEL_POST_MESSAGES and _is_duel_post_message_roll(random.random()):
            post_message = random.choice(DUEL_POST_MESSAGES)
            final_text += (
                f"\n\n{get_text('duel.finish.post_message.prefix')}\n"
                f"{escape(post_message)}"
            )

        result = {
            "kind": "finalized", "terminal_resolution": checkpoint,
            "winner_user_id": winner["user_id"], "loser_user_id": loser["user_id"],
            "winner_points": winner_points, "loser_points": loser_points,
            "winner_reached_max": plan["winner_reached_max"],
            "is_dick_stolen": is_dick_stolen, "stolen_item": stolen_item,
            "round_flavor": round_flavor, "dwarf_fact": dwarf_fact,
            "berserk": berserk, "post_message": post_message,
            "custom_text": custom_text, "final_text": final_text,
        }
        timestamp = utc_unix_milliseconds() if now_ms is None else now_ms
        if not finish_duel_session_in_transaction(
            chat_id, duel_id, session["turn_id"], result, timestamp, cursor=cursor,
        ):
            raise RuntimeError("Validated terminal duel could not be finalized")
        finished = get_duel_session_in_transaction(chat_id, duel_id, cursor=cursor)
        create_duel_publication_in_transaction(
            cursor, chat_id, duel_id, "final_result", finished["turn_id"],
            {"text": final_text}, timestamp,
        )
        return PersistentDuelFinalizationResult("finalized", finished, result)


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
        return _activate_persistent_duel_turn_in_transaction(
            cursor, chat_id, duel_id, turn_id, message_id, publication_time_ms,
        )


def _activate_persistent_duel_turn_in_transaction(
    cursor, chat_id: int, duel_id: int, turn_id: int, message_id: int,
    publication_time_ms: int,
) -> PersistentDuelActionResult:
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


def acknowledge_persistent_duel_publication(
    chat_id: int, publication_id: int, attempt_count: int,
    message_id: int, published_at_ms: int,
) -> PersistentDuelPublicationResult:
    """Commit publication receipt and its DB acknowledgement in one short txn."""
    if type(message_id) is not int or message_id <= 0:
        return PersistentDuelPublicationResult("invalid_message_id")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        publication = get_duel_publication_in_transaction(cursor, chat_id, publication_id)
        if publication is None:
            return PersistentDuelPublicationResult("not_found")
        if publication["status"] == "delivered":
            return PersistentDuelPublicationResult("already_delivered", publication)
        if publication["status"] != "leased" or publication["attempt_count"] != attempt_count:
            return PersistentDuelPublicationResult("lease_lost", publication)
        session = get_duel_session_in_transaction(
            chat_id, publication["duel_id"], cursor=cursor,
        )
        if session is None:
            return PersistentDuelPublicationResult("not_found")

        kind = publication["kind"]
        acknowledged_message_id = message_id
        reason = "delivered"
        if kind in ("attack_prompt", "block_prompt"):
            expected_phase = "attack" if kind == "attack_prompt" else "block"
            if session["phase"] != expected_phase or session["turn_id"] != publication["turn_id"]:
                cancel_duel_publication_in_transaction(cursor, chat_id, publication_id, published_at_ms)
                return PersistentDuelPublicationResult("stale_prompt", None, session)
            if session["status"] == "active":
                # An existing deadline is authoritative; duplicate delivery never extends it.
                acknowledged_message_id = session["message_id"]
                reason = "already_active"
            else:
                activation = _activate_persistent_duel_turn_in_transaction(
                    cursor, chat_id, session["id"], publication["turn_id"],
                    message_id, published_at_ms,
                )
                if activation.reason != "success":
                    cancel_duel_publication_in_transaction(
                        cursor, chat_id, publication_id, published_at_ms,
                    )
                    return PersistentDuelPublicationResult("stale_prompt", None, session)
                session = activation.session
        elif kind == "final_result":
            if session["status"] != "finished" or session["result"]["kind"] != "finalized":
                return PersistentDuelPublicationResult("invalid_state", publication, session)
        elif kind == "pocket_drop":
            drop = publication["payload"]["drop"]
            if not bind_duel_item_event_message_in_transaction(
                cursor, chat_id, drop["event_id"], message_id, published_at_ms,
                format_pocket_drop_announcement(publication, session),
            ):
                return PersistentDuelPublicationResult("event_unavailable", publication, session)
        else:
            raise RuntimeError("Unsupported duel publication kind")

        if not mark_duel_publication_delivered_in_transaction(
            cursor, chat_id, publication_id, attempt_count,
            acknowledged_message_id, published_at_ms,
        ):
            raise RuntimeError("Leased duel publication could not be acknowledged")
        current = get_duel_publication_in_transaction(cursor, chat_id, publication_id)
        return PersistentDuelPublicationResult(reason, current, session)


def process_persistent_duel_pocket_drop(
    chat_id: int, duel_id: int, *, now_ms: int | None = None,
) -> PersistentDuelPocketResult:
    """After final publication, checkpoint the existing concrete pocket drop once."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        session = get_duel_session_in_transaction(chat_id, duel_id, cursor=cursor)
        if session is None:
            return PersistentDuelPocketResult("not_found")
        if (
            session["status"] != "finished"
            or not isinstance(session["result"], dict)
            or session["result"].get("kind") != "finalized"
        ):
            return PersistentDuelPocketResult("not_finished", session)
        if session["pocket_done_at"] is not None:
            return PersistentDuelPocketResult("already_done", session)
        final_publication = get_duel_publication_by_kind_in_transaction(
            cursor, chat_id, duel_id, "final_result",
        )
        if final_publication is None or final_publication["status"] != "delivered":
            return PersistentDuelPocketResult("not_published", session)
        loser_id = session["result"]["loser_user_id"]
        inventory = get_droppable_duel_inventory(
            get_duel_inventory_in_transaction(cursor, chat_id, loser_id)
        )
        timestamp = utc_unix_milliseconds() if now_ms is None else now_ms
        drop = None
        publication = None
        reason = "empty_inventory"
        if inventory:
            if random.random() >= DUEL_ITEM_DROP_CHANCE:
                reason = "miss"
            else:
                instance = random.choice(inventory)
                drop = create_duel_item_event_from_inventory_in_transaction(
                    cursor, chat_id, loser_id, instance["id"],
                )
                if drop is None:
                    reason = "unavailable_slot_or_instance"
                else:
                    publication = create_duel_publication_in_transaction(
                        cursor, chat_id, duel_id, "pocket_drop", None,
                        {
                            "drop": drop,
                            "text": get_text(
                                "duel.finish.item_drop",
                                item_name=escape(get_duel_item_name(drop["item_id"])),
                            ),
                        }, timestamp,
                    )
                    reason = "dropped"
        if not mark_duel_pocket_done_in_transaction(
            chat_id, duel_id, timestamp, cursor=cursor,
        ):
            raise RuntimeError("Validated pocket checkpoint could not be stored")
        current = get_duel_session_in_transaction(chat_id, duel_id, cursor=cursor)
        return PersistentDuelPocketResult(reason, current, drop, publication)


def compensate_persistent_duel_drop(
    chat_id: int, duel_id: int, *, now_ms: int | None = None,
) -> PersistentDuelPocketResult:
    """Explicit permanent-failure policy: restore the original unclaimed instance."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        session = get_duel_session_in_transaction(chat_id, duel_id, cursor=cursor)
        if session is None:
            return PersistentDuelPocketResult("not_found")
        publication = get_duel_publication_by_kind_in_transaction(
            cursor, chat_id, duel_id, "pocket_drop",
        )
        if publication is None:
            return PersistentDuelPocketResult("no_drop", session)
        if publication["status"] == "cancelled":
            return PersistentDuelPocketResult("already_compensated", session)
        if publication["status"] == "leased":
            return PersistentDuelPocketResult("in_flight", session)
        drop = publication["payload"]["drop"]
        if not restore_unpublished_duel_drop_in_transaction(
            cursor, drop, publication["message_id"],
        ):
            return PersistentDuelPocketResult("event_unavailable", session)
        timestamp = utc_unix_milliseconds() if now_ms is None else now_ms
        if not cancel_duel_publication_in_transaction(
            cursor, chat_id, publication["id"], timestamp,
        ):
            raise RuntimeError("Restored pocket drop outbox could not be cancelled")
        current = get_duel_publication_in_transaction(cursor, chat_id, publication["id"])
        return PersistentDuelPocketResult("compensated", session, drop, current)


def _apply_persistent_attack(
    cursor, chat_id: int, duel_id: int, session: dict, zone: str, now_ms: int,
) -> PersistentDuelActionResult:
    if not save_duel_attack_in_transaction(
        chat_id, duel_id, session["turn_id"], session["attacker_user_id"],
        zone, now_ms, cursor=cursor,
    ):
        raise RuntimeError("Validated duel attack could not be saved")
    current = get_duel_session_in_transaction(chat_id, duel_id, cursor=cursor)
    create_duel_publication_in_transaction(
        cursor, chat_id, duel_id, "block_prompt", current["turn_id"],
        _prompt_payload(current), now_ms,
    )
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
    if not terminal:
        create_duel_publication_in_transaction(
            cursor, chat_id, duel_id, "attack_prompt", current["turn_id"],
            _prompt_payload(current), now_ms,
        )
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
        if timestamp >= session["deadline_at"]:
            return PersistentDuelActionResult("turn_expired")
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
        if timestamp >= session["deadline_at"]:
            return PersistentDuelActionResult("turn_expired")
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
    original_message_id: int | None = None,
) -> PersistentDuelStartResult:
    """Reserve a chat's duel slot and snapshot two eligible registered users.

    SQLite serializes admission and
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
            original_message_id=original_message_id,
            now_ms=now_ms,
            cursor=cursor,
        )
        create_duel_publication_in_transaction(
            cursor, chat_id, session["id"], "attack_prompt", session["turn_id"],
            _prompt_payload(session), session["created_at"],
        )
        return PersistentDuelStartResult(None, session)


def get_duel_profile(chat_id: int, user_id: int, *, read_only: bool = False) -> DuelProfile | None:
    """Return an existing participant's state and inventory in this chat only."""
    user = (get_duel_user_by_id(chat_id, user_id, read_only=True) if read_only
            else get_duel_user_by_id(chat_id, user_id))
    if user is None:
        return None
    return DuelProfile(
        user=user,
        inventory=get_duel_inventory(chat_id, user_id),
        ineligibility=_get_duel_participant_ineligibility(user),
    )


def list_duel_opponents(chat_id: int, user_id: int, limit: int = 20, *,
                        read_only: bool = False) -> DuelOpponentList:
    """Build the same eligible top-list used by /duel, without starting a fight."""
    initiator = (get_duel_user_by_id(chat_id, user_id, read_only=True) if read_only
                 else get_duel_user_by_id(chat_id, user_id))
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

        opponent = (get_duel_user_by_username(username, chat_id, read_only=True)
                    if read_only else get_duel_user_by_username(username, chat_id))
        if opponent is None or _get_duel_participant_ineligibility(opponent) is not None:
            continue

        title = format_user_title_plain({
            "display_name": (display_name or username).lstrip("@"),
            "dwarf_name": opponent.get("dwarf_name"),
        })
        opponents.append(DuelOpponent(opponent["user_id"], username, title))

    return DuelOpponentList(None, opponents)
