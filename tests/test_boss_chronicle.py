"""The Mini App chronicle captures Telegram's one canonical final narration."""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import database
import gnome_avatars
import miniapp_api
from handlers import boss_presentation, boss_registration, duel
from handlers.boss_read_model import get_boss_battle_read_model
from handlers.boss_result_repository import (
    build_boss_result, get_latest_boss_result, save_boss_result,
    update_boss_result_narrative,
)
from miniapp_api import create_miniapp_api
from tests.test_boss_flow import make_participant
from tests.test_boss_recent_result import CHAT_A, CHAT_B, finished_battle
from tests.test_miniapp_api import session_for
from tests.test_miniapp_auth import TEST_BOT_TOKEN


@pytest.fixture
def chronicle_world(temp_database, monkeypatch):
    monkeypatch.setattr(boss_registration, "_BOSS_REG_DB_PATH", temp_database)
    duel.ACTIVE_BOSS_BATTLES.clear()
    yield temp_database
    duel.ACTIVE_BOSS_BATTLES.clear()


async def narrate(chat_id, battle, victory, monkeypatch):
    save_boss_result(build_boss_result(
        chat_id, battle, duel.BOSS_CATALOG_IDS[0], 5, victory=victory,
    ))
    # The item roll is tested separately; this test traces only the final report.
    monkeypatch.setattr(duel, "_maybe_award_boss_item", lambda *_: None)
    bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message=AsyncMock())
    await duel._boss_send_final_report(SimpleNamespace(bot=bot), chat_id, battle,
                                       victory=victory)
    return bot.edit_message_text.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_defeat_persists_exact_selected_epitaphs_verdict_and_viewer_story(
    chronicle_world, monkeypatch,
):
    first = make_participant(101, alive=False, hits=1, blocks=0)
    first.update(death_round=1, death_attack_zone="dick",
                 death_defended_zone="body", death_by_zone="dick")
    second = make_participant(202, alive=False, hits=2, blocks=1)
    second.update(death_round=3, death_attack_zone="head",
                  death_defended_zone="body", death_by_zone="head")
    battle = finished_battle("narrated-defeat", [first, second], hits=3)
    choices = iter((0, 2))
    chosen = []
    def choose(phrases):
        position = next(choices)
        chosen.append(position)
        return phrases[position]
    monkeypatch.setattr(boss_presentation.random, "choice", choose)

    telegram_text = await narrate(CHAT_A, battle, False, monkeypatch)
    result = get_latest_boss_result(CHAT_A)
    story = result["narrative"]
    assert chosen == [0, 2]
    assert story["survivors"] == []
    assert [entry["user_id"] for entry in story["deaths"]] == [101, 202]
    assert story["deaths"][0]["round"] == 1
    assert story["deaths"][0]["attack_zone"] == {"id": "dick", "label": "Хуй"}
    assert story["deaths"][0]["defended_zone"] == {"id": "body", "label": "Торс"}
    assert story["deaths"][0]["boss_attack_zone"] == {"id": "dick", "label": "Хуй"}
    assert "Гномская бухгалтерия — ошибкой" in story["deaths"][0]["text"]
    assert "Памятник решили не ставить" in story["deaths"][1]["text"]
    assert "Гномская бухгалтерия — ошибкой" in telegram_text
    assert "Памятник решили не ставить" in telegram_text
    assert story["featured"]["role"] == "last_gnome"
    assert story["featured"]["user_id"] == 202
    assert story["verdict_kind"] == "verdict"
    assert story["verdict"] == "📜 Вердикт: гномы были храбрыми. Но босс был охуенно внимательным."
    assert "<b>Вердикт:</b> гномы были храбрыми. Но босс был охуенно внимательным." in telegram_text
    update_boss_result_narrative(CHAT_A, battle["battle_id"], {"verdict": "rerolled"})
    assert get_latest_boss_result(CHAT_A)["narrative"] == story
    with sqlite3.connect(chronicle_world) as conn:
        assert conn.execute("SELECT count(*) FROM boss_battle_results").fetchone() == (1,)

    def no_rng(*args, **kwargs):
        raise AssertionError("reading or rendering a persisted chronicle rerolled")
    monkeypatch.setattr(boss_presentation.random, "choice", no_rng)
    monkeypatch.setattr(duel.random, "random", no_rng)
    monkeypatch.setattr(gnome_avatars.secrets, "choice", no_rng)
    monkeypatch.setattr(miniapp_api, "get_or_assign_gnome_variant", no_rng)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        viewer = await session_for(client, CHAT_A, 101)
        other_chat = await session_for(client, CHAT_B, 101)
        before = chronicle_world.read_bytes()
        for _ in range(10):
            response = await client.get(f"/api/v1/boss?chat_id={CHAT_B}",
                                        headers={**viewer, "X-Chat-Id": str(CHAT_B)})
            assert response.status_code == 200
            recent = response.json()["recent_result"]
            assert recent["narrative"] == story
            assert recent["viewer"]["chronicle"] == story["deaths"][0]
            assert recent["viewer"]["death_round"] == 1
            assert response.json()["registration"] is not None
        assert (await client.get("/api/v1/boss", headers=other_chat)).json()[
            "recent_result"] is None
        assert chronicle_world.read_bytes() == before


@pytest.mark.asyncio
async def test_victory_captures_survivor_phrase_death_and_same_decree(
    chronicle_world, monkeypatch,
):
    survivor = make_participant(101, hits=4, blocks=2, rounds_survived=3)
    fallen = make_participant(202, alive=False, hits=1)
    fallen.update(death_round=2, death_attack_zone="head",
                  death_defended_zone="body", death_by_zone="head")
    battle = finished_battle("narrated-victory", [survivor, fallen])
    choose = Mock(side_effect=lambda phrases: phrases[3])
    monkeypatch.setattr(boss_presentation.random, "choice", choose)

    telegram_text = await narrate(CHAT_A, battle, True, monkeypatch)
    story = get_latest_boss_result(CHAT_A)["narrative"]
    choose.assert_called_once()
    assert len(story["survivors"]) == 1
    assert story["survivors"][0]["user_id"] == 101
    assert "терминатор" in story["survivors"][0]["text"]
    assert len(story["deaths"]) == 1
    assert "Гном просчитался. Босс — нет." in story["deaths"][0]["text"]
    assert "Гном просчитался. Босс — нет." in telegram_text
    assert story["featured"]["role"] == "victory_hero"
    assert story["featured"]["user_id"] == 101
    assert story["verdict_kind"] == "decree"
    assert "сегодня эти гномы официально слишком охуенны" in story["verdict"]
    assert "сегодня эти гномы официально слишком охуенны" in telegram_text
    own = (await get_boss_battle_read_model(CHAT_A, 101))["recent_result"]["viewer"]
    assert own["chronicle"] == story["survivors"][0]


def test_stage_5b1_rows_migrate_without_inventing_chronicle(tmp_path, monkeypatch):
    old_db = tmp_path / "old_boss_results.db"
    with sqlite3.connect(old_db) as conn:
        conn.execute("""CREATE TABLE boss_battle_results (
            chat_id INTEGER NOT NULL, battle_id TEXT NOT NULL, boss_id TEXT NOT NULL,
            boss_name TEXT NOT NULL, finished_at INTEGER NOT NULL,
            outcome TEXT NOT NULL, hits INTEGER NOT NULL,
            required_hits INTEGER NOT NULL, rounds INTEGER NOT NULL,
            participants_json TEXT NOT NULL, hero_user_id INTEGER,
            rewarded_user_ids_json TEXT NOT NULL DEFAULT '[]', item_loot_json TEXT,
            PRIMARY KEY (chat_id, battle_id))""")
        conn.execute("""INSERT INTO boss_battle_results (
            chat_id, battle_id, boss_id, boss_name, finished_at, outcome,
            hits, required_hits, rounds, participants_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (CHAT_A, "legacy", "deep_snouted_baron", "Old boss", 1,
             "defeat", 3, 5, 2, json.dumps([])),
        )
    monkeypatch.setattr(database, "DB_NAME", old_db)
    database.init_db()
    database.init_db()
    result = get_latest_boss_result(CHAT_A)
    assert result["battle_id"] == "legacy"
    assert result["narrative"] is None
    with sqlite3.connect(old_db) as conn:
        assert conn.execute("SELECT count(*) FROM boss_battle_results").fetchone() == (1,)
        assert conn.execute("SELECT narrative_json FROM boss_battle_results").fetchone() == (None,)
