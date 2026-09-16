import asyncio
import difflib
import json
import logging
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from telegram.error import BadRequest
from telegram.ext import ContextTypes, JobQueue
from text_resources import get_text

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import (
    get_all_pizda_candidates,
    get_bot_meta,
    get_pizda_candidate_chats,
    init_db,
    mark_pizda_candidate_used,
    pick_pizda_candidates,
    save_pizda_candidate,
    set_bot_meta,
)

EXPORT_PATH = Path(__file__).resolve().parent.parent / "data" / "result.json"
SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "pizda_candidates_seed.json"
MOSCOW_TZ = pytz.timezone("Europe/Moscow")
JOB_NAME = "past_pizda"
MIN_GAP_DAYS = 2
MAX_GAP_DAYS = 7
WINDOW_START_HOUR = 10
WINDOW_END_HOUR = 22
LAST_RUN_KEY = "past_pizda_last_run"
NEXT_RUN_KEY = "past_pizda_next_run"
_KEYWORD_LIST = ("да", "нет", "da", "net")
_YES = {"да", "da"}
_NO = {"нет", "net"}
_SUPERGROUP_TYPES = {
    "private_supergroup",
    "public_supergroup",
    "private_channel",
    "public_channel",
}


def match_yes_no(text: str):
    normalized = (text or "").strip().lower()
    if not normalized or "давг" in normalized:
        return None

    max_ratio = 0
    max_keyword = ""
    for keyword in _KEYWORD_LIST:
        ratio = difflib.SequenceMatcher(None, keyword, normalized).ratio()
        if ratio > max_ratio and ratio >= 0.6:
            max_ratio = ratio
            max_keyword = keyword

    if max_keyword in _YES:
        return "да"
    if max_keyword in _NO:
        return "нет"
    return None


def flatten_export_text(text) -> str:
    if isinstance(text, str):
        return text.strip()
    if not isinstance(text, list):
        return ""

    parts = []
    for chunk in text:
        if isinstance(chunk, str):
            parts.append(chunk)
        elif isinstance(chunk, dict):
            parts.append(chunk.get("text") or "")
    return "".join(parts).strip()


def export_chat_id_to_bot(export_id: int, chat_type: str) -> int:
    if chat_type in _SUPERGROUP_TYPES:
        return int(f"-100{export_id}")
    return export_id


def _to_unix(created_at) -> int:
    if hasattr(created_at, "timestamp"):
        return int(created_at.timestamp())
    return int(created_at)


def remember_pizda_candidate(chat_id: int, message_id: int, created_at, used: bool = False) -> bool:
    return save_pizda_candidate(chat_id, message_id, _to_unix(created_at), int(used))


def import_from_export(path: Path = EXPORT_PATH) -> int:
    init_db()
    with open(path, encoding="utf-8") as export_file:
        data = json.load(export_file)

    chat_id = export_chat_id_to_bot(data["id"], data.get("type", ""))
    inserted = 0
    for message in data.get("messages", []):
        if message.get("type") != "message":
            continue
        text = flatten_export_text(message.get("text"))
        if match_yes_no(text) != "да":
            continue
        created_at = int(message.get("date_unixtime") or 0)
        if remember_pizda_candidate(chat_id, message["id"], created_at):
            inserted += 1
    logging.info("Imported %s pizda candidates for chat_id=%s", inserted, chat_id)
    return inserted


def export_seed(path: Path = SEED_PATH) -> int:
    candidates = get_all_pizda_candidates()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as seed_file:
        json.dump(candidates, seed_file, ensure_ascii=False, indent=2)
    logging.info("Exported %s pizda candidates to %s", len(candidates), path)
    return len(candidates)


def import_from_seed(path: Path = SEED_PATH) -> int:
    init_db()
    with open(path, encoding="utf-8") as seed_file:
        candidates = json.load(seed_file)

    inserted = 0
    for item in candidates:
        if remember_pizda_candidate(
            item["chat_id"],
            item["message_id"],
            item["created_at"],
            used=bool(item.get("used")),
        ):
            inserted += 1
    logging.info("Imported %s pizda candidates from seed", inserted)
    return inserted


def _now_moscow() -> datetime:
    return datetime.now(MOSCOW_TZ)


def _start_of_today_ts() -> int:
    now = _now_moscow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def _parse_meta_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(value)


def _random_time_on(day) -> datetime:
    minutes = random.randint(0, (WINDOW_END_HOUR - WINDOW_START_HOUR) * 60)
    hour = WINDOW_START_HOUR + minutes // 60
    minute = minutes % 60
    naive = datetime(day.year, day.month, day.day, hour, minute)
    return MOSCOW_TZ.localize(naive)


def _next_slot_in_window(now: datetime) -> datetime:
    today_start = now.replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    today_end = now.replace(hour=WINDOW_END_HOUR, minute=0, second=0, microsecond=0)
    if now < today_start:
        return _random_time_on(now.date())
    remaining_minutes = int((today_end - now).total_seconds() // 60)
    if remaining_minutes > 5:
        return now + timedelta(minutes=random.randint(1, remaining_minutes))
    return _random_time_on(now.date() + timedelta(days=1))


def compute_next_run(now: datetime, last_run):
    if last_run is None:
        return _next_slot_in_window(now)

    min_date = last_run.date() + timedelta(days=MIN_GAP_DAYS)
    max_date = last_run.date() + timedelta(days=MAX_GAP_DAYS)
    if now.date() > max_date:
        return _next_slot_in_window(now)

    earliest = now.date() if now < now.replace(hour=WINDOW_END_HOUR, minute=0, second=0, microsecond=0) else now.date() + timedelta(days=1)
    earliest = max(earliest, min_date)
    if earliest > max_date:
        return _next_slot_in_window(now)

    day_offset = random.randint(0, (max_date - earliest).days)
    return _random_time_on(earliest + timedelta(days=day_offset))


def schedule_past_pizda_job(job_queue: JobQueue):
    now = _now_moscow()
    stored_next = _parse_meta_dt(get_bot_meta(NEXT_RUN_KEY))
    last_run = _parse_meta_dt(get_bot_meta(LAST_RUN_KEY))

    if stored_next and stored_next > now:
        when = stored_next
    else:
        when = compute_next_run(now, last_run)
        set_bot_meta(NEXT_RUN_KEY, when.isoformat())

    for job in job_queue.get_jobs_by_name(JOB_NAME):
        job.schedule_removal()
    job_queue.run_once(past_pizda_job, when=when, name=JOB_NAME)
    logging.info("Next past_pizda at %s", when.isoformat())


async def run_past_pizda_in_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    before_ts = _start_of_today_ts()
    limit = random.randint(1, 5)
    message_ids = pick_pizda_candidates(chat_id, before_ts, limit)
    for message_id in message_ids:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=get_text("past_pizda.messages.reply"),
                reply_to_message_id=message_id,
            )
        except BadRequest as exc:
            logging.info("Skip past pizda reply chat_id=%s message_id=%s: %s", chat_id, message_id, exc)
        mark_pizda_candidate_used(chat_id, message_id)
        await asyncio.sleep(1)


async def past_pizda_job(context: ContextTypes.DEFAULT_TYPE):
    before_ts = _start_of_today_ts()
    chats = get_pizda_candidate_chats(before_ts)
    for (chat_id,) in chats:
        await run_past_pizda_in_chat(context, chat_id)

    set_bot_meta(LAST_RUN_KEY, _now_moscow().isoformat())
    set_bot_meta(NEXT_RUN_KEY, None)
    if context.job_queue:
        schedule_past_pizda_job(context.job_queue)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1 and sys.argv[1] == "--from-export":
        print(f"Imported {import_from_export()} candidates")
    elif len(sys.argv) > 1 and sys.argv[1] == "--export-seed":
        print(f"Exported {export_seed()} candidates")
    else:
        print(f"Imported {import_from_seed()} candidates")
