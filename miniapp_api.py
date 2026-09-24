"""Chat-scoped Mini App API for reads and persistent duel challenges."""

import hashlib
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, PositiveInt
from starlette.concurrency import run_in_threadpool

from config import BOT_TOKEN
from database import format_user_title_plain
from duel_session_repository import get_current_duel_session, utc_unix_milliseconds
from handlers.duel_items import DUEL_ITEM_NAMES
from handlers.duel_service import get_duel_profile, list_duel_opponents, start_persistent_duel
from handlers.persistent_duel_publisher import recover_persistent_duel_chat
from miniapp_auth import InitDataError, verify_telegram_init_data
from miniapp_sessions import MiniAppSession, exchange_launch_token, get_miniapp_session


_STATIC_DIR = Path(__file__).resolve().parent / "miniapp_static"


def _versioned_static_url(filename: str) -> str:
    digest = hashlib.sha256((_STATIC_DIR / filename).read_bytes()).hexdigest()
    return f"/static/{filename}?v={digest}"


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    init_data: str
    launch_token: str


class StartDuelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opponent_user_id: PositiveInt


_START_FAILURES = {
    "active_duel": (409, "В этом чате уже идёт дуэль."),
    "self_target": (409, "Нельзя вызвать себя на дуэль."),
    "initiator_not_registered": (403, "Вы не зарегистрированы в этом чате."),
    "opponent_not_registered": (404, "Соперник недоступен в этом чате."),
    "initiator_no_dick": (403, "Сегодня ваш гном не может участвовать в дуэли."),
    "opponent_no_dick": (403, "Сегодня соперник не может участвовать в дуэли."),
}


def create_miniapp_api(*, bot_token: str | None = None,
                       allowed_origin: str | None = None,
                       telegram_bot=None, job_queue=None) -> FastAPI:
    """Create an API without owning a second bot or a second game engine."""
    token = BOT_TOKEN if bot_token is None else bot_token
    if not token:
        raise RuntimeError("BOT_TOKEN is required for Mini App initData validation")
    origin = os.getenv("MINIAPP_ORIGIN", "").strip() if allowed_origin is None else allowed_origin
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def frontend_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path in ("/", "/app", "/healthz") or request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    if origin:
        parsed = urlparse(origin)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.path or
                parsed.query or parsed.fragment or parsed.username or parsed.password or
                origin == "*"):
            raise ValueError("MINIAPP_ORIGIN must be one HTTPS origin")
        app.add_middleware(
            CORSMiddleware, allow_origins=[origin], allow_credentials=False,
            allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"],
        )

    def require_session(authorization: str | None = Header(default=None)) -> MiniAppSession:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Unauthorized")
        session = get_miniapp_session(authorization[7:])
        if session is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return session

    @app.post("/api/v1/session")
    def create_session(request: SessionRequest):
        try:
            verified = verify_telegram_init_data(request.init_data, token)
        except InitDataError:
            raise HTTPException(status_code=401, detail="Unauthorized") from None
        issued = exchange_launch_token(request.launch_token, verified.user_id)
        if issued is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return {"session_token": issued.token, "expires_at": issued.session.expires_at}

    @app.get("/api/v1/me")
    def me(session: MiniAppSession = Depends(require_session)):
        profile = get_duel_profile(session.chat_id, session.user_id, read_only=True)
        if profile is None:
            raise HTTPException(status_code=404, detail="Player not found")
        user = profile.user
        inventory = {}
        for item in profile.inventory:
            inventory[item["item_id"]] = inventory.get(item["item_id"], 0) + 1
        return {
            "user_id": user["user_id"], "username": user["username"],
            "display_name": user["display_name"], "dwarf_name": user["dwarf_name"],
            "points": user["points"], "wins": user["wins"], "losses": user["losses"],
            "daily_wins": user["daily_wins"],
            "dick_stolen_today": user["dick_stolen_today"],
            "ineligibility": profile.ineligibility,
            "inventory": [
                {"item_id": key, "name": DUEL_ITEM_NAMES.get(key, key), "count": value}
                for key, value in sorted(inventory.items())
            ],
        }

    @app.get("/api/v1/duel/opponents")
    def opponents(session: MiniAppSession = Depends(require_session)):
        found = list_duel_opponents(session.chat_id, session.user_id, read_only=True)
        return {
            "ineligibility": found.ineligibility,
            "opponents": [{"user_id": item.user_id, "username": item.username,
                           "title": item.title} for item in found.opponents],
        }

    @app.post("/api/v1/duel/start", status_code=201)
    async def start_duel(request: StartDuelRequest,
                         session: MiniAppSession = Depends(require_session)):
        # The transactional service owns admission, RNG, the chat slot and initial outbox.
        try:
            started = await run_in_threadpool(
                start_persistent_duel, session.chat_id, session.user_id,
                request.opponent_user_id,
            )
        except Exception:
            logging.exception("Mini App persistent duel start failed in chat %s", session.chat_id)
            raise HTTPException(status_code=500, detail={
                "code": "start_failed", "message": "Не удалось начать дуэль. Обновите данные.",
            }) from None
        if not started.success:
            status, message = _START_FAILURES.get(
                started.reason, (409, "Соперник сейчас недоступен."),
            )
            raise HTTPException(status_code=status, detail={
                "code": started.reason if started.reason in _START_FAILURES else "opponent_unavailable",
                "message": message,
            })
        # State is committed before Telegram I/O. The ordinary outbox worker also retries
        # this publication if the immediate wakeup fails.
        if telegram_bot is not None:
            try:
                await recover_persistent_duel_chat(
                    session.chat_id, telegram_bot, job_queue=job_queue,
                )
            except Exception:
                logging.exception("Mini App duel publication failed in chat %s", session.chat_id)
        return {"duel_id": started.session["id"]}

    @app.get("/api/v1/duel/active")
    def active(session: MiniAppSession = Depends(require_session)):
        duel = get_current_duel_session(session.chat_id)
        if duel is None:
            return {"duel": None}
        players = {
            duel["player1_user_id"]: duel["player1_snapshot"],
            duel["player2_user_id"]: duel["player2_snapshot"],
        }
        role = ("attacker" if session.user_id == duel["attacker_user_id"] else
                "defender" if session.user_id == duel["defender_user_id"] else "spectator")
        is_active = duel["status"] == "active"
        can_act = is_active and (
            (duel["phase"] == "attack" and role == "attacker") or
            (duel["phase"] == "block" and role == "defender")
        ) and duel["deadline_at"] is not None and duel["deadline_at"] > utc_unix_milliseconds()

        def participant(user_id: int) -> dict:
            snapshot = players[user_id]
            return {"user_id": user_id, "username": snapshot["username"],
                    "display_name": format_user_title_plain(snapshot)}

        return {"duel": {
            "id": duel["id"], "status": duel["status"], "phase": duel["phase"],
            "round": duel["round_no"], "turn_id": duel["turn_id"],
            "attacker": participant(duel["attacker_user_id"]),
            "defender": participant(duel["defender_user_id"]),
            "attack_zone": duel["attack_zone"] if role == "attacker" and duel["phase"] == "block" else None,
            "deadline_at": duel["deadline_at"] if is_active else None,
            "role": role, "can_act": can_act,
        }}

    @app.get("/", include_in_schema=False)
    @app.get("/app", include_in_schema=False)
    def miniapp_index():
        html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace(
            'href="/static/app.css"', f'href="{_versioned_static_url("app.css")}"',
        )
        html = html.replace(
            'src="/static/app.js"', f'src="{_versioned_static_url("app.js")}"',
        )
        return HTMLResponse(html)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="miniapp_static")

    return app
