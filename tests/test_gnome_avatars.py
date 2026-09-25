"""Persistent cosmetic avatars stay chat-scoped and outside gameplay reads."""

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import database
import gnome_avatars as avatars
import miniapp_api
from handlers import duel
from miniapp_api import create_miniapp_api
from tests.test_inspect import update_for
from tests.test_miniapp_api import CHAT_A, CHAT_B, register, session_for
from tests.test_miniapp_auth import TEST_BOT_TOKEN


def test_canonical_catalog_contains_blue_and_nine_ordered_variants():
    assert avatars.GNOME_VARIANTS == tuple(f"gnome_{index:02d}" for index in range(10))
    assert list(avatars.GNOME_FILE_IDS.values()) == [
        "AgACAgIAAxkBAAPaarZGZ0LcyUlK8_7as-niVWw-EbIAApYbaxt2IbBJ2k2XK8ElkUMBAAMCAAN5AAM9BA",
        "AgACAgIAAxkBAAPcarZULnrKuWMKOtosgqroZQKMDqMAAukbaxt2IbBJ6ovXlnzZiDIBAAMCAAN4AAM9BA",
        "AgACAgIAAxkBAAPdarZULpkcnzswX5qchzGAoa2_XO4AAuobaxt2IbBJjO9Oc7zXz-ABAAMCAAN4AAM9BA",
        "AgACAgIAAxkBAAPearZULu1WMsikSaXmq-xIc8zewVAAAu0baxt2IbBJK5Mk0XhO92UBAAMCAAN4AAM9BA",
        "AgACAgIAAxkBAAPfarZULqToimhBC4PhKTdc39VH3gYAAuwbaxt2IbBJuKbfOY8oPbcBAAMCAAN4AAM9BA",
        "AgACAgIAAxkBAAPgarZULqu05wb-iog5HsejGfX62BoAAusbaxt2IbBJOt65YrnLJcYBAAMCAAN4AAM9BA",
        "AgACAgIAAxkBAAPharZULnkiYNqZI0E1_TM6usZHlLEAAu4baxt2IbBJ7R13AgkF1HkBAAMCAAN4AAM9BA",
        "AgACAgIAAxkBAAPiarZULnig_lN0DAm2CpQ4QCxPyYMAAvAbaxt2IbBJx2Zdb-5GOMwBAAMCAAN4AAM9BA",
        "AgACAgIAAxkBAAPjarZULhlRW246UV5qzNXB2X4bic4AAu8baxt2IbBJA8fYz-ScoBgBAAMCAAN4AAM9BA",
        "AgACAgIAAxkBAAPkarZULukMv-4XGppZT_DeG6u9DKAAAvEbaxt2IbBJUx30mDJiuY8BAAMCAAN4AAM9BA",
    ]
    assert len(set(avatars.GNOME_FILE_IDS.values())) == 10


def test_existing_player_schema_migrates_without_backfill_or_duplicate_column(
    temp_database,
):
    register(CHAT_A, 101, "existing")
    with database.get_db() as conn:
        conn.execute("ALTER TABLE duel_users DROP COLUMN gnome_variant")

    database.init_db()
    database.init_db()

    with database.get_db() as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(duel_users)")]
        stored = conn.execute(
            "SELECT username, gnome_variant FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (CHAT_A, 101),
        ).fetchone()
    assert columns.count("gnome_variant") == 1
    assert stored == ("existing", None)


def test_first_assignment_uses_one_cosmetic_choice_and_restart_reads_zero_rng(
    temp_database, monkeypatch,
):
    register(CHAT_A, 101, "player")
    choice = Mock(return_value="gnome_02")
    monkeypatch.setattr(avatars.secrets, "choice", choice)

    assert avatars.get_or_assign_gnome_variant(CHAT_A, 999) is None
    assert avatars.get_or_assign_gnome_variant(CHAT_A, 101) == "gnome_02"
    choice.assert_called_once_with(avatars.GNOME_VARIANTS)
    with database.get_db() as conn:
        conn.execute(
            "UPDATE duel_users SET last_activity_date = '2000-01-01' "
            "WHERE chat_id = ? AND user_id = ?", (CHAT_A, 101),
        )
    database.init_db()  # Same persisted DB, as after process restart.
    choice.side_effect = AssertionError("existing avatar used cosmetic RNG")
    assert avatars.get_or_assign_gnome_variant(CHAT_A, 101) == "gnome_02"
    with database.get_db() as conn:
        assert conn.execute(
            "SELECT gnome_variant FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (CHAT_A, 101),
        ).fetchone() == ("gnome_02",)
    choice.assert_called_once()


@pytest.mark.asyncio
async def test_me_assigns_independently_by_session_chat_and_repeated_reads_do_not_reroll(
    temp_database, monkeypatch,
):
    register(CHAT_A, 101, "player_a")
    register(CHAT_B, 101, "player_b")
    choice = Mock(side_effect=["gnome_02", "gnome_07"])
    monkeypatch.setattr(avatars.secrets, "choice", choice)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        a = await session_for(client, CHAT_A, 101)
        b = await session_for(client, CHAT_B, 101)
        forged = {**a, "X-Chat-Id": str(CHAT_B), "X-User-Id": "101"}
        first_a = (await client.request(
            "GET", f"/api/v1/me?chat_id={CHAT_B}&target_user_id=101",
            headers=forged, json={"chat_id": CHAT_B, "user_id": 101},
        )).json()
        first_b = (await client.get("/api/v1/me", headers=b)).json()
        assert first_a["gnome_variant"] == "gnome_02"
        assert first_b["gnome_variant"] == "gnome_07"
        assert first_a["gnome_image_url"] == avatars.gnome_image_url("gnome_02")
        assert first_b["gnome_image_url"] == avatars.gnome_image_url("gnome_07")
        assert (await client.get("/api/v1/me", headers=a)).json() == first_a
        assert (await client.get("/api/v1/me", headers=b)).json() == first_b
        assert "file_id" not in first_a and TEST_BOT_TOKEN not in str(first_a)
        assert "api.telegram.org" not in str(first_a)
    assert choice.call_count == 2
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT chat_id, gnome_variant FROM duel_users WHERE user_id = ? ORDER BY chat_id DESC",
            (101,),
        ).fetchall()
    assert rows == [(CHAT_A, "gnome_02"), (CHAT_B, "gnome_07")]


@pytest.mark.asyncio
async def test_inspect_opponents_and_duel_stats_do_not_assign_cosmetic_avatar(
    temp_database, fake_context, monkeypatch,
):
    register(CHAT_A, 101, "viewer")
    register(CHAT_A, 202, "target")
    choice = Mock(side_effect=AssertionError("non-avatar read used cosmetic RNG"))
    monkeypatch.setattr(avatars.secrets, "choice", choice)
    monkeypatch.setattr(duel, "send_and_schedule", AsyncMock())
    fake_context.args = ["@target"]
    await duel.inspect_command(update_for(CHAT_A, 101), fake_context)
    user = SimpleNamespace(id=101, username="viewer", first_name="viewer")
    update = SimpleNamespace(message=SimpleNamespace(
        from_user=user, chat=SimpleNamespace(id=CHAT_A), chat_id=CHAT_A,
    ))
    await duel.duel_stats_command(update, fake_context)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        headers = await session_for(client, CHAT_A, 101)
        assert (await client.get("/api/v1/players/202", headers=headers)).status_code == 200
        assert (await client.get("/api/v1/duel/opponents", headers=headers)).status_code == 200
    choice.assert_not_called()
    with database.get_db() as conn:
        assert conn.execute(
            "SELECT user_id, gnome_variant FROM duel_users WHERE chat_id = ? ORDER BY user_id",
            (CHAT_A,),
        ).fetchall() == [(101, None), (202, None)]


@pytest.mark.asyncio
async def test_repeated_inspect_of_unassigned_target_is_blue_and_never_writes_or_uses_rng(
    temp_database, monkeypatch,
):
    register(CHAT_A, 101, "viewer")
    register(CHAT_A, 202, "target")
    choice = Mock(side_effect=AssertionError("inspect used cosmetic RNG"))
    ensure = Mock(side_effect=AssertionError("inspect called assignment path"))
    monkeypatch.setattr(avatars.secrets, "choice", choice)
    monkeypatch.setattr(miniapp_api, "get_or_assign_gnome_variant", ensure)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        headers = await session_for(client, CHAT_A, 101)
        with database.get_db() as conn:
            before = conn.execute(
                "SELECT * FROM duel_users WHERE chat_id = ? AND user_id = ?",
                (CHAT_A, 202),
            ).fetchone()
        for _ in range(10):
            response = await client.get("/api/v1/players/202", headers=headers)
            assert response.status_code == 200
            data = response.json()
            assert data["gnome_image_url"] == avatars.gnome_image_url("gnome_00")
            assert "gnome_variant" not in data
            assert TEST_BOT_TOKEN not in response.text
            assert avatars.GNOME_FILE_IDS["gnome_00"] not in response.text
        with database.get_db() as conn:
            after = conn.execute(
                "SELECT * FROM duel_users WHERE chat_id = ? AND user_id = ?",
                (CHAT_A, 202),
            ).fetchone()
    assert before == after
    choice.assert_not_called()
    ensure.assert_not_called()


@pytest.mark.asyncio
async def test_inspect_uses_target_persisted_variant_without_touching_viewer(
    temp_database, monkeypatch,
):
    register(CHAT_A, 101, "viewer")
    register(CHAT_A, 202, "target")
    with database.get_db() as conn:
        conn.execute("UPDATE duel_users SET gnome_variant = 'gnome_03' WHERE chat_id = ? AND user_id = ?",
                     (CHAT_A, 101))
        conn.execute("UPDATE duel_users SET gnome_variant = 'gnome_07' WHERE chat_id = ? AND user_id = ?",
                     (CHAT_A, 202))
    choice = Mock(side_effect=AssertionError("inspect used cosmetic RNG"))
    monkeypatch.setattr(avatars.secrets, "choice", choice)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        headers = await session_for(client, CHAT_A, 101)
        response = await client.get("/api/v1/players/202", headers=headers)
    assert response.status_code == 200
    assert response.json()["gnome_image_url"] == avatars.gnome_image_url("gnome_07")
    assert response.json()["gnome_image_url"] != avatars.gnome_image_url("gnome_03")
    choice.assert_not_called()


@pytest.mark.asyncio
async def test_inspect_corrupt_variant_falls_back_without_reroll_or_rewrite(
    temp_database, monkeypatch, caplog,
):
    register(CHAT_A, 101, "viewer")
    register(CHAT_A, 202, "target")
    with database.get_db() as conn:
        conn.execute("UPDATE duel_users SET gnome_variant = 'legacy_unknown' "
                     "WHERE chat_id = ? AND user_id = ?", (CHAT_A, 202))
    choice = Mock(side_effect=AssertionError("inspect used cosmetic RNG"))
    monkeypatch.setattr(avatars.secrets, "choice", choice)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        headers = await session_for(client, CHAT_A, 101)
        response = await client.get("/api/v1/players/202", headers=headers)
    assert response.status_code == 200
    assert response.json()["gnome_image_url"] == avatars.gnome_image_url("gnome_00")
    assert "Unknown persisted gnome variant" in caplog.text
    choice.assert_not_called()
    with database.get_db() as conn:
        assert conn.execute(
            "SELECT gnome_variant FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (CHAT_A, 202),
        ).fetchone() == ("legacy_unknown",)


@pytest.mark.asyncio
async def test_inspect_avatar_uses_session_chat_and_cannot_reach_other_chat(
    temp_database, monkeypatch,
):
    register(CHAT_A, 101, "viewer")
    register(CHAT_A, 202, "same")
    register(CHAT_B, 101, "viewer_b")
    register(CHAT_B, 202, "same")
    register(CHAT_B, 303, "only_b")
    with database.get_db() as conn:
        conn.execute("UPDATE duel_users SET gnome_variant = 'gnome_02' WHERE chat_id = ? AND user_id = ?",
                     (CHAT_A, 202))
        conn.execute("UPDATE duel_users SET gnome_variant = 'gnome_08' WHERE chat_id = ? AND user_id = ?",
                     (CHAT_B, 202))
    choice = Mock(side_effect=AssertionError("inspect used cosmetic RNG"))
    monkeypatch.setattr(avatars.secrets, "choice", choice)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        headers = await session_for(client, CHAT_A, 101)
        forged = {**headers, "X-Chat-Id": str(CHAT_B), "X-User-Id": "202"}
        response = await client.request(
            "GET", f"/api/v1/players/202?chat_id={CHAT_B}&target_user_id=303",
            headers=forged, json={"chat_id": CHAT_B, "target_user_id": 303},
        )
        headers_b = await session_for(client, CHAT_B, 101)
        response_b = await client.get("/api/v1/players/202", headers=headers_b)
        hidden = await client.get("/api/v1/players/303", headers=forged)
    assert response.status_code == 200
    assert response.json()["gnome_image_url"] == avatars.gnome_image_url("gnome_02")
    assert response.json()["gnome_image_url"] != avatars.gnome_image_url("gnome_08")
    assert response_b.json()["gnome_image_url"] == avatars.gnome_image_url("gnome_08")
    assert hidden.status_code == 404
    assert hidden.json()["detail"]["code"] == "inaccessible_player"
    choice.assert_not_called()


def test_concurrent_first_access_persists_one_choice_for_one_chat_player(
    temp_database, monkeypatch,
):
    register(CHAT_A, 101, "player")
    choice = Mock(return_value="gnome_04")
    monkeypatch.setattr(avatars.secrets, "choice", choice)
    gate = Barrier(8)

    def assign(_):
        gate.wait(timeout=5)
        return avatars.get_or_assign_gnome_variant(CHAT_A, 101)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(assign, range(8)))

    assert results == ["gnome_04"] * 8
    choice.assert_called_once_with(avatars.GNOME_VARIANTS)
    with database.get_db() as conn:
        assert conn.execute(
            "SELECT gnome_variant FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (CHAT_A, 101),
        ).fetchone() == ("gnome_04",)


@pytest.mark.asyncio
async def test_concurrent_me_requests_see_one_persisted_avatar(temp_database, monkeypatch):
    register(CHAT_A, 101, "player")
    choice = Mock(return_value="gnome_06")
    monkeypatch.setattr(avatars.secrets, "choice", choice)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        headers = await session_for(client, CHAT_A, 101)
        responses = await asyncio.gather(*(
            client.get("/api/v1/me", headers=headers) for _ in range(8)
        ))
    assert all(response.status_code == 200 for response in responses)
    assert {response.json()["gnome_variant"] for response in responses} == {"gnome_06"}
    assert {response.json()["gnome_image_url"] for response in responses} == {
        avatars.gnome_image_url("gnome_06"),
    }
    choice.assert_called_once_with(avatars.GNOME_VARIANTS)
    with database.get_db() as conn:
        assert conn.execute(
            "SELECT gnome_variant FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (CHAT_A, 101),
        ).fetchone() == ("gnome_06",)


@pytest.mark.asyncio
async def test_unknown_persisted_variant_uses_blue_presentation_without_reroll(
    temp_database, monkeypatch, caplog,
):
    register(CHAT_A, 101, "player")
    with database.get_db() as conn:
        conn.execute(
            "UPDATE duel_users SET gnome_variant = 'legacy_unknown' WHERE chat_id = ? AND user_id = ?",
            (CHAT_A, 101),
        )
    choice = Mock(side_effect=AssertionError("unknown avatar used RNG"))
    monkeypatch.setattr(avatars.secrets, "choice", choice)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        headers = await session_for(client, CHAT_A, 101)
        data = (await client.get("/api/v1/me", headers=headers)).json()
    assert data["gnome_variant"] == "legacy_unknown"
    assert data["gnome_image_url"] == avatars.gnome_image_url("gnome_00")
    assert "Unknown persisted gnome variant" in caplog.text
    choice.assert_not_called()
    with database.get_db() as conn:
        assert conn.execute(
            "SELECT gnome_variant FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (CHAT_A, 101),
        ).fetchone() == ("legacy_unknown",)


@pytest.mark.asyncio
async def test_all_media_variants_use_fixed_file_ids_and_independent_cache(monkeypatch):
    files = {
        file_id: SimpleNamespace(download_as_bytearray=AsyncMock(
            return_value=bytearray(b"\xff\xd8\xff" + variant.encode()),
        ))
        for variant, file_id in avatars.GNOME_FILE_IDS.items()
    }
    get_file = AsyncMock(side_effect=lambda file_id: files[file_id])
    bot = SimpleNamespace(get_file=get_file)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="", telegram_bot=bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        for variant, file_id in avatars.GNOME_FILE_IDS.items():
            url = avatars.gnome_image_url(variant)
            assert avatars.gnome_image_version(variant) == hashlib.sha256(file_id.encode()).hexdigest()
            assert url.endswith(avatars.gnome_image_version(variant))
            for _ in range(2):
                response = await client.get(url)
                assert response.status_code == 200
                assert response.content == b"\xff\xd8\xff" + variant.encode()
                assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
                assert TEST_BOT_TOKEN not in response.text
            files[file_id].download_as_bytearray.assert_awaited_once_with()
        assert (await client.get("/media/gnome")).content == b"\xff\xd8\xffgnome_00"
        assert (await client.get("/media/gnome/unknown")).status_code == 404
        assert (await client.get(f"/media/gnome/{avatars.GNOME_FILE_IDS['gnome_01']}")).status_code == 404
        assert (await client.get("/media/gnome/gnome_01?file_id=other")).status_code == 404
        assert (await client.get("/media/gnome/gnome_01?v=wrong")).status_code == 404
    assert get_file.await_count == 10
    assert [call.args[0] for call in get_file.await_args_list] == list(avatars.GNOME_FILE_IDS.values())

    original_url = avatars.gnome_image_url("gnome_01")
    with monkeypatch.context() as patch:
        patch.setitem(avatars.GNOME_FILE_IDS, "gnome_01", "replacement-file-id")
        assert avatars.gnome_image_url("gnome_01") != original_url


@pytest.mark.asyncio
async def test_media_failure_retries_same_persisted_variant_without_reroll(
    temp_database, monkeypatch,
):
    register(CHAT_A, 101, "player")
    choice = Mock(return_value="gnome_03")
    monkeypatch.setattr(avatars.secrets, "choice", choice)
    image_file = SimpleNamespace(download_as_bytearray=AsyncMock(
        return_value=bytearray(b"\xff\xd8\xffgnome_03"),
    ))
    get_file = AsyncMock(side_effect=[RuntimeError(f"failed with {TEST_BOT_TOKEN}"), image_file])
    bot = SimpleNamespace(get_file=get_file)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="", telegram_bot=bot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        headers = await session_for(client, CHAT_A, 101)
        first_me = (await client.get("/api/v1/me", headers=headers)).json()
        first_media = await client.get(first_me["gnome_image_url"])
        second_me = (await client.get("/api/v1/me", headers=headers)).json()
        second_media = await client.get(second_me["gnome_image_url"])
    assert first_me == second_me
    assert first_me["gnome_variant"] == "gnome_03"
    assert first_media.status_code == 503
    assert TEST_BOT_TOKEN not in first_media.text
    assert second_media.status_code == 200
    assert second_media.content == b"\xff\xd8\xffgnome_03"
    assert [call.args[0] for call in get_file.await_args_list] == [
        avatars.GNOME_FILE_IDS["gnome_03"], avatars.GNOME_FILE_IDS["gnome_03"],
    ]
    choice.assert_called_once_with(avatars.GNOME_VARIANTS)
    with database.get_db() as conn:
        assert conn.execute(
            "SELECT gnome_variant FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (CHAT_A, 101),
        ).fetchone() == ("gnome_03",)
