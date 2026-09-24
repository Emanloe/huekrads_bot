"""Read-only game API behind a chat-scoped Mini App bearer session."""

import os
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

from config import BOT_TOKEN
from database import format_user_title_plain
from duel_session_repository import get_current_duel_session, utc_unix_milliseconds
from handlers.duel_service import get_duel_profile, list_duel_opponents
from miniapp_auth import InitDataError, verify_telegram_init_data
from miniapp_sessions import MiniAppSession, exchange_launch_token, get_miniapp_session


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    init_data: str
    launch_token: str


def create_miniapp_api(*, bot_token: str | None = None,
                       allowed_origin: str | None = None) -> FastAPI:
    """Create an API without owning a second bot or a second game engine."""
    token = BOT_TOKEN if bot_token is None else bot_token
    if not token:
        raise RuntimeError("BOT_TOKEN is required for Mini App initData validation")
    origin = os.getenv("MINIAPP_ORIGIN", "").strip() if allowed_origin is None else allowed_origin
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
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
            "inventory": [{"item_id": key, "count": value} for key, value in sorted(inventory.items())],
        }

    @app.get("/api/v1/duel/opponents")
    def opponents(session: MiniAppSession = Depends(require_session)):
        found = list_duel_opponents(session.chat_id, session.user_id, read_only=True)
        return {
            "ineligibility": found.ineligibility,
            "opponents": [{"user_id": item.user_id, "username": item.username,
                           "title": item.title} for item in found.opponents],
        }

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

    return app
