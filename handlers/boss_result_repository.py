"""Persist only completed boss outcomes; active battles remain in memory."""

import json
import time

from database import format_user_title_plain, get_db
from handlers.duel_text import _boss_battle_hero


def build_boss_result(chat_id: int, battle: dict, boss_id: str,
                      required_hits: int, *, victory: bool) -> dict:
    participants = list(battle["participants"].values())
    hero = _boss_battle_hero(participants)
    return {
        "chat_id": chat_id,
        "battle_id": battle["battle_id"],
        "boss_id": boss_id,
        "boss_name": battle["boss"]["name"],
        "finished_at": int(time.time() * 1000),
        "outcome": "victory" if victory else "defeat",
        "hits": battle["hits"],
        "required_hits": required_hits,
        "rounds": battle["round"],
        "participants": [
            {
                "user_id": participant["tg_user"].id,
                "title": format_user_title_plain(participant["data"]),
                "alive": bool(participant["alive"]),
                "hits": participant.get("hits", 0),
                "misses": participant.get("misses", 0),
                "blocks": participant.get("blocks", 0),
                "rounds_survived": participant.get("rounds_survived", 0),
                "death_round": participant.get("death_round"),
            }
            for participant in participants
        ],
        "hero_user_id": hero["tg_user"].id if hero else None,
        "rewarded_user_ids": [],
        "item_loot": None,
    }


def save_boss_result(result: dict) -> bool:
    """The chat/battle key makes repeat final writes harmless."""
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO boss_battle_results (
                   chat_id, battle_id, boss_id, boss_name, finished_at, outcome,
                   hits, required_hits, rounds, participants_json, hero_user_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result["chat_id"], result["battle_id"], result["boss_id"],
                result["boss_name"], result["finished_at"], result["outcome"],
                result["hits"], result["required_hits"], result["rounds"],
                json.dumps(result["participants"], ensure_ascii=False),
                result["hero_user_id"],
            ),
        )
        return cursor.rowcount == 1


def update_boss_result_rewards(chat_id: int, battle_id: str,
                               rewarded_user_ids: list[int]) -> None:
    with get_db() as conn:
        conn.execute(
            """UPDATE boss_battle_results SET rewarded_user_ids_json = ?
               WHERE chat_id = ? AND battle_id = ?""",
            (json.dumps(rewarded_user_ids), chat_id, battle_id),
        )


def update_boss_result_loot(chat_id: int, battle_id: str, item_loot: dict) -> None:
    with get_db() as conn:
        conn.execute(
            """UPDATE boss_battle_results SET item_loot_json = ?
               WHERE chat_id = ? AND battle_id = ? AND item_loot_json IS NULL""",
            (json.dumps(item_loot, ensure_ascii=False), chat_id, battle_id),
        )


def update_boss_result_narrative(chat_id: int, battle_id: str,
                                 narrative: dict) -> None:
    with get_db() as conn:
        conn.execute(
            """UPDATE boss_battle_results SET narrative_json = ?
               WHERE chat_id = ? AND battle_id = ? AND narrative_json IS NULL""",
            (json.dumps(narrative, ensure_ascii=False), chat_id, battle_id),
        )


def get_latest_boss_result(chat_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """SELECT chat_id, battle_id, boss_id, boss_name, finished_at, outcome,
                      hits, required_hits, rounds, participants_json, hero_user_id,
                      rewarded_user_ids_json, item_loot_json, narrative_json
               FROM boss_battle_results
               WHERE chat_id = ?
               ORDER BY finished_at DESC, rowid DESC LIMIT 1""",
            (chat_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "chat_id": row[0], "battle_id": row[1], "boss_id": row[2],
        "boss_name": row[3], "finished_at": row[4], "outcome": row[5],
        "hits": row[6], "required_hits": row[7], "rounds": row[8],
        "participants": json.loads(row[9]), "hero_user_id": row[10],
        "rewarded_user_ids": json.loads(row[11]),
        "item_loot": json.loads(row[12]) if row[12] is not None else None,
        "narrative": json.loads(row[13]) if row[13] is not None else None,
    }
