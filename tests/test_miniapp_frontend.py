"""Static Mini App delivery and limited challenge-write boundaries."""

from pathlib import Path
import hashlib
import re

import httpx
import pytest
from fastapi.routing import APIRoute

import miniapp_api
from miniapp_api import create_miniapp_api
from tests.test_miniapp_auth import TEST_BOT_TOKEN


STATIC_DIR = Path(__file__).resolve().parents[1] / "miniapp_static"


@pytest.mark.asyncio
async def test_healthz_needs_no_session_or_game_reads(monkeypatch):
    def unexpected_call(*args, **kwargs):
        raise AssertionError("health check accessed game state")

    for name in ("get_miniapp_session", "get_duel_profile", "list_duel_opponents",
                 "get_current_duel_session"):
        monkeypatch.setattr(miniapp_api, name, unexpected_call)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_frontend_routes_and_api_auth_are_served_without_route_conflicts(temp_database):
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        for path in ("/", "/app"):
            response = await client.get(path)
            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
            assert "Дуэли гномов" in response.text
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["x-content-type-options"] == "nosniff"
            css_url = re.search(r'href="(/static/app\.css\?v=[0-9a-f]{64})"', response.text).group(1)
            js_url = re.search(r'src="(/static/app\.js\?v=[0-9a-f]{64})"', response.text).group(1)
            assert css_url == "/static/app.css?v=" + hashlib.sha256(
                (STATIC_DIR / "app.css").read_bytes(),
            ).hexdigest()
            assert js_url == "/static/app.js?v=" + hashlib.sha256(
                (STATIC_DIR / "app.js").read_bytes(),
            ).hexdigest()
            assert 'src="https://telegram.org/js/telegram-web-app.js"' in response.text
        css = await client.get(css_url)
        js = await client.get(js_url)
        assert css.status_code == js.status_code == 200
        assert "text/css" in css.headers["content-type"]
        assert "javascript" in js.headers["content-type"]
        assert ".game-shell" in css.text
        assert "Telegram" in js.text
        assert css.headers["x-content-type-options"] == "nosniff"
        assert (await client.get("/api/v1/me")).status_code == 401
        assert (await client.get("/api/v1/duel/opponents")).status_code == 401
        assert (await client.get("/api/v1/duel/active")).status_code == 401
        assert (await client.post("/api/v1/session", json={
            "init_data": "bad", "launch_token": "bad",
        })).status_code == 401
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert health.headers["cache-control"] == "no-store"
        assert (await client.post("/healthz")).status_code == 405


@pytest.mark.asyncio
async def test_asset_content_change_rotates_app_urls_without_release_constant(tmp_path, monkeypatch):
    for name in ("index.html", "app.css", "app.js"):
        (tmp_path / name).write_bytes((STATIC_DIR / name).read_bytes())
    monkeypatch.setattr(miniapp_api, "_STATIC_DIR", tmp_path)
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")

    def urls(html):
        return (
            re.search(r'href="(/static/app\.css\?v=[0-9a-f]{64})"', html).group(1),
            re.search(r'src="(/static/app\.js\?v=[0-9a-f]{64})"', html).group(1),
        )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        initial = await client.get("/app")
        old_css, old_js = urls(initial.text)
        (tmp_path / "app.js").write_bytes((tmp_path / "app.js").read_bytes() + b"\n// new build\n")
        changed_js = await client.get("/app")
        css_after_js, new_js = urls(changed_js.text)
        assert css_after_js == old_css and new_js != old_js
        assert new_js.endswith(hashlib.sha256((tmp_path / "app.js").read_bytes()).hexdigest())
        assert (await client.get(new_js)).status_code == 200
        (tmp_path / "app.css").write_bytes((tmp_path / "app.css").read_bytes() + b"\n/* new build */\n")
        changed_css = await client.get("/app")
        new_css, js_after_css = urls(changed_css.text)
        assert new_css != old_css and js_after_css == new_js
        assert (await client.get(new_css)).status_code == 200
        for response in (initial, changed_js, changed_css):
            assert response.headers["cache-control"] == "no-store"


def test_frontend_has_only_session_and_ordinary_duel_posts():
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    routes = {route.path: route.methods for route in app.routes if isinstance(route, APIRoute)}
    assert routes == {
        "/api/v1/session": {"POST"},
        "/api/v1/me": {"GET"},
        "/api/v1/duel/opponents": {"GET"},
        "/api/v1/duel/active": {"GET"},
        "/api/v1/duel/start": {"POST"},
        "/api/v1/duel/move": {"POST"},
        "/": {"GET"},
        "/app": {"GET"},
        "/healthz": {"GET"},
    }
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'src="https://telegram.org/js/telegram-web-app.js"' in html
    for path in routes:
        if path.startswith("/api/"):
            assert path in js
    assert 'method: "POST", body: { init_data: webApp.initData, launch_token: launchToken }' in js
    assert "new URLSearchParams(webApp.initData).get(\"start_param\")" in js
    assert "localStorage" not in js
    assert "sessionStorage" not in js
    assert "chat_id" not in js
    assert "innerHTML" not in js
    assert "eval(" not in js
    assert 'element("span", null, item.name || item.item_id)' in js
    assert "ceremonial_bolt" not in js
    assert "cork_with_bite_marks" not in js
    assert 'action.addEventListener("click", () => challengeOpponent(opponent.user_id))' in js
    assert 'body: { opponent_user_id: opponentUserId }' in js
    assert 'await navigate("duel")' in js
    assert 'challengeInFlight' in js
    assert "action.disabled = challengeInFlight" in js
    assert 'const ZONE_NAMES = { head: "Голова", body: "Торс", dick: "Хуй" }' in js
    assert 'body: { duel_id: duel.id, turn_id: duel.turn_id, zone }' in js
    assert 'button.addEventListener("click", () => submitMove(zone))' in js
    assert "button.disabled = !canChoose" in js
    assert "if (loading.duel) await loading.duel" in js
    assert 'const refreshed = await loadView("duel", true)' in js
    assert 'renderRoundHistory(duel.rounds)' in js
    assert 'renderFinished(data.recent_finished)' in js
    assert 'renderRoundHistory(finished.rounds)' in js
    assert 'round.timed_out.map(player => player.display_name)' in js
    assert 'data.recent_finished.id !== duel.id' in js
    assert 'previous.append(element("summary", null, "Последняя завершённая дуэль")' in js
    assert '"Выбор принят. Ожидаем соперника…"' in js
    assert 'opponents.addEventListener("click", () => navigate("opponents"))' in js
    assert 'node.textContent = String(value)' in js
    assert "Math.random" not in js
    assert 'const OUTCOME_NAMES = { miss: "Промах", block: "Блок", hit: "Попадание", suicide: "Самопоражение" }' in js
    assert 'const ACTIVE_POLL_MS = 8000' in js
    assert "button.disabled = true" in js
    for path in ("/api/v1/duel/attack", "/api/v1/duel/block",
                 "/api/v1/dig", "/api/v1/boss"):
        assert path not in js


def test_home_and_opponents_render_full_read_only_stats_safely():
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    assert 'dataCell("Очки", `${data.points} / ${data.max_points}`)' in js
    assert 'dataCell("Побед сегодня", data.daily_wins)' in js
    assert 'dataCell("Участие в дуэли"' in js
    assert 'addHeading(content, "Хуяние")' in js
    assert 'dataCell("Побеждено боссов", data.boss_wins)' in js
    assert 'dataCell("Хуй сегодня", data.dick_status?.text' in js
    assert 'if (data.pet) content.append(notice(data.pet))' in js

    for category in ('["wins", "Победы"]', '["losses", "Поражения"]',
                     '["stolen_dicks", "Украденные хуи"]'):
        assert category in js
    assert 'title?.text || "Нет звания"' in js
    assert '`${label}: ${title?.count ?? 0}`' in js
    assert 'content.append(renderTitles(data.titles))' in js
    assert 'identity.append(renderTitles(opponent.titles, true))' in js
    assert 'identity.append(element("span", "opponent-stats"' in js
    assert 'action.addEventListener("click", () => challengeOpponent(opponent.user_id))' in js
    assert 'element("span", null, item.name || item.item_id)' in js
    assert 'body.replaceChildren(content)' in js
    assert 'node.textContent = String(value)' in js
    assert 'overflow-wrap: anywhere' in css
    assert '.title-list.compact' in css
    assert 'Math.random' not in js
