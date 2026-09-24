"""Static Mini App delivery and read-only frontend boundaries."""

from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute

from miniapp_api import create_miniapp_api
from tests.test_miniapp_auth import TEST_BOT_TOKEN


STATIC_DIR = Path(__file__).resolve().parents[1] / "miniapp_static"


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
        css = await client.get("/static/app.css")
        js = await client.get("/static/app.js")
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


def test_frontend_has_only_session_post_and_read_only_game_api():
    app = create_miniapp_api(bot_token=TEST_BOT_TOKEN, allowed_origin="")
    routes = {route.path: route.methods for route in app.routes if isinstance(route, APIRoute)}
    assert routes == {
        "/api/v1/session": {"POST"},
        "/api/v1/me": {"GET"},
        "/api/v1/duel/opponents": {"GET"},
        "/api/v1/duel/active": {"GET"},
        "/": {"GET"},
        "/app": {"GET"},
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
    assert "button.disabled = true" in js
    assert "action.disabled = true" in js
    for path in ("/api/v1/duel/start", "/api/v1/duel/attack", "/api/v1/duel/block",
                 "/api/v1/dig", "/api/v1/boss"):
        assert path not in js
