"""Telegram publication and recovery for durable ordinary duels."""

import logging
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import MAX_DAILY_POINTS, WINNER_100_PTS_GIF
from duel_outbox_repository import claim_duel_publication, release_duel_publication
from duel_outbox_repository import list_retryable_duel_publications, list_retryable_duel_publication_chat_ids
from duel_session_repository import (
    get_duel_session, list_due_duel_sessions, list_ready_pocket_duel_sessions,
    list_recoverable_duel_chat_ids, list_terminal_pending_duel_sessions,
    utc_unix_milliseconds,
)
from handlers.duel_items import DUEL_ITEM_EVENT_CALLBACK_PREFIX
from handlers.duel_messaging import AUTO_DELETE_DELAY, delete_messages_job
from handlers.duel_service import (
    acknowledge_persistent_duel_publication, finalize_persistent_duel,
    process_persistent_duel_pocket_drop, resolve_persistent_duel_timeout,
)
from database import format_user_title
from text_resources import get_text


@dataclass(frozen=True)
class PersistentDuelSendResult:
    reason: str
    publication: dict | None = None


async def publish_persistent_duel_outbox(
    chat_id: int, publication_id: int, bot, *,
    claim_time_ms: int | None = None, published_at_ms: int | None = None,
    job_queue=None,
) -> PersistentDuelSendResult:
    """Lease, send/edit outside SQLite, then acknowledge on a short transaction."""
    publication = claim_duel_publication(chat_id, publication_id, now_ms=claim_time_ms)
    if publication is None:
        return PersistentDuelSendResult("not_retryable")

    session = get_duel_session(chat_id, publication["duel_id"])
    if session is None:
        release_duel_publication(chat_id, publication_id, publication["attempt_count"])
        return PersistentDuelSendResult("not_found")

    kind = publication["kind"]
    payload = publication["payload"]
    try:
        if kind in ("attack_prompt", "block_prompt"):
            # The live Telegram flow edits its one duel message, with send fallback.
            from handlers.duel import _get_block_keyboard, _get_strike_keyboard

            keyboard = (
                _get_strike_keyboard(publication["turn_id"], publication["duel_id"])
                if kind == "attack_prompt"
                else _get_block_keyboard(publication["turn_id"], publication["duel_id"])
            )
            previous_message_id = session["message_id"]
            if previous_message_id is not None:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=previous_message_id,
                        text=payload["text"], parse_mode="HTML", reply_markup=keyboard,
                    )
                    message_id = previous_message_id
                except Exception:
                    logging.exception("Could not edit persistent duel prompt in chat %s", chat_id)
                    message = await bot.send_message(
                        chat_id=chat_id, text=payload["text"],
                        parse_mode="HTML", reply_markup=keyboard,
                    )
                    message_id = message.message_id
            else:
                message = await bot.send_message(
                    chat_id=chat_id, text=payload["text"],
                    parse_mode="HTML", reply_markup=keyboard,
                )
                message_id = message.message_id
        elif kind == "final_result":
            if session["message_id"] is not None:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=session["message_id"])
                except Exception:
                    logging.exception("Could not remove old duel prompt in chat %s", chat_id)
            message = await bot.send_message(
                chat_id=chat_id, text=payload["text"], parse_mode="HTML",
            )
            message_id = message.message_id
        elif kind == "pocket_drop":
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
                get_text("duel.item_event.button"),
                callback_data=f"{DUEL_ITEM_EVENT_CALLBACK_PREFIX}{payload['drop']['event_id']}",
            )]])
            message = await bot.send_message(
                chat_id=chat_id, text=payload["text"],
                parse_mode="HTML", reply_markup=keyboard,
            )
            message_id = message.message_id
        else:
            raise RuntimeError("Unsupported persistent duel publication kind")
    except Exception:
        logging.exception("Persistent duel publication send failed in chat %s", chat_id)
        release_duel_publication(chat_id, publication_id, publication["attempt_count"])
        return PersistentDuelSendResult("send_failed", publication)

    timestamp = utc_unix_milliseconds() if published_at_ms is None else published_at_ms
    try:
        acknowledgement = acknowledge_persistent_duel_publication(
            chat_id, publication_id, publication["attempt_count"], message_id, timestamp,
        )
    except Exception:
        # Telegram may have accepted the message. Keep the lease for retry;
        # never undo committed game state on a transport/ack failure.
        logging.exception("Persistent duel publication acknowledgement failed in chat %s", chat_id)
        return PersistentDuelSendResult("ack_failed", publication)
    if (
        acknowledgement.reason in ("delivered", "already_active")
        and kind == "attack_prompt" and publication["turn_id"] == 1
        and session["original_message_id"] is not None
    ):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=session["original_message_id"])
        except Exception:
            logging.exception("Could not remove original duel command in chat %s", chat_id)
    if acknowledgement.reason == "delivered" and kind == "final_result":
        result = session["result"]
        if not result["is_dick_stolen"] and job_queue is not None:
            job_queue.run_once(
                delete_messages_job, when=AUTO_DELETE_DELAY,
                data={"chat_id": chat_id, "message_ids": [message_id]},
            )
        if result["winner_reached_max"] and WINNER_100_PTS_GIF:
            winner = (session["player1_snapshot"] if session["player1_user_id"] ==
                      result["winner_user_id"] else session["player2_snapshot"])
            try:
                await bot.send_animation(
                    chat_id=chat_id, animation=WINNER_100_PTS_GIF,
                    caption=get_text("duel.finish.max_points_caption",
                                     winner_title=format_user_title(winner),
                                     max_daily_points=MAX_DAILY_POINTS),
                    parse_mode="HTML",
                )
            except Exception:
                logging.exception("Could not publish duel max-points animation in chat %s", chat_id)
    return PersistentDuelSendResult(acknowledgement.reason, acknowledgement.publication)


async def recover_persistent_duel_chat(chat_id: int, bot, *, now_ms: int | None = None,
                                       job_queue=None) -> None:
    """Advance committed checkpoints, publications, pocket drops, then due turns."""
    now = utc_unix_milliseconds() if now_ms is None else now_ms
    for session in list_terminal_pending_duel_sessions(chat_id):
        try:
            finalize_persistent_duel(chat_id, session["id"], now_ms=now)
        except Exception:
            logging.exception("Could not finalize duel %s in chat %s", session["id"], chat_id)

    attempted = set()

    async def publish_ready():
        for publication in list_retryable_duel_publications(chat_id, now):
            if publication["id"] in attempted:
                continue
            attempted.add(publication["id"])
            try:
                await publish_persistent_duel_outbox(chat_id, publication["id"], bot,
                                                    claim_time_ms=now, job_queue=job_queue)
            except Exception:
                logging.exception("Could not process duel outbox %s", publication["id"])

    def pocket_ready():
        for session in list_ready_pocket_duel_sessions(chat_id):
            try:
                process_persistent_duel_pocket_drop(chat_id, session["id"], now_ms=now)
            except Exception:
                logging.exception("Could not process duel pocket %s", session["id"])

    await publish_ready()
    pocket_ready()
    await publish_ready()

    # Publishing sessions have no deadline. Only a published, active prompt can time out.
    for session in list_due_duel_sessions(chat_id, now):
        try:
            result = resolve_persistent_duel_timeout(chat_id, session["id"],
                                                     session["turn_id"], now)
            if not result.accepted:
                continue
            player = (session["player1_snapshot"] if session["player1_user_id"] ==
                      (session["attacker_user_id"] if session["phase"] == "attack"
                       else session["defender_user_id"]) else session["player2_snapshot"])
            key = "duel.live.timeout.attack" if session["phase"] == "attack" else "duel.live.timeout.block"
            try:
                notice = await bot.send_message(
                    chat_id=chat_id, text=get_text(key, title=format_user_title(player)),
                    parse_mode="HTML",
                )
                if job_queue is not None:
                    job_queue.run_once(
                        delete_messages_job, when=AUTO_DELETE_DELAY,
                        data={"chat_id": chat_id, "message_ids": [notice.message_id]},
                    )
            except Exception:
                logging.exception("Could not publish duel timeout notice in chat %s", chat_id)
            if result.reason == "terminal_pending":
                finalize_persistent_duel(chat_id, session["id"], now_ms=now)
        except Exception:
            logging.exception("Could not resolve duel timeout %s in chat %s", session["id"], chat_id)
    await publish_ready()
    pocket_ready()
    await publish_ready()


async def recover_persistent_duels(bot, *, job_queue=None) -> None:
    """One bounded pass; a later worker tick retries failed or expired leases."""
    chats = set(list_recoverable_duel_chat_ids())
    chats.update(list_retryable_duel_publication_chat_ids(utc_unix_milliseconds()))
    for chat_id in sorted(chats):
        try:
            await recover_persistent_duel_chat(chat_id, bot, job_queue=job_queue)
        except Exception:
            logging.exception("Persistent duel recovery failed in chat %s", chat_id)


async def persistent_duel_worker_job(context) -> None:
    await recover_persistent_duels(context.bot, job_queue=context.job_queue)
