"""Personal current-game-day chronicle for registered gnomes."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

from config import DUEL_TIMEZONE
from database import (
    format_user_title_plain,
    get_duel_user_by_id,
    get_huecrab_tame_for_recap,
    list_finished_duels_for_recap,
    list_game_events,
    list_item_receipts_for_recap,
)
from handlers.duel_items import get_duel_item_name
from handlers.duel_messaging import send_and_schedule
from text_resources import get_text


TELEGRAM_SAFE_TEXT_LIMIT = 4000


def game_day_bounds_ms(when: datetime | None = None) -> tuple[int, int]:
    """Return the half-open UTC interval for the current Moscow game day."""
    instant = when if when is not None else datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Recap clock must be timezone-aware")
    local = instant.astimezone(ZoneInfo(DUEL_TIMEZONE))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _short(value: object, limit: int = 100) -> str:
    text = str(value or "—").replace("\n", " ").replace("\r", " ")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _item_name(item_id: object) -> str:
    try:
        return _short(get_duel_item_name(str(item_id)))
    except Exception:
        return _short(item_id)


def _native_duel_as_event(duel: dict) -> dict:
    result = duel["result"]
    player1 = duel["player1"]
    player2 = duel["player2"]
    participants = {player1["user_id"]: player1, player2["user_id"]: player2}
    winner = participants[result["winner_user_id"]]
    loser = participants[result["loser_user_id"]]
    return {
        "event_type": "duel_finished",
        "source_id": f"duel:{duel['duel_id']}",
        "occurred_at": duel["finished_at"],
        "data": {
            "duel_id": duel["duel_id"],
            "initiator_user_id": player1["user_id"],
            "initiator_name": format_user_title_plain(player1),
            "opponent_user_id": player2["user_id"],
            "opponent_name": format_user_title_plain(player2),
            "winner_user_id": winner["user_id"],
            "winner_name": format_user_title_plain(winner),
            "loser_user_id": loser["user_id"],
            "loser_name": format_user_title_plain(loser),
            "winner_points_delta": result.get("winner_points_delta", 0),
            "loser_points_delta": result.get("loser_points_delta", 0),
            "is_dick_stolen": bool(result.get("is_dick_stolen")),
            "stolen_item": result.get("stolen_item"),
            "rounds": duel["rounds"],
        },
    }


def _plural_times(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return "раз"
    return "раза" if count % 10 in (2, 3, 4) else "раз"


def _fit_message(header: str, sections: list[tuple[str, list[str]]], total: str) -> str:
    """Keep whole HTML lines and the day totals under Telegram's hard limit."""
    chosen: list[str] = [header]
    omitted = 0
    for title, lines in sections:
        accepted: list[str] = []
        for line in lines:
            candidate = "\n\n".join(chosen + [title + "\n" + "\n".join(accepted + [line]), total])
            if len(candidate) > TELEGRAM_SAFE_TEXT_LIMIT:
                omitted += 1
            else:
                accepted.append(line)
        if accepted:
            chosen.append(title + "\n" + "\n".join(accepted))
    if omitted:
        marker = get_text("recap.more", count=omitted)
        candidate = "\n\n".join(chosen + [marker, total])
        if len(candidate) <= TELEGRAM_SAFE_TEXT_LIMIT:
            chosen.append(marker)
    return "\n\n".join(chosen + [total])


def build_recap_model(events: list[dict], user_id: int) -> dict:
    """Plain-text, JSON-safe chronicle shared by Telegram and Mini App."""
    opponents: dict[int, dict[str, int | str]] = {}
    theft_lines: list[str] = []
    found = Counter()
    received = Counter()
    pet_received = Counter()
    lost = Counter()
    missed: list[str] = []
    tamed = 0
    missed_ids = {
        event.get("data", {}).get("item_event_id")
        for event in events if event.get("event_type") == "item_missed"
    }

    for event in events:
        kind = event.get("event_type")
        data = event.get("data") or {}
        if kind == "duel_finished":
            if data.get("initiator_user_id") == user_id:
                opponent_id = data.get("opponent_user_id")
                opponent_name = _short(data.get("opponent_name"))
                direction = "outgoing"
            else:
                opponent_id = data.get("initiator_user_id")
                opponent_name = _short(data.get("initiator_name"))
                direction = "incoming"
            if opponent_id is None:
                continue
            opponent = opponents.setdefault(opponent_id, {
                "name": opponent_name, "incoming": 0, "outgoing": 0,
                "wins": 0, "losses": 0,
            })
            opponent[direction] += 1
            if data.get("winner_user_id") == user_id:
                opponent["wins"] += 1
            else:
                opponent["losses"] += 1
            if data.get("is_dick_stolen"):
                theft_opponent_name = (
                    data.get("loser_name")
                    if data.get("winner_user_id") == user_id
                    else data.get("winner_name")
                )
                theft_lines.append(get_text(
                    "recap.theft_line",
                    action=get_text(
                        "recap.stole" if data.get("winner_user_id") == user_id
                        else "recap.stolen_from",
                    ),
                    opponent=_short(theft_opponent_name),
                ))
            stolen_item = data.get("stolen_item")
            if isinstance(stolen_item, dict) and stolen_item.get("item_name"):
                item_name = _short(stolen_item["item_name"])
                if data.get("winner_user_id") == user_id:
                    received[item_name] += 1
                else:
                    lost[(item_name, get_text("recap.loss_stolen"))] += 1
        elif kind == "dick_stolen":
            theft_lines.append(get_text("recap.huegryz_theft"))
        elif kind == "item_found" and data.get("item_event_id") not in missed_ids:
            found[_item_name(data.get("item_id"))] += 1
        elif kind == "item_received":
            target = pet_received if data.get("method") == "huecrab" else received
            target[_item_name(data.get("item_id"))] += 1
        elif kind == "item_lost":
            reason = get_text("recap.loss_lure" if data.get("reason") == "lure" else "recap.loss_duel")
            lost[(_item_name(data.get("item_id")), reason)] += 1
        elif kind == "item_missed":
            missed.append(get_text(
                "recap.item_missed",
                item=_item_name(data.get("item_id")),
                player=_short(data.get("claimed_by_name")),
            ))
        elif kind == "huecrab_tamed":
            tamed += 1

    loot_lines = []
    loot_lines.extend(get_text("recap.item_found", item=name, count=count) for name, count in found.most_common())
    loot_lines.extend(get_text("recap.item_received", item=name, count=count) for name, count in received.most_common())
    loot_lines.extend(missed)
    loot_lines.extend(
        get_text("recap.item_lost", item=item, reason=reason, count=count)
        for (item, reason), count in lost.most_common()
    )
    pet_lines = []
    if tamed:
        pet_lines.append(get_text("recap.pet_tamed", count=tamed))
    pet_lines.extend(
        get_text("recap.pet_loot", item=name, count=count)
        for name, count in pet_received.most_common()
    )
    wins = sum(opponent["wins"] for opponent in opponents.values())
    losses = sum(opponent["losses"] for opponent in opponents.values())
    return {
        "empty": not (opponents or theft_lines or loot_lines or pet_lines),
        "duels": [{"user_id": opponent_id, **opponent}
                  for opponent_id, opponent in opponents.items()],
        "thefts": theft_lines,
        "loot": loot_lines,
        "pets": pet_lines,
        "totals": {
            "wins": wins, "losses": losses,
            "found": sum(found.values()) + len(missed),
            "received": sum(received.values()) + sum(pet_received.values()),
            "lost": sum(lost.values()),
        },
    }


def build_recap_text(events: list[dict], user_id: int) -> str:
    model = build_recap_model(events, user_id)
    if model["empty"]:
        return get_text("recap.empty")

    sections: list[tuple[str, list[str]]] = []
    if model["duels"]:
        duel_summary = []
        for opponent in model["duels"]:
            directions = []
            if opponent["incoming"]:
                directions.append(get_text(
                    "recap.attacked_by", name=escape(opponent["name"]),
                    count=opponent["incoming"], times=_plural_times(opponent["incoming"]),
                ))
            if opponent["outgoing"]:
                directions.append(get_text(
                    "recap.attacked", name=escape(opponent["name"]),
                    count=opponent["outgoing"], times=_plural_times(opponent["outgoing"]),
                ))
            directions.append(get_text(
                "recap.duel_results", wins=opponent["wins"], losses=opponent["losses"],
            ))
            duel_summary.append("\n".join(directions))
        sections.append((get_text("recap.sections.duels"), duel_summary))
    for section, key in (("thefts", "thefts"), ("loot", "loot"), ("pets", "pets")):
        if model[key]:
            sections.append((get_text(f"recap.sections.{section}"),
                             [escape(line) for line in model[key]]))
    totals = model["totals"]
    total = get_text(
        "recap.total", **totals,
    )
    return _fit_message(get_text("recap.title"), sections, total)


async def recap_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_user is None:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if get_duel_user_by_id(chat_id, user_id, read_only=True) is None:
        await send_and_schedule(update, context, get_text("recap.not_registered"))
        return

    events = load_recap_events(chat_id, user_id)
    await send_and_schedule(update, context, build_recap_text(events, user_id))


def load_recap_events(chat_id: int, user_id: int, when: datetime | None = None) -> list[dict]:
    """Read this user's current Moscow-day events, including legacy fallbacks."""
    start_ms, end_ms = game_day_bounds_ms(when)
    events = list_game_events(chat_id, user_id, start_ms, end_ms)
    sources = {event.get("source_id") for event in events}
    for duel in list_finished_duels_for_recap(chat_id, user_id, start_ms, end_ms):
        source = f"duel:{duel['duel_id']}"
        if source not in sources:
            events.append(_native_duel_as_event(duel))
    for receipt in list_item_receipts_for_recap(chat_id, user_id, start_ms, end_ms):
        source = f"item:{receipt['item_event_id']}"
        if source not in sources:
            events.append({
                "event_type": "item_received", "source_id": source,
                "occurred_at": receipt["occurred_at"], "data": receipt,
            })
    if not any(event.get("event_type") == "huecrab_tamed" for event in events):
        tame = get_huecrab_tame_for_recap(chat_id, user_id, start_ms, end_ms)
        if tame is not None:
            events.append({
                "event_type": "huecrab_tamed", "source_id": None,
                "occurred_at": tame["occurred_at"], "data": {},
            })
    events.sort(key=lambda event: (event.get("occurred_at", 0), event.get("id", 0)))
    return events
