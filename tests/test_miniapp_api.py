"""HTTP sessions expose only their own chat world's read models."""

import time
import hashlib
from types import SimpleNamespace

import httpx
import pytest

import database
from duel_session_repository import create_duel_session, utc_unix_milliseconds
from handlers.duel_service import DUEL_PARTICIPANT_SNAPSHOT_FIELDS
from miniapp_api import create_miniapp_api
from miniapp_sessions import create_launch_token
from tests.test_miniapp_auth import TEST_BOT_TOKEN, signed_init_data


CHAT_A = -9101
CHAT_B = -9102


def register(chat_id, user_id, username):
    actor = SimpleNamespace(id=user_id, username=username, first_name=username,
                            last_name=None, is_bot=False)
    return database.get_or_create_duel_user(actor, chat_id)


def snapshot(user):
    return {key: user[key] for key in DUEL_PARTICIPANT_SNAPSHOT_FIELDS}


async def session_for(client, chat_id, user_id):
    launch = create_launch_token(chat_id, user_id)
    response = await client.post("/api/v1/session", json={
        "init_data": signed_init_data(user={"id": user_id}, auth_date=int(time.time())),
        "launch_token": launch,
    })
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"session_token", "expires_at"}
    return {"Authorization": f"Bearer {body['session_token']}"}


@pytest.mark.asyncio
async def test_read_api_isolates_same_user_in_two_chats_and_hides_block_zone(
    temp_database, monkeypatch,
):
    a1, a2 = register(CHAT_A, 101, "hero_a"), register(CHAT_A, 202, "opponent_a")
    b1, b3 = register(CHAT_B, 101, "hero_b"), register(CHAT_B, 303, "opponent_b")
    with database.get_db() as conn:
        conn.execute("UPDATE duel_users SET points = 25 WHERE chat_id = ? AND user_id = 101", (CHAT_A,))
        conn.execute("UPDATE duel_users SET points = 70 WHERE chat_id = ? AND user_id = 101", (CHAT_B,))
    database.add_duel_inventory_item(CHAT_A, 101, "rat_knuckle")
    database.add_duel_inventory_item(CHAT_B, 101, "vevangel_wing")
    deadline = utc_unix_milliseconds() + 100_000
    duel_a = create_duel_session(
        CHAT_A, 101, 202, snapshot(a1), snapshot(a2), 101, 202,
        status="active", phase="block", attack_zone="head", turn_id=2,
        deadline_at=deadline,
    )
    duel_b = create_duel_session(
        CHAT_B, 101, 303, snapshot(b1), snapshot(b3), 101, 303,
        status="active", phase="attack", deadline_at=deadline,
    )
    from handlers import duel_service
    monkeypatch.setattr(duel_service, "random", SimpleNamespace(
        random=lambda: (_ for _ in ()).throw(AssertionError("GET used game RNG")),
        choice=lambda _: (_ for _ in ()).throw(AssertionError("GET used game RNG")),
    ))
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        headers_a = await session_for(client, CHAT_A, 101)
        headers_b = await session_for(client, CHAT_B, 101)
        headers_defender = await session_for(client, CHAT_A, 202)
        extra = {"X-Chat-Id": str(CHAT_B), **headers_a}
        me_a = (await client.get(f"/api/v1/me?chat_id={CHAT_B}", headers=extra)).json()
        me_b = (await client.get("/api/v1/me", headers=headers_b)).json()
        assert (me_a["username"], me_a["points"]) == ("hero_a", 25)
        assert (me_b["username"], me_b["points"]) == ("hero_b", 70)
        assert me_a["inventory"] == [
            {"item_id": "oiled_vest", "name": "Промасленная жилетка", "count": 1},
            {"item_id": "knife", "name": "Нож", "count": 1},
            {"item_id": "rat_knuckle", "name": "Крысиный кастет", "count": 1},
        ]
        assert me_b["inventory"] == [
            {"item_id": "oiled_vest", "name": "Промасленная жилетка", "count": 1},
            {"item_id": "knife", "name": "Нож", "count": 1},
            {"item_id": "vevangel_wing", "name": "Крыло Вевангела", "count": 1},
        ]
        assert "chat_id" not in me_a
        opponents_a = (await client.get(f"/api/v1/duel/opponents?chat_id={CHAT_B}",
                                        headers=extra)).json()["opponents"]
        opponents_b = (await client.get("/api/v1/duel/opponents", headers=headers_b)).json()["opponents"]
        assert [item["user_id"] for item in opponents_a] == [202]
        assert [item["user_id"] for item in opponents_b] == [303]
        active_a = (await client.get(f"/api/v1/duel/active?chat_id={CHAT_B}",
                                     headers=extra)).json()["duel"]
        active_b = (await client.get("/api/v1/duel/active", headers=headers_b)).json()["duel"]
        active_defender = (await client.get("/api/v1/duel/active", headers=headers_defender)).json()["duel"]
        assert (active_a["id"], active_a["attack_zone"], active_a["role"]) == (duel_a["id"], "head", "attacker")
        assert active_b["id"] == duel_b["id"] and active_b["can_act"] is True
        assert active_defender["attack_zone"] is None and active_defender["role"] == "defender"
        assert "result" not in active_a and "outbox" not in active_a
        assert (await client.post("/api/v1/duel/active", headers=headers_a)).status_code == 405


@pytest.mark.asyncio
async def test_me_inventory_uses_catalog_names_counts_and_legacy_id_fallback(temp_database):
    register(CHAT_A, 101, "hero")
    database.add_duel_inventory_item(CHAT_A, 101, "ceremonial_bolt")
    database.add_duel_inventory_item(CHAT_A, 101, "ceremonial_bolt")
    database.add_duel_inventory_item(CHAT_A, 101, "cork_with_bite_marks")
    database.add_duel_inventory_item(CHAT_A, 101, "legacy_missing_id")
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        headers = await session_for(client, CHAT_A, 101)
        response = await client.get("/api/v1/me", headers=headers)
    assert response.status_code == 200
    assert response.json()["inventory"] == [
        {"item_id": "oiled_vest", "name": "Промасленная жилетка", "count": 1},
        {"item_id": "knife", "name": "Нож", "count": 1},
        {"item_id": "cork_with_bite_marks", "name": "Пробка со следами укусов", "count": 1},
        {"item_id": "ceremonial_bolt", "name": "Парадный болт", "count": 2},
        {"item_id": "legacy_missing_id", "name": "legacy_missing_id", "count": 1},
    ]


@pytest.mark.asyncio
async def test_http_opponents_apply_zero_point_rule_with_chat_isolation(temp_database, monkeypatch):
    register(CHAT_A, 101, "hero_a")
    register(CHAT_A, 202, "shared_opponent")
    register(CHAT_B, 101, "hero_b")
    register(CHAT_B, 202, "shared_opponent")
    register(CHAT_B, 303, "b_only")
    with database.get_db() as conn:
        conn.execute("UPDATE duel_users SET points = 0 WHERE user_id = 202")
        conn.execute("UPDATE duel_users SET dick_stolen_today = 1 "
                     "WHERE chat_id = ? AND user_id = 202", (CHAT_B,))
    from handlers import duel_service

    monkeypatch.setattr(duel_service.random, "random",
                        lambda: (_ for _ in ()).throw(AssertionError("GET used game RNG")))
    monkeypatch.setattr(duel_service.random, "choice",
                        lambda _: (_ for _ in ()).throw(AssertionError("GET used game RNG")))
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        headers_a = await session_for(client, CHAT_A, 101)
        headers_b = await session_for(client, CHAT_B, 101)
        a = (await client.get(f"/api/v1/duel/opponents?chat_id={CHAT_B}",
                              headers={**headers_a, "X-Chat-Id": str(CHAT_B)})).json()
        b = (await client.get(f"/api/v1/duel/opponents?chat_id={CHAT_A}",
                              headers={**headers_b, "X-Chat-Id": str(CHAT_A)})).json()
        assert [opponent["user_id"] for opponent in a["opponents"]] == [202]
        assert [opponent["user_id"] for opponent in b["opponents"]] == [303]


@pytest.mark.asyncio
async def test_auth_failures_extra_chat_id_and_gets_do_not_write_game_state(temp_database):
    register(CHAT_A, 101, "hero")
    register(CHAT_A, 202, "other")
    with database.get_db() as conn:
        conn.execute("UPDATE duel_users SET points = 7, last_activity_date = '2000-01-01' "
                     "WHERE chat_id = ? AND user_id = ?", (CHAT_A, 101))
        conn.execute("UPDATE duel_users SET last_activity_date = '2000-01-01' "
                     "WHERE chat_id = ? AND user_id = ?", (CHAT_A, 202))
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        assert (await client.get("/api/v1/me")).status_code == 401
        assert (await client.get("/api/v1/me", headers={"Authorization": "Bearer unknown"})).status_code == 401
        launch = create_launch_token(CHAT_A, 101)
        signed = signed_init_data(user={"id": 101}, auth_date=int(time.time()))
        rejected = await client.post("/api/v1/session", json={
            "init_data": signed, "launch_token": launch, "chat_id": CHAT_B,
        })
        assert rejected.status_code == 422
        wrong_user = await client.post("/api/v1/session", json={
            "init_data": signed_init_data(user={"id": 202}, auth_date=int(time.time())),
            "launch_token": launch,
        })
        assert wrong_user.status_code == 401
        accepted = await client.post("/api/v1/session", json={
            "init_data": signed, "launch_token": launch,
        })
        assert accepted.status_code == 200
        replay = await client.post("/api/v1/session", json={
            "init_data": signed, "launch_token": launch,
        })
        assert replay.status_code == 401
        headers = await session_for(client, CHAT_A, 101)
        assert (await client.get("/api/v1/me", headers=headers)).json()["points"] == 20
        assert (await client.get("/api/v1/duel/opponents", headers=headers)).status_code == 200
        assert (await client.get("/api/v1/duel/active", headers=headers)).json() == {
            "duel": None, "recent_finished": None,
        }
        with database.get_db() as conn:
            row = conn.execute("SELECT points, last_activity_date FROM duel_users "
                               "WHERE chat_id = ? AND user_id = 101", (CHAT_A,)).fetchone()
        assert row == (7, "2000-01-01")
        digest = hashlib.sha256(headers["Authorization"][7:].encode()).hexdigest()
        with database.get_db() as conn:
            conn.execute("UPDATE miniapp_sessions SET created_at = created_at - 7200, "
                         "expires_at = expires_at - 7200 WHERE token_digest = ?", (digest,))
        assert (await client.get("/api/v1/me", headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_cors_default_denies_and_configured_origin_is_exact(temp_database):
    default = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    configured = create_miniapp_api(bot_token=TEST_BOT_TOKEN,
                                    allowed_origin="https://app.example.test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=default),
                                 base_url="http://test") as client:
        response = await client.options("/api/v1/me", headers={
            "Origin": "https://app.example.test", "Access-Control-Request-Method": "GET",
        })
        assert "access-control-allow-origin" not in response.headers
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=configured),
                                 base_url="http://test") as client:
        allowed = await client.options("/api/v1/me", headers={
            "Origin": "https://app.example.test", "Access-Control-Request-Method": "GET",
        })
        denied = await client.options("/api/v1/me", headers={
            "Origin": "https://evil.example.test", "Access-Control-Request-Method": "GET",
        })
        assert allowed.headers["access-control-allow-origin"] == "https://app.example.test"
        assert "access-control-allow-origin" not in denied.headers


@pytest.mark.asyncio
async def test_me_does_not_register_a_missing_player(temp_database):
    api = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url="http://test") as client:
        headers = await session_for(client, CHAT_A, 404)
        assert (await client.get("/api/v1/me", headers=headers)).status_code == 404
        with database.get_db() as conn:
            assert conn.execute("SELECT count(*) FROM duel_users WHERE chat_id = ? AND user_id = ?",
                                (CHAT_A, 404)).fetchone()[0] == 0
