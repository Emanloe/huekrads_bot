"""Read-only presentation snapshot of the Telegram boss world for Mini App."""

from database import format_user_title_plain
from handlers import boss_registration, duel
from handlers.boss_presentation import BOSS_REQUIRED_HITS
from handlers.boss_result_repository import get_latest_boss_result
from text_resources import get_text


def _available_actions(battle: dict, viewer: dict | None) -> list[dict]:
    phase = battle["phase"]
    if phase == "join" and viewer is None:
        return [{"id": "join", "label": get_text("boss.join.button")}]
    if phase not in ("attack", "block") or viewer is None or not viewer["alive"]:
        return []
    return [
        {"id": zone, "label": get_text(f"boss.keyboards.{phase}.{zone}")}
        for zone in duel.BOSS_ZONES
    ]


def _public_boss(boss: dict) -> dict:
    return {
        "id": duel._boss_catalog_id(boss),
        "name": boss["name"],
        "emoji": boss["emoji"],
        "description": boss["description"],
    }


def _battle_snapshot(battle: dict, viewer_user_id: int) -> dict:
    phase = battle["phase"]
    participants = battle["participants"]
    viewer = participants.get(viewer_user_id)
    choice_field = phase if phase in ("attack", "block") else None
    rows = []
    for user_id, participant in participants.items():
        rows.append({
            "user_id": user_id,
            "title": format_user_title_plain(participant["data"]),
            "alive": bool(participant["alive"]),
            "hits": participant["hits"],
            "choice_submitted": (
                participant.get(choice_field) is not None
                if choice_field and participant["alive"] else None
            ),
        })
    viewer_choice = (
        viewer.get(choice_field) is not None
        if viewer and viewer["alive"] and choice_field else None
    )
    viewer_selection = (
        viewer.get(choice_field)
        if viewer and viewer["alive"] and choice_field else None
    )
    return {
        "battle": {
            "battle_id": battle.get("battle_id"),
            "deadline_at": (int(battle["deadline_at"] * 1000)
                            if phase in ("join", "attack", "block")
                            and battle.get("deadline_at") is not None else None),
            "boss": _public_boss(battle["boss"]),
            "phase": phase,
            "round": battle["round"],
            "hits": battle["hits"],
            "required_hits": BOSS_REQUIRED_HITS,
            "participants_count": len(participants),
            "alive_count": sum(row["alive"] for row in rows),
            "participants": rows,
        },
        "registration": None,
        "recent_result": None,
        "available_actions": _available_actions(battle, viewer),
        "viewer": {
            "in_battle": viewer is not None,
            "alive": bool(viewer["alive"]) if viewer else None,
            "hits": viewer["hits"] if viewer else None,
            "choice_submitted": viewer_choice,
            "selected_action": viewer_selection,
            "can_act_in_telegram": (
                (viewer is None and phase == "join") or
                (viewer is not None and viewer["alive"] and choice_field is not None)
            ),
        },
    }


def _public_recent_result(result: dict, viewer_user_id: int) -> dict:
    participants = result["participants"]
    viewer = next((row for row in participants if row["user_id"] == viewer_user_id), None)
    hero = next(
        (row for row in participants if row["user_id"] == result["hero_user_id"]),
        None,
    )
    loot = result["item_loot"]
    narrative = result["narrative"]
    viewer_story = next(
        (row for row in (narrative["deaths"] + narrative["survivors"])
         if row["user_id"] == viewer_user_id),
        None,
    ) if narrative else None
    return {
        "battle_id": result["battle_id"],
        "boss": {"id": result["boss_id"], "name": result["boss_name"]},
        "finished_at": result["finished_at"],
        "outcome": result["outcome"],
        "hits": result["hits"],
        "required_hits": result["required_hits"],
        "rounds": result["rounds"],
        "participants": participants,
        "participants_count": len(participants),
        "alive_count": sum(row["alive"] for row in participants),
        "hero": {
            "user_id": hero["user_id"], "title": hero["title"],
            "hits": hero["hits"], "blocks": hero["blocks"],
            "rounds_survived": hero["rounds_survived"],
        } if hero else None,
        "item_loot": loot,
        "narrative": narrative,
        "viewer": {
            "participated": viewer is not None,
            "alive": viewer["alive"] if viewer else None,
            "hits": viewer["hits"] if viewer else None,
            "blocks": viewer["blocks"] if viewer else None,
            "rounds_survived": viewer["rounds_survived"] if viewer else None,
            "death_round": viewer["death_round"] if viewer else None,
            "chronicle": viewer_story,
            "rewarded": viewer_user_id in result["rewarded_user_ids"],
            "received_item": bool(loot and loot["recipient_user_id"] == viewer_user_id),
        },
    }


async def get_boss_battle_read_model(chat_id: int, viewer_user_id: int) -> dict:
    """Read the live shared battle without advancing timers or resolving rounds."""
    battle = duel.ACTIVE_BOSS_BATTLES.get(chat_id)
    if battle is not None:
        async with battle["lock"]:
            if duel.ACTIVE_BOSS_BATTLES.get(chat_id) is battle:
                return _battle_snapshot(battle, viewer_user_id)

    count, registered = boss_registration._boss_registration_snapshot(chat_id, viewer_user_id)
    result = get_latest_boss_result(chat_id)
    return {
        "battle": None,
        "recent_result": _public_recent_result(result, viewer_user_id) if result else None,
        "available_actions": [],
        "registration": {
            "open": boss_registration._boss_registration_is_open(),
            "participants_count": count,
            "viewer_registered": registered,
        },
        "viewer": {
            "in_battle": False, "alive": None, "hits": None,
            "choice_submitted": None, "selected_action": None,
            "can_act_in_telegram": False,
        },
    }
