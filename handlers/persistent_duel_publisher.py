"""Dormant Telegram adapter for durable persistent-duel publications.

It is intentionally not registered in bot.py. Telegram I/O happens only after
the gameplay transaction; duplicate sends remain possible across a crash.
"""

import logging
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from duel_outbox_repository import claim_duel_publication, release_duel_publication
from duel_session_repository import get_duel_session, utc_unix_milliseconds
from handlers.duel_items import DUEL_ITEM_EVENT_CALLBACK_PREFIX
from handlers.duel_service import acknowledge_persistent_duel_publication
from text_resources import get_text


@dataclass(frozen=True)
class PersistentDuelSendResult:
    reason: str
    publication: dict | None = None


async def publish_persistent_duel_outbox(
    chat_id: int, publication_id: int, bot, *,
    claim_time_ms: int | None = None, published_at_ms: int | None = None,
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
                _get_strike_keyboard(publication["turn_id"])
                if kind == "attack_prompt"
                else _get_block_keyboard(publication["turn_id"])
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
    return PersistentDuelSendResult(acknowledgement.reason, acknowledgement.publication)
