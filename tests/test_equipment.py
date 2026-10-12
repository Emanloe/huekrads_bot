"""Generated equipment uses owned inventory instances and protects only real hits."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import database as db
from handlers.player_stats import player_stats_read_model, format_player_stats_telegram


CHAT = -6610


def register(user_id=1, chat_id=CHAT):
    db.get_or_create_duel_user(SimpleNamespace(
        id=user_id, username=f"player{user_id}", first_name=f"Player {user_id}",
        last_name=None, is_bot=False,
    ), chat_id)


def award(item_id, name, kind, *, base_form="m", user_id=1, chat_id=CHAT):
    event_id = db.create_duel_item_event(chat_id)
    assert event_id is not None
    assert db.set_duel_item_event_message(event_id, event_id + 1000, "Loot")
    selected = Mock(return_value=(item_id, name, kind, base_form))
    status, instance = db.claim_duel_item_event(event_id, chat_id, user_id, selected)
    assert status == "claimed"
    selected.assert_called_once_with()
    return instance


@pytest.mark.parametrize("kind,slot", [
    ("оружие", "weapon"), ("верхняя одежда", "outerwear"),
    ("одежда", "clothing"), ("головной убор", "head"),
    ("пах", "groin"), ("обувь", "footwear"), ("аксессуар", "accessory"),
])
def test_new_generated_kind_equips_one_owned_instance(temp_database, kind, slot):
    register()
    instance = award("generated_piece", "Тестовая вещь", kind)
    assert db.get_duel_equipment(CHAT, 1)[slot] == {
        "inventory_id": instance["id"], "item_id": "generated_piece", "name": "Тестовая вещь",
    }
    assert len(db.get_duel_inventory(CHAT, 1)) == 1


def test_unknown_legacy_and_static_items_do_not_equip(temp_database):
    register()
    award("unknown_piece", "Неизвестная", "неизвестный kind")
    with db.get_db() as conn:
        conn.execute("INSERT INTO generated_item_names (item_id, name) VALUES (?, ?)",
                     ("legacy_piece", "Старый предмет"))
        conn.execute("INSERT INTO generated_item_names (item_id, name, kind) VALUES (?, ?, ?)",
                     ("knife", "Чужое имя", "оружие"))
    db.add_duel_inventory_item(CHAT, 1, "legacy_piece")
    db.add_duel_inventory_item(CHAT, 1, "knife")
    db.add_duel_inventory_item(CHAT, 1, "oiled_vest")
    db.add_duel_inventory_item(CHAT, 1, "ceremonial_bolt")
    assert db.get_duel_equipment(CHAT, 1) == {}


def test_additive_kind_and_equipment_migration_preserves_old_names(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE generated_item_names (item_id TEXT PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute("INSERT INTO generated_item_names VALUES ('old_generated', 'Старое имя')")
    monkeypatch.setattr(db, "DB_NAME", str(path))
    db.init_db()
    db.init_db()
    with db.get_db() as conn:
        assert conn.execute(
            "SELECT name, kind, base_form FROM generated_item_names WHERE item_id = 'old_generated'"
        ).fetchone() == ("Старое имя", None, None)
        assert conn.execute(
            "SELECT COUNT(*) FROM duel_equipment"
        ).fetchone()[0] == 0


def test_base_form_migration_from_existing_kind_schema(tmp_path, monkeypatch):
    path = tmp_path / "kind.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE generated_item_names (
                        item_id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT)""")
        conn.execute("INSERT INTO generated_item_names VALUES (?, ?, ?)",
                     ("old_hat", "Старая шляпа", "головной убор"))
    monkeypatch.setattr(db, "DB_NAME", str(path))
    db.init_db()
    with db.get_db() as conn:
        assert conn.execute("SELECT name, kind, base_form FROM generated_item_names"
                            ).fetchone() == ("Старая шляпа", "головной убор", None)


def test_six_slot_table_migrates_without_changing_existing_equipment(temp_database):
    register()
    hat = award("old_hat_for_schema", "Шляпа", "головной убор")
    with db.get_db() as conn:
        conn.execute("DROP TABLE duel_equipment")
        conn.execute("""CREATE TABLE duel_equipment (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            slot TEXT NOT NULL CHECK (slot IN
                ('weapon', 'outerwear', 'clothing', 'head', 'groin', 'footwear')),
            inventory_id INTEGER NOT NULL UNIQUE,
            PRIMARY KEY (chat_id, user_id, slot))""")
        conn.execute("INSERT INTO duel_equipment VALUES (?, ?, ?, ?)",
                     (CHAT, 1, "head", hat["id"]))
    db.init_db()
    db.init_db()
    assert db.get_duel_equipment(CHAT, 1)["head"]["inventory_id"] == hat["id"]
    ring = award("new_ring_for_schema", "Кольцо", "аксессуар")
    assert db.get_duel_equipment(CHAT, 1)["accessory"]["inventory_id"] == ring["id"]
    assert db.remove_duel_inventory_instance(CHAT, 1, ring["id"])
    assert "accessory" not in db.get_duel_equipment(CHAT, 1)
    assert db.get_duel_equipment(CHAT, 1)["head"]["inventory_id"] == hat["id"]


def test_seven_slot_table_migrates_existing_accessory_once(temp_database):
    register()
    ring = award("legacy_worn_ring", "Старое кольцо", "аксессуар")
    with db.get_db() as conn:
        marker_before = conn.execute(
            "SELECT applied_at FROM generated_equipment_migrations WHERE migration_key = ?",
            ("legacy_generated_equipment_v1",),
        ).fetchone()
        conn.execute("DROP TABLE duel_equipment")
        conn.execute("""CREATE TABLE duel_equipment (
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            slot TEXT NOT NULL CHECK (slot IN
                ('weapon', 'outerwear', 'clothing', 'head', 'groin', 'footwear', 'accessory')),
            inventory_id INTEGER NOT NULL UNIQUE,
            PRIMARY KEY (chat_id, user_id, slot))""")
        conn.execute("INSERT INTO duel_equipment VALUES (?, ?, ?, ?)",
                     (CHAT, 1, "accessory", ring["id"]))
    db.init_db()
    db.init_db()
    assert db.get_duel_equipment(CHAT, 1)["accessory"]["inventory_id"] == ring["id"]
    assert "accessory_2" not in db.get_duel_equipment(CHAT, 1)
    with db.get_db() as conn:
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'duel_equipment'").fetchone()[0]
        assert "'accessory_2'" in schema
        assert conn.execute("""SELECT applied_at FROM generated_equipment_migrations
                               WHERE migration_key = ?""",
                            ("legacy_generated_equipment_v1",)).fetchone() == marker_before
        assert conn.execute("""SELECT COUNT(*) FROM sqlite_master
                               WHERE type = 'trigger' AND name = 'trg_duel_inventory_equipment_delete'""").fetchone()[0] == 1
    second = award("second_worn_ring", "Новое кольцо", "аксессуар")
    assert db.get_duel_equipment(CHAT, 1)["accessory_2"]["inventory_id"] == second["id"]
    assert db.remove_duel_inventory_instance(CHAT, 1, second["id"])
    assert "accessory_2" not in db.get_duel_equipment(CHAT, 1)


def test_accessory_move_between_slots_is_atomic_and_unique(temp_database):
    register()
    first = award("move_first", "Первое кольцо", "аксессуар")
    second = award("move_second", "Второе кольцо", "аксессуар")
    assert db.change_generated_equipment(CHAT, 1, "accessory_2", first["id"]) == "equipped"
    assert db.get_duel_equipment(CHAT, 1) == {
        "accessory_2": {"inventory_id": first["id"], "item_id": "move_first", "name": "Первое кольцо"},
    }
    assert db.change_generated_equipment(CHAT, 1, "accessory", first["id"]) == "equipped"
    assert db.change_generated_equipment(CHAT, 1, "accessory_2", second["id"]) == "equipped"
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM duel_equipment WHERE inventory_id = ?",
                            (first["id"],)).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO duel_equipment VALUES (?, ?, ?, ?)",
                         (CHAT, 1, "head", first["id"]))
        conn.execute("""CREATE TRIGGER reject_accessory_move BEFORE INSERT ON duel_equipment
                        WHEN NEW.slot = 'accessory_2'
                        BEGIN SELECT RAISE(ABORT, 'move rejected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="move rejected"):
        db.change_generated_equipment(CHAT, 1, "accessory_2", first["id"])
    assert db.get_duel_equipment(CHAT, 1)["accessory"]["inventory_id"] == first["id"]
    assert db.get_duel_equipment(CHAT, 1)["accessory_2"]["inventory_id"] == second["id"]


def test_unpublished_second_accessory_drop_restores_its_slot(temp_database):
    register()
    award("first_drop_ring", "Первое кольцо", "аксессуар")
    second = award("second_drop_ring", "Второе кольцо", "аксессуар")
    drop = db.create_duel_item_event_from_inventory(CHAT, 1, second["id"])
    assert drop["equipped_slot"] == "accessory_2"
    assert "accessory_2" not in db.get_duel_equipment(CHAT, 1)
    assert db.restore_unpublished_duel_drop(drop)
    assert db.get_duel_equipment(CHAT, 1)["accessory_2"]["inventory_id"] == second["id"]


@pytest.mark.parametrize("base_form,name,expected", [
    ("m", "Ебейший железный шлем дракона", "остановил удар и порвался"),
    ("f", "Поганая меховая шапка дракона", "остановила удар и порвалась"),
    ("n", "Необычное канотье дракона", "остановило удар и порвалось"),
    ("pl", "Волшебные наголовники дракона", "остановили удар и порвались"),
])
def test_combat_form_comes_from_persisted_base_not_name_ending(
    temp_database, base_form, name, expected,
):
    from handlers.duel_text import get_equipment_break_text

    register()
    award("generated_guard", name, "головной убор", base_form=base_form)
    db.init_db()  # Idempotent restart migration must preserve grammatical metadata.
    consumed = db.consume_equipped_item_for_hit(CHAT, 1, "head")
    assert consumed["base_form"] == base_form
    assert get_equipment_break_text(consumed) == f"{name} {expected}"


def test_unknown_legacy_base_form_uses_neutral_combat_text(temp_database):
    from handlers.duel_text import get_equipment_break_text

    register()
    award("legacy_hat", "Шапка", "головной убор", base_form=None)
    consumed = db.consume_equipped_item_for_hit(CHAT, 1, "head")
    assert consumed["base_form"] is None
    assert get_equipment_break_text(consumed) == "Удар остановлен. Предмет уничтожен: Шапка"


def test_replacement_stack_and_no_re_equip_after_break(temp_database):
    register()
    old = award("old_clothing", "Старая рубаха", "одежда")
    first = award("new_clothing", "Новая рубаха", "одежда")
    second = award("new_clothing", "Новая рубаха", "одежда")
    assert db.get_duel_equipment(CHAT, 1)["clothing"]["inventory_id"] == second["id"]
    assert {item["id"] for item in db.get_duel_inventory(CHAT, 1)} == {
        old["id"], first["id"], second["id"],
    }
    assert db.consume_equipped_item_for_hit(CHAT, 1, "body")["inventory_id"] == second["id"]
    assert "clothing" not in db.get_duel_equipment(CHAT, 1)
    assert {item["id"] for item in db.get_duel_inventory(CHAT, 1)} == {old["id"], first["id"]}
    assert db.consume_equipped_item_for_hit(CHAT, 1, "body") is None


@pytest.mark.parametrize("kind,zone,wrong_zone", [
    ("одежда", "body", "head"),
    ("головной убор", "head", "dick"),
    ("пах", "dick", "body"),
])
def test_protection_matches_only_its_hit_zone(temp_database, kind, zone, wrong_zone):
    register()
    instance = award("guard_piece", "Щит <&>", kind)
    assert db.consume_equipped_item_for_hit(CHAT, 1, wrong_zone) is None
    assert db.get_duel_inventory(CHAT, 1)[0]["id"] == instance["id"]
    assert db.consume_equipped_item_for_hit(CHAT, 1, zone)["name"] == "Щит <&>"
    assert db.get_duel_inventory(CHAT, 1) == []


@pytest.mark.parametrize("kind", ["оружие", "верхняя одежда", "обувь", "аксессуар"])
def test_nonprotective_slots_never_absorb(temp_database, kind):
    register()
    award("cosmetic", "Украшение", kind)
    for zone in ("head", "body", "dick"):
        assert db.consume_equipped_item_for_hit(CHAT, 1, zone) is None
    assert len(db.get_duel_inventory(CHAT, 1)) == 1


def test_new_accessory_replaces_worn_instance_without_changing_inventory(temp_database):
    register()
    first = award("first_ring", "Первое кольцо", "аксессуар")
    assert db.get_duel_equipment(CHAT, 1)["accessory"]["inventory_id"] == first["id"]
    assert "accessory_2" not in db.get_duel_equipment(CHAT, 1)
    second = award("second_ring", "Второе кольцо", "аксессуар")
    assert db.get_duel_equipment(CHAT, 1)["accessory"]["inventory_id"] == first["id"]
    assert db.get_duel_equipment(CHAT, 1)["accessory_2"]["inventory_id"] == second["id"]
    third = award("third_ring", "Третье кольцо", "аксессуар")
    assert db.get_duel_equipment(CHAT, 1)["accessory"]["inventory_id"] == third["id"]
    assert db.get_duel_equipment(CHAT, 1)["accessory_2"]["inventory_id"] == second["id"]
    assert db.change_generated_equipment(CHAT, 1, "accessory") == "unequipped"
    fourth = award("fourth_ring", "Четвёртое кольцо", "аксессуар")
    assert db.get_duel_equipment(CHAT, 1)["accessory"]["inventory_id"] == fourth["id"]
    assert db.get_duel_equipment(CHAT, 1)["accessory_2"]["inventory_id"] == second["id"]
    assert len(db.get_duel_inventory(CHAT, 1)) == 4
    db.init_db()
    assert db.get_duel_equipment(CHAT, 1)["accessory"]["inventory_id"] == fourth["id"]
    assert db.get_duel_equipment(CHAT, 1)["accessory_2"]["inventory_id"] == second["id"]
    assert {row["id"] for row in db.get_duel_inventory(CHAT, 1)} == {
        first["id"], second["id"], third["id"], fourth["id"],
    }
    for zone in ("head", "body", "dick"):
        assert db.consume_equipped_item_for_hit(CHAT, 1, zone) is None
    assert db.get_duel_equipment(CHAT, 1)["accessory"]["inventory_id"] == fourth["id"]
    assert db.get_duel_equipment(CHAT, 1)["accessory_2"]["inventory_id"] == second["id"]


def test_atomic_consume_race_and_deletion_cleanup(temp_database):
    register()
    award("helmet", "Шлем", "головной убор")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: db.consume_equipped_item_for_hit(CHAT, 1, "head"), range(2)))
    assert sum(item is not None for item in outcomes) == 1
    assert db.get_duel_equipment(CHAT, 1) == {}
    assert db.get_duel_inventory(CHAT, 1) == []
    award("boots", "Башмаки", "обувь")
    assert db.delete_gnome(1, "player1")
    assert db.get_duel_equipment(CHAT, 1) == {}
    assert db.return_gnome("player1", verified_user_id=1) == "returned"
    register()
    assert db.get_duel_equipment(CHAT, 1) == {}


def test_failed_inventory_delete_rolls_back_equipment_consume(temp_database):
    register()
    item = award("helmet", "Шлем", "головной убор")
    with db.get_db() as conn:
        conn.execute("""CREATE TRIGGER reject_armor_delete BEFORE DELETE ON duel_inventory
                        BEGIN SELECT RAISE(ABORT, 'blocked'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="blocked"):
        db.consume_equipped_item_for_hit(CHAT, 1, "head")
    assert db.get_duel_equipment(CHAT, 1)["head"]["inventory_id"] == item["id"]
    assert db.get_duel_inventory(CHAT, 1)[0]["id"] == item["id"]


def test_transfer_and_drop_clear_old_reference_and_award_new_owner(temp_database):
    register(1)
    register(2)
    item = award("sword", "Меч", "оружие")
    assert db.transfer_duel_inventory_item(CHAT, 1, 2, item["id"])
    assert db.get_duel_equipment(CHAT, 1) == {}
    receiver = db.get_duel_equipment(CHAT, 2)["weapon"]
    assert receiver["item_id"] == "sword"
    drop = db.create_duel_item_event_from_inventory(CHAT, 2, receiver["inventory_id"])
    assert drop and db.get_duel_equipment(CHAT, 2) == {}
    assert db.set_duel_item_event_message(drop["event_id"], 4510, "Dropped")
    status, new_item = db.claim_duel_item_event(
        drop["event_id"], CHAT, 1, Mock(side_effect=AssertionError("rerolled")),
    )
    assert status == "claimed"
    assert db.get_duel_equipment(CHAT, 1)["weapon"]["inventory_id"] == new_item["id"]


def test_unpublished_equipped_drop_restores_slot_without_crossing_profile_reset(temp_database):
    register()
    item = award("hat", "Шляпа", "головной убор")
    drop = db.create_duel_item_event_from_inventory(CHAT, 1, item["id"])
    assert drop["equipped_slot"] == "head"
    assert db.get_duel_equipment(CHAT, 1) == {}
    assert db.restore_unpublished_duel_drop(drop)
    assert db.get_duel_equipment(CHAT, 1)["head"]["inventory_id"] == item["id"]

    later_drop = db.create_duel_item_event_from_inventory(CHAT, 1, item["id"])
    assert db.delete_gnome(1, "player1")
    assert db.return_gnome("player1", verified_user_id=1) == "returned"
    register()
    assert not db.restore_unpublished_duel_drop(later_drop)
    assert db.get_duel_equipment(CHAT, 1) == {}


def test_dig_pickup_uses_recorded_kind_without_reroll(temp_database):
    register()
    with db.get_db() as conn:
        conn.execute("UPDATE duel_users SET points = 50 WHERE chat_id = ? AND user_id = 1", (CHAT,))
    status, dig = db.try_duel_dig(
        CHAT, 1, lambda: 0.0, Mock(return_value=("dig_boots", "Сапоги", "обувь")),
    )
    assert status == "found" and db.get_duel_equipment(CHAT, 1) == {}
    assert db.set_duel_item_event_message(dig["event_id"], 912, "Dig")
    no_reroll = Mock(side_effect=AssertionError("fixed dig item rerolled"))
    status, item = db.claim_duel_item_event(dig["event_id"], CHAT, 1, no_reroll)
    assert status == "claimed" and item["item_id"] == "dig_boots"
    no_reroll.assert_not_called()
    assert db.get_duel_equipment(CHAT, 1)["footwear"]["inventory_id"] == item["id"]


def test_dig_pickup_equips_accessory_only_after_claim(temp_database):
    register()
    with db.get_db() as conn:
        conn.execute("UPDATE duel_users SET points = 50 WHERE chat_id = ? AND user_id = 1", (CHAT,))
    status, dig = db.try_duel_dig(CHAT, 1, lambda: 0.0,
                                  Mock(return_value=("dig_ring", "Кольцо", "аксессуар")))
    assert status == "found" and db.get_duel_equipment(CHAT, 1) == {}
    assert db.set_duel_item_event_message(dig["event_id"], 915, "Dig")
    status, instance = db.claim_duel_item_event(dig["event_id"], CHAT, 1,
                                                  Mock(side_effect=AssertionError("rerolled")))
    assert status == "claimed"
    assert db.get_duel_equipment(CHAT, 1)["accessory"]["inventory_id"] == instance["id"]


@pytest.mark.parametrize("kind,slot", [
    ("головной убор", "head"),
    ("аксессуар", "accessory"),
])
def test_huecrab_autoloot_auto_equips_without_pet_loss(temp_database, kind, slot):
    register(1)
    register(2)
    with db.get_db() as conn:
        conn.executemany("INSERT INTO huecrab_owners (chat_id, user_id) VALUES (?, ?)",
                         [(CHAT, user_id) for user_id in (1, 2)])
    event_id = db.create_duel_item_event(CHAT)
    assert db.set_duel_item_event_message(event_id, 913, "Crab")
    with db.get_db() as conn:
        conn.execute("UPDATE duel_item_events SET published_at = 1000 WHERE event_id = ?", (event_id,))
    status, claim = db.claim_due_item_for_huecrab(
        event_id, 1020, 20,
        Mock(return_value=("crab_item", "Крабья вещь", kind)),
        lambda candidates: candidates[0],
    )
    assert status == "claimed"
    winner_id = claim["owner"]["user_id"]
    assert winner_id == 1
    assert db.get_duel_equipment(CHAT, winner_id)[slot]["inventory_id"] == claim["id"]
    assert db.has_huecrab(CHAT, 1) and db.has_huecrab(CHAT, 2)


def test_effective_defaults_overrides_and_telegram_inventory(temp_database):
    register()
    model = player_stats_read_model(CHAT, 1)
    assert model["equipment"]["weapon"]["name"] == "Нож"
    assert model["equipment"]["outerwear"]["name"] == "Промасленная жилетка"
    assert model["inventory"] == []
    assert model["telegram_inventory"] == "пусто"
    assert model["equipment"]["clothing"]["label"] == "👕 Торс"
    assert [model["equipment"][slot]["label"] for slot in (
        "weapon", "outerwear", "head", "groin", "footwear")] == [
        "⚔️ Оружие", "🧥 Верхняя одежда", "🎩 Головной убор", "🍆 Пах", "👞 Обувь"]
    assert all(model["equipment"][slot]["item_id"] is None
               for slot in ("clothing", "head", "groin", "footwear", "accessory", "accessory_2"))
    assert model["equipment"]["accessory"]["label"] == "💍 Аксессуар 1"
    assert model["equipment"]["accessory_2"]["label"] == "💍 Аксессуар 2"
    award("sword", "Меч & щит", "оружие")
    award("cloak", "Плащ", "верхняя одежда")
    model = player_stats_read_model(CHAT, 1)
    assert model["equipment"]["weapon"]["name"] == "Меч & щит"
    assert model["equipment"]["outerwear"]["name"] == "Плащ"
    assert model["equipment"]["weapon"]["generated"] is True
    telegram = format_player_stats_telegram(model)
    assert "Меч &amp; щит" in telegram
    assert "👕 Торс: Пусто" in telegram
    assert "💍 Аксессуар 1: Пусто" in telegram
    assert "💍 Аксессуар 2: Пусто" in telegram
    assert "👕 Одежда" not in telegram
    assert "<b>Инвентарь:</b> Промасленная жилетка" not in telegram
    assert "Нож" not in telegram.split("<b>Инвентарь:</b>", 1)[1]
    assert next(item for item in model["inventory"] if item["item_id"] == "sword")["equipped_count"] == 1
    assert {item["item_id"] for item in model["inventory"]} == {"sword", "cloak"}
    assert db.remove_duel_inventory_instance(CHAT, 1, model["equipment"]["weapon"]["inventory_id"])
    assert player_stats_read_model(CHAT, 1)["equipment"]["weapon"]["name"] == "Нож"
    cloak_id = model["equipment"]["outerwear"]["inventory_id"]
    assert db.remove_duel_inventory_instance(CHAT, 1, cloak_id)
    assert player_stats_read_model(CHAT, 1)["equipment"]["outerwear"]["name"] == "Промасленная жилетка"


@pytest.mark.asyncio
async def test_miniapp_profile_exposes_effective_equipment_and_owned_stack(temp_database):
    import httpx
    from miniapp_api import create_miniapp_api
    from tests.test_miniapp_api import session_for
    from tests.test_miniapp_auth import TEST_BOT_TOKEN

    register()
    award("relic_sword", "Именной меч", "оружие")
    award("relic_sword", "Именной меч", "оружие")
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        headers = await session_for(client, CHAT, 1)
        profile = (await client.get("/api/v1/me", headers=headers)).json()
    assert len(profile["equipment"]) == 8
    assert list(profile["equipment"]) == ["head", "clothing", "groin", "weapon", "outerwear", "footwear", "accessory", "accessory_2"]
    assert profile["equipment"]["weapon"]["name"] == "Именной меч"
    assert profile["equipment"]["outerwear"]["name"] == "Промасленная жилетка"
    assert profile["equipment"]["clothing"]["item_id"] is None
    assert profile["equipment"]["clothing"]["label"] == "👕 Торс"
    assert {item["item_id"] for item in profile["inventory"]} == {"relic_sword"}
    sword = next(item for item in profile["inventory"] if item["item_id"] == "relic_sword")
    assert sword["count"] == 2 and sword["equipped_count"] == 1


@pytest.mark.parametrize("kind,zone,block_zone,name,base_form,verb", [
    ("головной убор", "head", "body", "Поганая меховая шапка <&>", "f",
     "остановила удар и порвалась"),
    ("одежда", "body", "head", "Ебейший железный панцирь <&>", "m",
     "остановил удар и порвался"),
    ("пах", "dick", "head", "Сыромятное гульфище <&>", "n",
     "остановило удар и порвалось"),
])
def test_persistent_duel_hit_is_absorbed_without_changing_rng(
    temp_database, monkeypatch, kind, zone, block_zone, name, base_form, verb,
):
    from handlers import duel_service
    from tests.test_persistent_duel_transitions import (
        CHAT_A, PUBLISHED_AT, TraceRng, make_session,
    )

    register(1, CHAT_A)
    register(2, CHAT_A)
    award("armor", name, kind, base_form=base_form, user_id=2, chat_id=CHAT_A)
    session = make_session(
        CHAT_A, status="active", phase="block", attack_zone=zone,
        turn_id=2, round_no=3,
    )
    rng = TraceRng((0.5, 0.5))
    monkeypatch.setattr(duel_service, "random", rng)
    result = duel_service.submit_persistent_duel_block(
        CHAT_A, session["id"], 2, 2, block_zone, now_ms=PUBLISHED_AT + 1,
    )
    assert result.reason == "success"
    assert result.resolution["outcome"] == "absorbed"
    assert result.resolution["kind"] == "round_resolution"
    escaped_name = name.replace("<&>", "&lt;&amp;&gt;")
    assert f"{escaped_name} {verb}" in result.resolution["presentation_html"]
    assert db.get_duel_inventory(CHAT_A, 2) == []
    assert db.get_duel_equipment(CHAT_A, 2) == {}
    assert rng.trace == [
        ("random", 0.5), ("random", 0.5), ("choice", "hit"), ("choice", "attack"),
    ]
    retry = duel_service.submit_persistent_duel_block(
        CHAT_A, session["id"], 2, 2, block_zone, now_ms=PUBLISHED_AT + 2,
    )
    assert retry.reason == "stale_turn" and db.get_duel_inventory(CHAT_A, 2) == []
    assert len(rng.trace) == 4


def test_persistent_duel_miss_block_and_wrong_zone_preserve_equipment(temp_database, monkeypatch):
    from handlers import duel_service
    from tests.test_persistent_duel_transitions import CHAT_A, PUBLISHED_AT, TraceRng, make_session

    register(1, CHAT_A)
    register(2, CHAT_A)
    award("helmet", "Шлем", "головной убор", user_id=2, chat_id=CHAT_A)
    session = make_session(CHAT_A, status="active", phase="block", attack_zone="body", turn_id=2)
    monkeypatch.setattr(duel_service, "random", TraceRng((0.5, 0.5)))
    result = duel_service.submit_persistent_duel_block(
        CHAT_A, session["id"], 2, 2, "dick", now_ms=PUBLISHED_AT + 1,
    )
    assert result.resolution["outcome"] == "hit"
    assert db.get_duel_equipment(CHAT_A, 2)["head"]["item_id"] == "helmet"


@pytest.mark.parametrize("rolls,block_zone,outcome", [
    ((0.5, 0.0), "body", "miss"),
    ((0.5, 0.5), "head", "block"),
])
def test_persistent_duel_nonhit_does_not_break_armor(
    temp_database, monkeypatch, rolls, block_zone, outcome,
):
    from handlers import duel_service
    from tests.test_persistent_duel_transitions import CHAT_A, PUBLISHED_AT, TraceRng, make_session

    register(1, CHAT_A)
    register(2, CHAT_A)
    item = award("helmet", "Шлем", "головной убор", user_id=2, chat_id=CHAT_A)
    session = make_session(CHAT_A, status="active", phase="block", attack_zone="head", turn_id=2)
    monkeypatch.setattr(duel_service, "random", TraceRng(rolls))
    result = duel_service.submit_persistent_duel_block(
        CHAT_A, session["id"], 2, 2, block_zone, now_ms=PUBLISHED_AT + 1,
    )
    assert result.resolution["outcome"] == outcome
    assert db.get_duel_equipment(CHAT_A, 2)["head"]["inventory_id"] == item["id"]


@pytest.mark.asyncio
async def test_legacy_telegram_duel_absorption_keeps_fight_running(
    temp_database, monkeypatch, fake_context,
):
    from handlers import duel
    from tests.test_duel_flow import CHAT_ID, install_fake_tasks, make_block_phase_duel
    from tests.test_persistent_duel_transitions import TraceRng

    register(602, CHAT_ID)
    award("helmet", "Поганая меховая шапка", "головной убор",
          base_form="f", user_id=602, chat_id=CHAT_ID)
    state = make_block_phase_duel(strike_zone="head")
    duel.ACTIVE_DUELS[CHAT_ID] = state
    install_fake_tasks(monkeypatch, duel)
    finish = AsyncMock()
    monkeypatch.setattr(duel, "_finish_duel", finish)
    rng = TraceRng((0.5, 0.5))
    monkeypatch.setattr(duel, "random", rng)
    try:
        await duel._process_block_choice(fake_context, CHAT_ID, "body")
        finish.assert_not_awaited()
        assert state["phase"] == "attack" and state["round"] == 4
        assert "Поганая меховая шапка остановила удар и порвалась" in (
            fake_context.bot.edit_message_text.await_args.kwargs["text"])
        fake_context.bot.send_message.assert_not_awaited()
        assert db.get_duel_inventory(CHAT_ID, 602) == []
        assert rng.trace == [
            ("random", 0.5), ("random", 0.5), ("choice", "hit"), ("choice", "attack"),
        ]
    finally:
        duel.ACTIVE_DUELS.pop(CHAT_ID, None)


@pytest.mark.parametrize("kind,zone", [
    ("головной убор", "head"), ("одежда", "body"), ("пах", "dick"),
])
def test_boss_hit_absorbed_once_and_survivor_continues(temp_database, kind, zone):
    from handlers.boss_state import _apply_boss_round_result
    from tests.test_boss_state import make_round_battle, make_round_participant

    register()
    award("armor", "Броня", kind)
    participant = make_round_participant(
        attack="head", block="body" if zone != "body" else "head",
    )
    battle = make_round_battle({1: participant})
    battle["boss_attack"] = zone
    result = _apply_boss_round_result(
        battle, 5, absorb_hit=lambda _participant, attack: db.consume_equipped_item_for_hit(CHAT, 1, attack),
    )
    assert result["outcome"] == "continue"
    assert participant["alive"] and participant["rounds_survived"] == 1
    assert participant["blocks"] == 0
    assert result["round_results"][0]["absorbed_item"]["name"] == "Броня"
    assert db.get_duel_inventory(CHAT, 1) == []
    battle["round"] += 1
    second = _apply_boss_round_result(
        battle, 5, absorb_hit=lambda _participant, attack: db.consume_equipped_item_for_hit(CHAT, 1, attack),
    )
    assert second["outcome"] == "defeat"
    assert not participant["alive"] and participant["rounds_survived"] == 1


def test_boss_block_and_finishing_hit_do_not_consume_armor(temp_database):
    from handlers.boss_state import _apply_boss_round_result
    from tests.test_boss_state import make_round_battle, make_round_participant

    register()
    item = award("helmet", "Шлем", "головной убор")
    participant = make_round_participant(attack="head", block="head")
    battle = make_round_battle({1: participant})
    result = _apply_boss_round_result(
        battle, 5, absorb_hit=lambda _p, zone: db.consume_equipped_item_for_hit(CHAT, 1, zone),
    )
    assert result["round_results"][0]["survived"] and participant["blocks"] == 1
    assert db.get_duel_equipment(CHAT, 1)["head"]["inventory_id"] == item["id"]
    battle["hits"] = 4
    result = _apply_boss_round_result(
        battle, 5, absorb_hit=lambda _p, zone: db.consume_equipped_item_for_hit(CHAT, 1, zone),
    )
    assert result["victory"] and not result["round_results"][0]["boss_responded"]
    assert db.get_duel_equipment(CHAT, 1)["head"]["inventory_id"] == item["id"]


@pytest.mark.asyncio
async def test_boss_round_log_reports_broken_item_and_continues(
    temp_database, monkeypatch, fake_context,
):
    from handlers import duel
    from tests.test_boss_flow import make_battle, make_participant

    register()
    award("helmet", "Поганая меховая шапка <&>", "головной убор", base_form="f")
    participant = make_participant(1, attack="head", block="body")
    battle = make_battle([participant], phase="block", boss_attack="head", boss_block="body")
    duel.ACTIVE_BOSS_BATTLES[CHAT] = battle
    monkeypatch.setattr(duel, "_boss_start_round", AsyncMock())
    monkeypatch.setattr(duel, "_boss_finish_victory", AsyncMock())
    monkeypatch.setattr(duel, "_boss_finish_defeat", AsyncMock())
    monkeypatch.setattr(duel.asyncio, "sleep", AsyncMock())
    try:
        await duel._boss_resolve_round(fake_context, CHAT)
        assert participant["alive"]
        assert participant["rounds_survived"] == 1
        assert db.get_duel_inventory(CHAT, 1) == []
        log_text = fake_context.bot.send_message.await_args.kwargs["text"]
        assert "Поганая меховая шапка &lt;&amp;&gt; остановила удар и порвалась" in log_text
        assert "💀 УБИТ" not in log_text
        assert fake_context.bot.send_message.await_count == 1
    finally:
        duel.ACTIVE_BOSS_BATTLES.pop(CHAT, None)
