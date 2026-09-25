"""Chat-scoped Mini App API for reads and persistent duel challenges."""

import asyncio
import hashlib
import logging
import os
import re
import time
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, StrictInt
from starlette.concurrency import run_in_threadpool
from telegram import Bot, User

from config import BOT_TOKEN
from database import (
    format_user_title, format_user_title_plain, get_duel_top_read_model,
    get_duel_user_by_id,
)
from gnome_avatars import (
    DEFAULT_GNOME_VARIANT, GNOME_FILE_IDS, GNOME_VARIANTS,
    existing_gnome_variant, get_or_assign_gnome_variant, gnome_image_url,
    gnome_image_version, presented_gnome_image_url,
)
from duel_outbox_repository import (
    get_duel_prompt_for_turn, list_persisted_duel_round_resolutions,
)
from duel_session_repository import (
    get_current_duel_session, get_duel_session,
    get_latest_finished_participant_duel_session, utc_unix_milliseconds,
)
from handlers.duel_service import (
    list_inspectable_players, start_persistent_duel,
    submit_persistent_duel_attack, submit_persistent_duel_block,
)
from handlers.boss_read_model import get_boss_battle_read_model
from handlers.boss_service import apply_boss_action
from handlers.persistent_duel_publisher import recover_persistent_duel_chat
from handlers.duel_text import (
    _plural_rounds, get_duel_round_presentation, get_duel_title_read_model,
)
from handlers.player_stats import player_stats_read_model, public_player_stats
from miniapp_auth import InitDataError, verify_telegram_init_data
from miniapp_sessions import MiniAppSession, exchange_launch_token, get_miniapp_session
from text_resources import get_text


_STATIC_DIR = Path(__file__).resolve().parent / "miniapp_static"
_GNOME_FILE_ID = GNOME_FILE_IDS[DEFAULT_GNOME_VARIANT]
_GNOME_IMAGE_VERSION = gnome_image_version(DEFAULT_GNOME_VARIANT)
_GNOME_IMAGE_URL = f"/media/gnome?v={_GNOME_IMAGE_VERSION}"


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


class BossJoinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    battle_id: str = Field(min_length=10, max_length=64)
    round: StrictInt = Field(ge=0)


class BossActionRequest(BossJoinRequest):
    phase: Literal["attack", "block"]
    action_id: str


_BOSS_FAILURES = {
    "no_active_battle": (409, "Битва с боссом завершилась."),
    "stale_battle": (409, "Открыта другая битва. Обновите экран."),
    "stale_phase": (409, "Фаза боя изменилась. Обновите экран."),
    "stale_round": (409, "Раунд боя изменился. Обновите экран."),
    "recruitment_closed": (409, "Набор участников завершён."),
    "already_joined": (409, "Вы уже участвуете в битве."),
    "not_registered": (403, "Ваш профиль недоступен в этом чате."),
    "not_participant": (403, "Вы не участвуете в этой битве."),
    "eliminated": (403, "Вы выбыли из битвы."),
    "already_acted": (409, "Этот выбор уже принят."),
    "invalid_action": (422, "Недопустимое действие."),
}


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
    gnome_image_bytes: dict[str, bytes] = {}
    gnome_image_locks = {variant: asyncio.Lock() for variant in GNOME_VARIANTS}

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
    async def create_session(request: SessionRequest):
        try:
            verified = verify_telegram_init_data(request.init_data, token)
        except InitDataError:
            raise HTTPException(status_code=401, detail="Unauthorized") from None
        issued = await run_in_threadpool(exchange_launch_token, request.launch_token, verified.user_id)
        if issued is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        if telegram_bot is not None and issued.launch_message_id is not None:
            try:
                await telegram_bot.delete_message(
                    chat_id=issued.session.chat_id,
                    message_id=issued.launch_message_id,
                )
            except Exception as exc:
                logging.warning(
                    "Could not delete Mini App launch message in chat %s message %s (%s)",
                    issued.session.chat_id, issued.launch_message_id, type(exc).__name__,
                )
        return {"session_token": issued.token, "expires_at": issued.session.expires_at}

    @app.get("/api/v1/me")
    def me(session: MiniAppSession = Depends(require_session)):
        model = player_stats_read_model(session.chat_id, session.user_id)
        if model is None:
            raise HTTPException(status_code=404, detail="Player not found")
        variant = get_or_assign_gnome_variant(session.chat_id, session.user_id)
        if variant is None:
            raise HTTPException(status_code=404, detail="Player not found")
        display_variant = variant
        if display_variant not in GNOME_FILE_IDS:
            logging.warning("Unknown persisted gnome variant for chat %s user %s",
                            session.chat_id, session.user_id)
            display_variant = DEFAULT_GNOME_VARIANT
        return {
            **public_player_stats(model),
            "gnome_variant": variant,
            "gnome_image_url": gnome_image_url(display_variant),
        }

    @app.get("/api/v1/players/{target_user_id}")
    def inspect_player(target_user_id: int,
                       session: MiniAppSession = Depends(require_session)):
        if target_user_id <= 0:
            raise HTTPException(status_code=404, detail={"code": "inaccessible_player"})
        model = player_stats_read_model(session.chat_id, target_user_id)
        if model is None:
            raise HTTPException(status_code=404, detail={"code": "inaccessible_player"})
        variant = existing_gnome_variant(session.chat_id, target_user_id)
        return {
            **public_player_stats(model),
            "gnome_image_url": presented_gnome_image_url(
                variant, session.chat_id, target_user_id,
            ),
        }

    @app.get("/api/v1/boss")
    async def boss(response: Response, session: MiniAppSession = Depends(require_session)):
        state = await get_boss_battle_read_model(session.chat_id, session.user_id)
        response.headers["X-Boss-Server-Time-Ms"] = str(int(time.time() * 1000))
        return state

    async def boss_action_response(session: MiniAppSession, intent: str,
                                   request: BossJoinRequest, zone: str | None = None):
        if telegram_bot is None:
            raise HTTPException(status_code=503, detail={
                "code": "boss_unavailable", "message": "Бой временно недоступен.",
            })
        tg_user = None
        if intent == "join":
            profile = get_duel_user_by_id(session.chat_id, session.user_id, read_only=True)
            if profile is None:
                raise HTTPException(status_code=403, detail={
                    "code": "not_registered", "message": _BOSS_FAILURES["not_registered"][1],
                })
            tg_user = User(
                id=session.user_id, first_name=profile["display_name"] or "Гном",
                is_bot=False, username=profile["username"],
            )
        result = await apply_boss_action(
            SimpleNamespace(bot=telegram_bot), session.chat_id, session.user_id,
            intent, zone=zone, tg_user=tg_user,
            expected_battle_id=request.battle_id, expected_round=request.round,
            expected_phase=request.phase if isinstance(request, BossActionRequest) else None,
            dedupe_same_choice=True,
        )
        if not result.accepted:
            status, message = _BOSS_FAILURES[result.code]
            raise HTTPException(status_code=status, detail={
                "code": result.code, "message": message,
            })
        return {"accepted": True}

    @app.post("/api/v1/boss/join")
    async def join_boss(request: BossJoinRequest,
                        session: MiniAppSession = Depends(require_session)):
        return await boss_action_response(session, "join", request)

    @app.post("/api/v1/boss/action")
    async def move_boss(request: BossActionRequest,
                        session: MiniAppSession = Depends(require_session)):
        return await boss_action_response(session, request.phase, request, request.action_id)

    @app.get("/api/v1/duel/hall-of-fame")
    def hall_of_fame(session: MiniAppSession = Depends(require_session)):
        rows = get_duel_top_read_model(session.chat_id, limit=10)
        return {"sort_by": "wins", "players": [
            {
                "rank": rank,
                "user_id": row["user_id"],
                "title": format_user_title_plain({
                    "display_name": (row["display_name"] or row["username"]).lstrip("@")
                    if (row["display_name"] or row["username"]) else None,
                    "dwarf_name": row["dwarf_name"],
                }),
                "points": row["points"],
                "wins": row["wins"],
                "losses": row["losses"],
                "gnome_image_url": presented_gnome_image_url(
                    row["gnome_variant"], session.chat_id, row["user_id"],
                ),
            }
            for rank, row in enumerate(rows, 1)
        ]}

    @app.get("/api/v1/duel/opponents")
    def opponents(session: MiniAppSession = Depends(require_session)):
        ineligibility, found = list_inspectable_players(session.chat_id, session.user_id)
        rows = []
        for item in found:
            user = item["user"]
            rows.append({
                "user_id": user["user_id"], "username": user["username"],
                "title": format_user_title_plain(user), "points": user["points"],
                "wins": user["wins"], "losses": user["losses"],
                "titles": get_duel_title_read_model(user),
                "duel_ineligibility": item["duel_ineligibility"],
            })
        return {
            "ineligibility": ("active_duel" if get_current_duel_session(session.chat_id)
                               else ineligibility),
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
        own_attack_accepted = False
        if role == "attacker" and duel["phase"] == "block" and duel["attack_zone"]:
            block_prompt = get_duel_prompt_for_turn(
                session.chat_id, duel["id"], "block_prompt", duel["turn_id"],
            )
            own_attack_accepted = block_prompt is not None and (
                block_prompt["payload"].get("attack_timed_out_user_id") is None
            )

        return {"duel": {
            "id": duel["id"], "status": duel["status"], "phase": duel["phase"],
            "round": duel["round_no"], "turn_id": duel["turn_id"],
            "attacker": _duel_participant(duel, duel["attacker_user_id"]),
            "defender": _duel_participant(duel, duel["defender_user_id"]),
            "attack_zone": duel["attack_zone"] if role == "attacker" and duel["phase"] == "block" else None,
            "own_attack_accepted": own_attack_accepted,
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
        html = html.replace(
            'data-gnome-src="/media/gnome"', f'data-gnome-src="{_GNOME_IMAGE_URL}"',
        )
        return HTMLResponse(html)

    async def serve_gnome_image(variant: str, request: Request):
        if variant not in GNOME_FILE_IDS:
            raise HTTPException(status_code=404, detail="Not found")
        params = request.query_params
        if any(key != "v" for key in params) or params.get("v") not in (
            None, gnome_image_version(variant),
        ):
            raise HTTPException(status_code=404, detail="Not found")
        if variant not in gnome_image_bytes:
            async with gnome_image_locks[variant]:
                if variant not in gnome_image_bytes:
                    try:
                        if telegram_bot is not None:
                            image_file = await telegram_bot.get_file(GNOME_FILE_IDS[variant])
                            image_bytes = bytes(await image_file.download_as_bytearray())
                        else:
                            async with Bot(token) as image_bot:
                                image_file = await image_bot.get_file(GNOME_FILE_IDS[variant])
                                image_bytes = bytes(await image_file.download_as_bytearray())
                        if not image_bytes.startswith(b"\xff\xd8\xff"):
                            raise ValueError("The gnome image is not a JPEG")
                        gnome_image_bytes[variant] = image_bytes
                    except Exception:
                        logging.warning("Could not load the Mini App gnome image")
                        raise HTTPException(status_code=503, detail="Image temporarily unavailable") from None
        return Response(
            content=gnome_image_bytes[variant],
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/media/gnome", include_in_schema=False)
    async def legacy_gnome_image(request: Request):
        return await serve_gnome_image(DEFAULT_GNOME_VARIANT, request)

    @app.get("/media/gnome/{variant}", include_in_schema=False)
    async def gnome_image(variant: str, request: Request):
        return await serve_gnome_image(variant, request)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="miniapp_static")

    return app
