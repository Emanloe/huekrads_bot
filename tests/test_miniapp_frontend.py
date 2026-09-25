"""Static Mini App delivery and limited challenge-write boundaries."""

from pathlib import Path
import hashlib
import re
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.routing import APIRoute

import miniapp_api
from miniapp_api import create_miniapp_api
from tests.test_miniapp_auth import TEST_BOT_TOKEN


STATIC_DIR = Path(__file__).resolve().parents[1] / "miniapp_static"


def test_duel_timer_and_action_layout_in_browser_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for the vanilla JS runtime check")
    harness = Path(__file__).with_name("miniapp_timer_harness.cjs")
    subprocess.run([node, str(harness)], check=True, timeout=10)


def test_global_refresh_toolbar_is_absent_but_automatic_sync_remains():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert re.search(r"</nav>\s*<main>\s*<section id=\"screen-home\"", html)
    for removed in ("Данные обновлены", ">Обновить<", "app-status", "refresh-button",
                    'class="toolbar"'):
        assert removed not in html + js
    assert ".toolbar" not in css
    assert ".view-message" in css
    assert 'setViewStatus(view, error.message || "Не удалось загрузить данные.", true)' in js
    assert 'notice(message, true)' in js
    assert 'const refreshed = await loadView("duel", true)' in js
    assert 'const ACTIVE_POLL_MS = 1000' in js
    assert 'const IDLE_POLL_MS = 8000' in js
    assert 'window.setInterval(updateCountdown, COUNTDOWN_TICK_MS)' in js


def test_main_profile_has_square_gnome_and_responsive_fields_without_extra_titles():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    home = re.search(r'<section id="screen-home".*?</section>', html, re.S).group(0)
    assert "Личное дело" not in home
    assert "panel-title" not in home
    assert 'data-gnome-src="/media/gnome"' in home
    assert 'addHeading(content, "Гном")' not in js
    assert 'const image = element("img", "gnome-image")' in js
    assert 'image.src = data.gnome_image_url || document.getElementById("screen-home").dataset.gnomeSrc' in js
    assert 'profile.append(image, grid)' in js
    assert 'content.append(renderTitles(data.titles))' in js
    for title in ("Хуяние", "Статус", "Инвентарь"):
        assert f'addHeading(content, "{title}")' in js
    assert re.search(r'\.home-profile\s*\{[^}]*--gnome-size:\s*clamp\(125px, 27vw, 200px\)', css)
    assert re.search(r'\.home-profile\s*\{[^}]*grid-template-columns:\s*var\(--gnome-size\) minmax\(0, 1fr\)', css)
    assert re.search(r'\.gnome-image\s*\{[^}]*width:\s*var\(--gnome-size\);\s*height:\s*var\(--gnome-size\)', css)
    assert "aspect-ratio: 1 / 1" in css
    assert "object-fit: contain" in css
    assert "image-rendering: pixelated" in css
    assert re.search(r'@media \(max-width: 480px\)\s*\{\s*\.home-profile-info \.data-cell\s*\{\s*display:\s*block', css)
    assert re.search(r'@media \(max-width: 299px\)\s*\{\s*\.home-profile\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)', css)
    assert "@media (max-width: 560px)" not in css
    assert ".home-profile-info .value { min-width: 0; overflow-wrap: anywhere; }" in css
    for viewport, expected_min, expected_max in ((360, 120, 150), (420, 120, 150), (550, 120, 150), (920, 200, 200)):
        image_size = min(200, max(125, viewport * .27))
        assert expected_min <= image_size <= expected_max
        if viewport >= 300:
            assert viewport - image_size - 8 - 40 >= 130
    assert "html { min-width: 0; }" in css


@pytest.mark.asyncio
async def test_healthz_needs_no_session_or_game_reads(monkeypatch):
    def unexpected_call(*args, **kwargs):
        raise AssertionError("health check accessed game state")

    for name in ("get_miniapp_session", "player_stats_read_model", "list_inspectable_players",
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
            assert '<h1>ГНОМЬИ БОИ НА НОЖАХ</h1>' in response.text
            assert 'id="header-player"' not in response.text
            for removed in ("Чатовая арена", "Арена</span>",
                            "Игровой мир текущего чата", "Дуэли гномов",
                            "Гном не загружен"):
                assert removed not in response.text
            assert 'class="eyebrow"' not in response.text
            assert 'class="header-badge"' not in response.text
            assert 'class="header-strip"' not in response.text
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
        assert (await client.get("/api/v1/players/202")).status_code == 401
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
async def test_gnome_media_uses_one_fixed_telegram_file_and_same_origin_cached_url():
    assert miniapp_api._GNOME_FILE_ID == (
        "AgACAgIAAxkBAAPaarZGZ0LcyUlK8_7as-niVWw-EbIAApYbaxt2IbBJ2k2XK8ElkUMBAAMCAAN5AAM9BA"
    )
    assert miniapp_api._GNOME_IMAGE_VERSION == hashlib.sha256(
        miniapp_api._GNOME_FILE_ID.encode(),
    ).hexdigest()
    image_bytes = b"\xff\xd8\xffmock-jpeg"
    image_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(image_bytes)))
    telegram_bot = SimpleNamespace(get_file=AsyncMock(return_value=image_file))
    app = create_miniapp_api(
        bot_token=TEST_BOT_TOKEN, allowed_origin="", telegram_bot=telegram_bot,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        index = await client.get("/app")
        media_url = re.search(r'data-gnome-src="([^"]+)"', index.text).group(1)
        assert media_url == miniapp_api._GNOME_IMAGE_URL
        assert media_url.startswith("/media/gnome?v=")
        assert TEST_BOT_TOKEN not in index.text + media_url
        assert miniapp_api._GNOME_FILE_ID not in index.text + media_url
        css_url = re.search(r'href="(/static/app\.css\?v=[0-9a-f]{64})"', index.text).group(1)
        js_url = re.search(r'src="(/static/app\.js\?v=[0-9a-f]{64})"', index.text).group(1)
        css_response = await client.get(css_url)
        js_response = await client.get(js_url)
        assert TEST_BOT_TOKEN not in css_response.text + js_response.text
        assert miniapp_api._GNOME_FILE_ID not in css_response.text + js_response.text
        assert (await client.get("/media/gnome?file_id=other")).status_code == 404
        assert (await client.get("/media/gnome?v=wrong")).status_code == 404
        telegram_bot.get_file.assert_not_awaited()

        first = await client.get(media_url)
        second = await client.get(media_url)
        assert first.status_code == second.status_code == 200
        assert first.content == second.content == image_bytes
        assert first.headers["content-type"] == "image/jpeg"
        assert first.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert TEST_BOT_TOKEN.encode() not in first.content
        telegram_bot.get_file.assert_awaited_once_with(miniapp_api._GNOME_FILE_ID)
        image_file.download_as_bytearray.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_gnome_media_failure_never_exposes_bot_token():
    telegram_bot = SimpleNamespace(get_file=AsyncMock(
        side_effect=RuntimeError(f"failed with {TEST_BOT_TOKEN}"),
    ))
    app = create_miniapp_api(
        bot_token=TEST_BOT_TOKEN, allowed_origin="", telegram_bot=telegram_bot,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        response = await client.get(miniapp_api._GNOME_IMAGE_URL)
    assert response.status_code == 503
    assert TEST_BOT_TOKEN not in response.text
    assert miniapp_api._GNOME_FILE_ID not in response.text


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
        "/api/v1/players/{target_user_id}": {"GET"},
        "/api/v1/duel/opponents": {"GET"},
        "/api/v1/duel/active": {"GET"},
        "/api/v1/duel/start": {"POST"},
        "/api/v1/duel/move": {"POST"},
        "/": {"GET"},
        "/app": {"GET"},
        "/media/gnome": {"GET"},
        "/media/gnome/{variant}": {"GET"},
        "/healthz": {"GET"},
    }
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'src="https://telegram.org/js/telegram-web-app.js"' in html
    for path in routes:
        if path.startswith("/api/"):
            assert (path in js if "{" not in path else "/api/v1/players/${encodeURIComponent(userId)}" in js)
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
    assert "if (canChoose) {" in js
    assert "if (loading.duel) await loading.duel" in js
    assert 'const refreshed = await loadView("duel", true)' in js
    assert 'renderRoundHistory(duel.rounds)' in js
    assert 'renderFinished(data.recent_finished)' in js
    assert 'renderRoundHistory(finished.rounds)' in js
    assert 'round.timeout_texts || []' in js
    assert 'round.presentation_text' in js
    assert 'Время вышло: ${round.timed_out' not in js
    assert 'Number.isInteger(awarded)' in js
    assert 'points.winner_delta_awarded' in js
    assert 'points.loser_delta_awarded' in js
    assert 'finished.duration?.text' in js
    assert 'finished.round_flavor' in js
    assert 'finished.dwarf_fact' in js
    assert 'finished.post_message' in js
    assert 'finished.note_prefix' in js
    assert 'data.recent_finished.id !== duel.id' in js
    assert 'previous.append(element("summary", null, "Последняя завершённая дуэль")' in js
    assert '"Атака принята. Ожидаем соперника…"' in js
    assert 'opponents.addEventListener("click", () => navigate("opponents"))' in js
    assert 'node.textContent = String(value)' in js
    assert "Math.random" not in js
    assert 'const OUTCOME_NAMES = { miss: "Промах", block: "Блок", hit: "Попадание", suicide: "Самопоражение" }' in js
    assert 'const ACTIVE_POLL_MS = 1000' in js
    assert 'const IDLE_POLL_MS = 8000' in js
    assert 'const COUNTDOWN_TICK_MS = 250' in js
    assert 'X-Duel-Server-Time-Ms' in js
    assert 'remainingCountdownMs()' in js
    assert 'Math.ceil(remainingCountdownMs() / 1000)' in js
    assert 'if (loading.duel) await loading.duel' in js
    assert 'const refreshed = await loadView("duel", true)' in js
    assert 'performance.now() - duelPollStartedAt' in js
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
    assert 'dataCell("Статус на сегодня", data.dick_status?.text' in js
    assert 'dataCell("Хуй сегодня"' not in js
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


def test_compact_header_only_has_centered_title_and_profile_keeps_dwarf_name():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    header = re.search(r'<header class="site-header">(.*?)</header>', html, re.S).group(1)
    assert re.sub(r"<[^>]+>", "", header).strip() == "ГНОМЬИ БОИ НА НОЖАХ"
    assert header.count("<h1>") == 1
    assert html.index('<h1>ГНОМЬИ БОИ НА НОЖАХ</h1>') < html.index('class="tab-bar"')
    assert 'dataCell("Имя гнома", data.dwarf_name' in js
    assert 'header-player' not in html + css + js
    assert 'text-align: center' in css
    assert '.brand-line h1 { margin: 0;' in css
    assert 'overflow-wrap: anywhere' in css
    assert '.brand-line { padding: 6px 8px; }' in css


def test_blue_theme_semantic_colors_keep_text_readable():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    tokens = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-f]{6});", css))

    def luminance(color):
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [channel / 12.92 if channel <= 0.04045 else
                  ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
        return sum(channel * weight for channel, weight in zip(
            linear, (0.2126, 0.7152, 0.0722),
        ))

    def contrast(first, second):
        light, dark = sorted((luminance(tokens[first]), luminance(tokens[second])),
                             reverse=True)
        return (light + 0.05) / (dark + 0.05)

    for first, second in (
        ("text", "surface"), ("text", "surface-raised"),
        ("text-muted", "surface"), ("text-on-dark", "header"),
        ("text-on-dark", "accent-dark"), ("text-on-dark", "accent"),
        ("button-text", "button-bg"),
        ("button-disabled-text", "button-disabled-bg"),
        ("danger", "danger-bg"),
    ):
        assert contrast(first, second) >= 4.5, (first, second)

    for token in ("bg", "header", "accent", "accent-dark"):
        red, green, blue = (int(tokens[token][index:index + 2], 16)
                            for index in (1, 3, 5))
        assert blue > green > red
    assert ".tab.is-active" in css and "border-top: 2px solid var(--accent)" in css
    assert ".small-button:disabled" in css and "var(--button-disabled-bg)" in css
    assert ".brand-line h1" in css and "overflow-wrap: anywhere" in css
