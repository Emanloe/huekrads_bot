"""Chat-scoped Mini App API for reads and persistent duel challenges."""

import hashlib
import logging
import os
import re
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, StrictInt
from starlette.concurrency import run_in_threadpool

from config import BOT_TOKEN, MAX_DAILY_POINTS
from database import (
    format_user_title, format_user_title_plain, get_bosses_defeated,
    get_duel_user_by_id, has_huecrab,
)
from duel_outbox_repository import list_persisted_duel_round_resolutions
from duel_session_repository import (
    get_current_duel_session, get_duel_session,
    get_latest_finished_participant_duel_session, utc_unix_milliseconds,
)
from handlers.duel_items import get_duel_display_inventory_rows
from handlers.duel_service import (
    get_duel_profile, list_duel_opponents, start_persistent_duel,
    submit_persistent_duel_attack, submit_persistent_duel_block,
)
from handlers.persistent_duel_publisher import recover_persistent_duel_chat
from handlers.duel_text import (
    _plural_rounds, get_duel_round_presentation, get_duel_title_read_model,
)
from miniapp_auth import InitDataError, verify_telegram_init_data
from miniapp_sessions import MiniAppSession, exchange_launch_token, get_miniapp_session
from text_resources import get_text


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


class DuelMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duel_id: StrictInt = Field(gt=0)
    turn_id: StrictInt = Field(gt=0)
    zone: str


_START_FAILURES = {
    "active_duel": (409, "В этом чате уже идёт дуэль."),
    "self_target": (409, "Нельзя вызвать себя на дуэль."),
    "initiator_not_registered": (403, "Вы не зарегистрированы в этом чате."),
    "opponent_not_registered": (404, "Соперник недоступен в этом чате."),
    "initiator_no_dick": (403, "Сегодня ваш гном не может участвовать в дуэли."),
    "opponent_no_dick": (403, "Сегодня соперник не может участвовать в дуэли."),
}

_MOVE_FAILURES = {
    "stale_turn": (409, "Этот ход уже закончился. Обновите дуэль."),
    "wrong_actor": (403, "Сейчас ход другого участника."),
    "wrong_phase": (409, "Фаза дуэли изменилась. Обновите дуэль."),
    "publishing": (409, "Ожидаем публикацию хода в Telegram."),
    "not_active": (409, "Дуэль уже не активна."),
    "terminal_pending": (409, "Дуэль завершается."),
    "turn_expired": (409, "Время хода вышло. Обновите дуэль."),
}


def _duel_participant(session: dict, user_id: int) -> dict:
    snapshot = (session["player1_snapshot"] if user_id == session["player1_user_id"]
                else session["player2_snapshot"])
    return {"user_id": user_id, "username": snapshot["username"],
            "display_name": format_user_title_plain(snapshot)}


def _plain_duel_text(value: object) -> str | None:
    """Remove only the bold/italic tags used by stored duel presentation."""
    if not isinstance(value, str):
        return None
    return unescape(re.sub(r"</?(?:b|i)>", "", value))


def _round_read_model(session: dict, resolution: dict) -> dict | None:
    participants = {session["player1_user_id"], session["player2_user_id"]}
    attacker_id = resolution.get("attacker_user_id")
    defender_id = resolution.get("defender_user_id")
    if (attacker_id not in participants or defender_id not in participants
            or attacker_id == defender_id
            or resolution.get("outcome") not in ("miss", "block", "hit", "suicide")
            or resolution.get("strike_zone") not in ("head", "body", "dick")
            or resolution.get("block_zone") not in ("head", "body", "dick")
            or type(resolution.get("round_no")) is not int
            or type(resolution.get("resolved_turn_id")) is not int):
        return None
    timeout_ids = resolution.get("timeout_user_ids", [])
    if not isinstance(timeout_ids, list):
        timeout_ids = []
    presentation = resolution.get("presentation_html")
    if not isinstance(presentation, str):
        try:
            presentation = get_duel_round_presentation(
                resolution,
                format_user_title(session["player1_snapshot"] if attacker_id ==
                                  session["player1_user_id"] else session["player2_snapshot"]),
                format_user_title(session["player1_snapshot"] if defender_id ==
                                  session["player1_user_id"] else session["player2_snapshot"]),
            )
        except (KeyError, TypeError, ValueError):
            presentation = None
    timeout_texts = []
    for user_id in timeout_ids:
        if type(user_id) is not int or user_id not in participants:
            continue
        snapshot = (session["player1_snapshot"] if user_id == session["player1_user_id"]
                    else session["player2_snapshot"])
        key = "duel.live.timeout.attack" if user_id == attacker_id else "duel.live.timeout.block"
        timeout_texts.append(_plain_duel_text(get_text(key, title=format_user_title(snapshot))))
    return {
        "round": resolution["round_no"],
        "resolved_turn_id": resolution["resolved_turn_id"],
        "attacker": _duel_participant(session, attacker_id),
        "defender": _duel_participant(session, defender_id),
        "attack_zone": resolution["strike_zone"],
        "defense_zone": resolution["block_zone"],
        "outcome": resolution["outcome"],
        "outcome_text": resolution.get("outcome_phrase") if isinstance(
            resolution.get("outcome_phrase"), str) else None,
        "presentation_text": _plain_duel_text(presentation),
        "timeout_texts": timeout_texts,
        "timed_out": [
            _duel_participant(session, user_id) for user_id in timeout_ids
            if type(user_id) is int and user_id in participants
        ],
    }


def _duel_rounds(session: dict) -> list[dict]:
    resolutions = list_persisted_duel_round_resolutions(session["chat_id"], session["id"])
    latest = session["result"]
    if isinstance(latest, dict):
        if latest.get("kind") == "finalized":
            latest = latest.get("terminal_resolution")
        if isinstance(latest, dict) and latest.get("kind") in (
            "round_resolution", "terminal_resolution",
        ):
            resolutions.append(latest)
    by_turn = {}
    for resolution in resolutions:
        model = _round_read_model(session, resolution)
        if model is not None:
            by_turn[model["resolved_turn_id"]] = model
    return [by_turn[turn] for turn in sorted(by_turn)]


def _finished_duel_read_model(session: dict) -> dict | None:
    result = session["result"]
    if not isinstance(result, dict) or result.get("kind") != "finalized":
        return None
    winner_id, loser_id = result["winner_user_id"], result["loser_user_id"]
    snapshots = {
        session["player1_user_id"]: session["player1_snapshot"],
        session["player2_user_id"]: session["player2_snapshot"],
    }
    stolen_item = result.get("stolen_item")
    berserk = result.get("berserk")
    berserk_model = None
    if (isinstance(berserk, dict)
            and berserk.get("berserker_user_id") in snapshots
            and berserk.get("victim_user_id") in snapshots):
        berserk_model = {
            "berserker": _duel_participant(session, berserk["berserker_user_id"]),
            "victim": _duel_participant(session, berserk["victim_user_id"]),
            "dick_lost": bool(berserk.get("applied")),
            "text": _plain_duel_text(berserk.get("text")),
        }
    duration_rounds = session["round_no"]
    return {
        "id": session["id"], "status": "finished", "finished_at": session["finished_at"],
        "player1": _duel_participant(session, session["player1_user_id"]),
        "player2": _duel_participant(session, session["player2_user_id"]),
        "winner": _duel_participant(session, winner_id),
        "loser": _duel_participant(session, loser_id),
        "points": {
            "winner_before": snapshots[winner_id]["points"],
            "winner_after": result["winner_points"],
            "winner_delta": result["winner_points"] - snapshots[winner_id]["points"],
            "winner_delta_awarded": result.get("winner_points_awarded") if type(
                result.get("winner_points_awarded")) is int else None,
            "loser_before": snapshots[loser_id]["points"],
            "loser_after": result["loser_points"],
            "loser_delta": result["loser_points"] - snapshots[loser_id]["points"],
            "loser_delta_awarded": result.get("loser_points_awarded") if type(
                result.get("loser_points_awarded")) is int else None,
        },
        "duration": {"rounds": duration_rounds,
                     "text": f"{duration_rounds} {_plural_rounds(duration_rounds)}"},
        "round_flavor": _plain_duel_text(result.get("round_flavor")),
        "dwarf_fact": result.get("dwarf_fact") if isinstance(result.get("dwarf_fact"), str) else None,
        "post_message": result.get("post_message") if isinstance(result.get("post_message"), str) else None,
        "note_prefix": (get_text("duel.finish.post_message.prefix")
                        if isinstance(result.get("post_message"), str) else None),
        "dick_stolen": bool(result["is_dick_stolen"]),
        "stolen_item": ({"item_id": stolen_item["item_id"],
                         "name": stolen_item["item_name"]} if stolen_item else None),
        "berserk": berserk_model,
        "rounds": _duel_rounds(session),
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
        has_dick = not user["dick_stolen_today"]
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
            "boss_wins": get_bosses_defeated(session.user_id, session.chat_id),
            "ineligibility": profile.ineligibility,
            "inventory": get_duel_display_inventory_rows(profile.inventory),
            "pet": (get_text("huecrab.inventory") if has_huecrab(
                session.chat_id, session.user_id) else None),
        }

    @app.get("/api/v1/duel/opponents")
    def opponents(session: MiniAppSession = Depends(require_session)):
        found = list_duel_opponents(session.chat_id, session.user_id, read_only=True)
        rows = []
        for item in found.opponents:
            user = get_duel_user_by_id(session.chat_id, item.user_id, read_only=True)
            if user is None:
                continue
            rows.append({
                "user_id": item.user_id, "username": item.username,
                "title": item.title, "points": user["points"],
                "wins": user["wins"], "losses": user["losses"],
                "titles": get_duel_title_read_model(user),
            })
        return {
            "ineligibility": found.ineligibility,
            "opponents": rows,
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

    @app.post("/api/v1/duel/move")
    async def move_duel(request: DuelMoveRequest,
                        session: MiniAppSession = Depends(require_session)):
        if request.zone not in ("head", "body", "dick"):
            raise HTTPException(status_code=422, detail={
                "code": "invalid_zone", "message": "Недопустимая зона хода.",
            })
        # This read only chooses the shared operation and hides inaccessible duels.
        # The chosen service rechecks phase, actor, turn and deadline in its write txn.
        duel = get_duel_session(session.chat_id, request.duel_id)
        if duel is None or session.user_id not in (
            duel["player1_user_id"], duel["player2_user_id"],
        ):
            raise HTTPException(status_code=404, detail={
                "code": "inaccessible_duel", "message": "Дуэль недоступна в этом чате.",
            })
        operation = (submit_persistent_duel_attack if duel["phase"] == "attack"
                     else submit_persistent_duel_block)
        try:
            result = await run_in_threadpool(
                operation, session.chat_id, request.duel_id, session.user_id,
                request.turn_id, request.zone,
            )
        except Exception:
            logging.exception("Mini App persistent duel move failed in chat %s", session.chat_id)
            raise HTTPException(status_code=500, detail={
                "code": "move_failed", "message": "Не удалось выполнить ход. Обновите дуэль.",
            }) from None
        if not result.accepted:
            if result.reason == "not_found":
                raise HTTPException(status_code=404, detail={
                    "code": "inaccessible_duel", "message": "Дуэль недоступна в этом чате.",
                })
            status, message = _MOVE_FAILURES.get(
                result.reason, (409, "Ход сейчас недоступен. Обновите дуэль."),
            )
            raise HTTPException(status_code=status, detail={
                "code": result.reason if result.reason in _MOVE_FAILURES else "move_unavailable",
                "message": message,
            })
        if telegram_bot is not None:
            try:
                await recover_persistent_duel_chat(
                    session.chat_id, telegram_bot, job_queue=job_queue,
                )
            except Exception:
                logging.exception("Mini App duel follow-up failed in chat %s", session.chat_id)
        return {"accepted": True, "duel_id": request.duel_id, "turn_id": request.turn_id}

    @app.get("/api/v1/duel/active")
    def active(response: Response, session: MiniAppSession = Depends(require_session)):
        duel = get_current_duel_session(session.chat_id)
        finished = get_latest_finished_participant_duel_session(
            session.chat_id, session.user_id,
        )
        server_now = utc_unix_milliseconds()
        response.headers["X-Duel-Server-Time-Ms"] = str(server_now)
        recent_finished = _finished_duel_read_model(finished) if finished else None
        if duel is None:
            return {"duel": None, "recent_finished": recent_finished}
        role = ("attacker" if session.user_id == duel["attacker_user_id"] else
                "defender" if session.user_id == duel["defender_user_id"] else "spectator")
        is_active = duel["status"] == "active"
        can_act = is_active and (
            (duel["phase"] == "attack" and role == "attacker") or
            (duel["phase"] == "block" and role == "defender")
        ) and duel["deadline_at"] is not None and duel["deadline_at"] > server_now

        return {"duel": {
            "id": duel["id"], "status": duel["status"], "phase": duel["phase"],
            "round": duel["round_no"], "turn_id": duel["turn_id"],
            "attacker": _duel_participant(duel, duel["attacker_user_id"]),
            "defender": _duel_participant(duel, duel["defender_user_id"]),
            "attack_zone": duel["attack_zone"] if role == "attacker" and duel["phase"] == "block" else None,
            "deadline_at": duel["deadline_at"] if is_active else None,
            "role": role, "can_act": can_act,
            "rounds": _duel_rounds(duel) if role != "spectator" else [],
        }, "recent_finished": recent_finished}

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
