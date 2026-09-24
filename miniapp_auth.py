"""Server-side Telegram Mini App initData verification.

Algorithm: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


INITDATA_MAX_AGE_SECONDS = 300
INITDATA_FUTURE_SKEW_SECONDS = 30
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_HASH = re.compile(r"[0-9a-fA-F]{64}\Z")
_BAD_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")


class InitDataError(ValueError):
    """Untrusted or malformed Telegram authentication data."""


@dataclass(frozen=True)
class VerifiedTelegramUser:
    user_id: int
    auth_date: int


def verify_telegram_init_data(raw_init_data: str, bot_token: str, *,
                              now: int | None = None) -> VerifiedTelegramUser:
    """Verify the raw query string before reading its Telegram user JSON."""
    if (not isinstance(raw_init_data, str) or not raw_init_data or
            len(raw_init_data.encode("utf-8")) > 8192 or
            _BAD_PERCENT.search(raw_init_data)):
        raise InitDataError("Invalid initData")
    if not isinstance(bot_token, str) or not bot_token:
        raise ValueError("Bot token is not configured")
    try:
        pairs = parse_qsl(raw_init_data, keep_blank_values=True,
                          strict_parsing=True, encoding="utf-8", errors="strict",
                          max_num_fields=64)
    except (ValueError, UnicodeError) as exc:
        raise InitDataError("Invalid initData") from exc
    fields = {}
    for key, value in pairs:
        if not _FIELD_NAME.fullmatch(key) or key in fields:
            raise InitDataError("Invalid initData fields")
        fields[key] = value
    supplied_hash = fields.pop("hash", None)
    if supplied_hash is None or not _HASH.fullmatch(supplied_hash):
        raise InitDataError("Invalid initData hash")
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash.lower()):
        raise InitDataError("Invalid initData signature")

    auth_date_text = fields.get("auth_date")
    if (auth_date_text is None or len(auth_date_text) > 12 or
            not auth_date_text.isdecimal()):
        raise InitDataError("Invalid auth_date")
    auth_date = int(auth_date_text)
    current = int(time.time()) if now is None else now
    if (auth_date > current + INITDATA_FUTURE_SKEW_SECONDS or
            current - auth_date > INITDATA_MAX_AGE_SECONDS):
        raise InitDataError("Expired initData")
    try:
        user = json.loads(fields["user"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InitDataError("Invalid Telegram user") from exc
    if not isinstance(user, dict) or type(user.get("id")) is not int or not (0 < user["id"] < 2**52):
        raise InitDataError("Invalid Telegram user")
    return VerifiedTelegramUser(user_id=user["id"], auth_date=auth_date)
