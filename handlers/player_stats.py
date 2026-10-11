"""Read-only, chat-scoped player statistics shared by Telegram and Mini App."""

from config import MAX_DAILY_POINTS
from database import format_user_title, get_bosses_defeated, has_huecrab, get_duel_equipment
from html import escape
from handlers.duel_items import get_duel_display_inventory_rows, format_duel_display_inventory
from handlers.duel_service import get_duel_profile
from handlers.duel_text import get_duel_title_read_model
from text_resources import get_text, get_text_mapping


EQUIPMENT_SLOT_ORDER = ("head", "clothing", "groin", "weapon", "outerwear", "footwear", "accessory", "accessory_2")


def _equipment_read_model(chat_id: int, user_id: int) -> dict:
    equipped = get_duel_equipment(chat_id, user_id)
    labels = get_text_mapping("duel.equipment.slots")
    defaults = get_text_mapping("duel.inventory.base_items")
    result = {}
    for slot in EQUIPMENT_SLOT_ORDER:
        item = equipped.get(slot)
        if item:
            result[slot] = {"label": labels[slot], "name": item["name"],
                            "item_id": item["item_id"], "inventory_id": item["inventory_id"],
                            "generated": True, "protective": slot in ("clothing", "head", "groin")}
        else:
            default_id = "knife" if slot == "weapon" else "oiled_vest" if slot == "outerwear" else None
            result[slot] = {"label": labels[slot],
                            "name": defaults[default_id] if default_id else get_text("duel.equipment.empty"),
                            "item_id": default_id, "inventory_id": None,
                            "generated": False, "protective": False}
    return result


def player_stats_read_model(chat_id: int, user_id: int) -> dict | None:
    profile = get_duel_profile(chat_id, user_id, read_only=True)
    if profile is None:
        return None
    user = profile.user
    has_dick = not user["dick_stolen_today"]
    equipment = _equipment_read_model(chat_id, user_id)
    equipped_ids = {slot["inventory_id"] for slot in equipment.values()
                    if slot["inventory_id"] is not None}
    return {
        "user_id": user["user_id"], "username": user["username"],
        "display_name": user["display_name"], "dwarf_name": user["dwarf_name"],
        "points": user["points"], "max_points": MAX_DAILY_POINTS,
        "wins": user["wins"], "losses": user["losses"],
        "daily_wins": user["daily_wins"],
        "dick_stolen_today": user["dick_stolen_today"],
        "dick_status": {
            "has_dick": has_dick,
            "text": get_text("duel.stats.status.has_dick" if has_dick
                             else "duel.stats.status.no_dick"),
        },
        "titles": get_duel_title_read_model(user),
        "boss_wins": get_bosses_defeated(user_id, chat_id),
        "ineligibility": profile.ineligibility,
        "inventory": get_duel_display_inventory_rows(
            profile.inventory, equipped_ids, include_base=False),
        "equipment": equipment,
        "pet": (get_text("huecrab.inventory") if has_huecrab(chat_id, user_id) else None),
        "telegram_title": format_user_title(user),
        "telegram_inventory": (format_duel_display_inventory(
            profile.inventory, equipped_ids, include_base=False)
            or get_text("duel.inventory.empty")),
        "telegram_equipment": "\n".join([
            get_text("duel.equipment.title"),
            *(get_text("duel.equipment.slot", label=escape(equipment[slot]["label"]),
                       item=escape(equipment[slot]["name"])) for slot in EQUIPMENT_SLOT_ORDER),
        ]),
    }


def public_player_stats(model: dict) -> dict:
    """Exclude Telegram HTML presentation from JSON responses."""
    public = {key: value for key, value in model.items()
              if key not in ("telegram_title", "telegram_inventory", "telegram_equipment")}
    if "equipment" in model:
        public["equipment"] = {
            slot: {key: value for key, value in item.items() if key != "inventory_id"}
            for slot, item in model["equipment"].items()
        }
    return public


def format_player_stats_telegram(model: dict, *, inspected: bool = False) -> str:
    titles = [
        get_text("duel.stats.title_item", title=item["text"], count=item["count"])
        for item in model["titles"].values() if item["text"]
    ]
    text = get_text(
        "duel.inspect.summary" if inspected else "duel.stats.summary",
        title=model["telegram_title"], points=model["points"],
        max_points=model["max_points"], wins=model["wins"],
        losses=model["losses"],
        huyanie_text="\n".join(titles) if titles else get_text("duel.stats.no_titles"),
        bosses_defeated=model["boss_wins"], status=model["dick_status"]["text"],
    )
    if model.get("telegram_equipment"):
        text += "\n\n" + model["telegram_equipment"]
    text += "\n\n" + (model["pet"] or get_text("huecrab.inventory_empty"))
    text += "\n\n" + get_text("duel.inventory.line", items=model["telegram_inventory"])
    return text
