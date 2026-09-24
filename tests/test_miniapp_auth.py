"""Telegram's documented WebAppData HMAC validation, without a real bot token."""

import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

import miniapp_auth


TEST_BOT_TOKEN = "123456:TEST_ONLY_TOKEN"
NOW = 1_800_000_000


def signed_init_data(*, user=None, auth_date=NOW, extra=None):
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AA_test_query",
        "user": json.dumps({"id": 101, "first_name": "Test"} if user is None else user,
                           separators=(",", ":"), ensure_ascii=False),
    }
    fields.update(extra or {})
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_valid_initdata_and_constant_time_comparison(monkeypatch):
    calls = []
    original = miniapp_auth.hmac.compare_digest

    def compare(left, right):
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr(miniapp_auth.hmac, "compare_digest", compare)
    verified = miniapp_auth.verify_telegram_init_data(
        signed_init_data(extra={"signature": "third_party_signature"}), TEST_BOT_TOKEN,
        now=NOW,
    )
    assert (verified.user_id, verified.auth_date) == (101, NOW)
    assert len(calls) == 1 and len(calls[0][0]) == len(calls[0][1]) == 64


@pytest.mark.parametrize("tamper", [
    lambda raw: raw.replace("101", "102"),
    lambda raw: raw.replace("auth_date=1800000000", "auth_date=1800000001"),
    lambda raw: raw.replace("hash=", "hash=0"),
])
def test_tampered_initdata_is_rejected(tamper):
    # The user field is URL-encoded; first tamper changes its encoded digits.
    with pytest.raises(miniapp_auth.InitDataError):
        miniapp_auth.verify_telegram_init_data(tamper(signed_init_data()),
                                               TEST_BOT_TOKEN, now=NOW)


@pytest.mark.parametrize("raw", [
    signed_init_data(auth_date=NOW - miniapp_auth.INITDATA_MAX_AGE_SECONDS - 1),
    signed_init_data(auth_date=NOW + miniapp_auth.INITDATA_FUTURE_SKEW_SECONDS + 1),
    signed_init_data(user={"id": "101"}),
    signed_init_data(user={"id": True}),
    signed_init_data(user=[]),
    signed_init_data().split("&hash=")[0],
    "auth_date=1800000000&user=%7B%22id%22%3A101%7D&hash=0",
    signed_init_data() + "&user=%7B%22id%22%3A102%7D",
    signed_init_data() + "&auth_date=1800000000",
    signed_init_data() + "&bad=%ZZ",
])
def test_expired_malformed_missing_and_duplicate_fields_are_rejected(raw):
    with pytest.raises(miniapp_auth.InitDataError):
        miniapp_auth.verify_telegram_init_data(raw, TEST_BOT_TOKEN, now=NOW)


def test_invalid_signature_never_exposes_user(monkeypatch):
    def unexpected_parse(*_args, **_kwargs):
        raise AssertionError("user parsed before signature validation")

    monkeypatch.setattr(miniapp_auth.json, "loads", unexpected_parse)
    with pytest.raises(miniapp_auth.InitDataError):
        miniapp_auth.verify_telegram_init_data(
            signed_init_data().replace("hash=", "hash=0"), TEST_BOT_TOKEN, now=NOW,
        )


def test_missing_auth_date_with_valid_signature_is_rejected():
    user = json.dumps({"id": 101}, separators=(",", ":"))
    secret = hmac.new(b"WebAppData", TEST_BOT_TOKEN.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, f"user={user}".encode(), hashlib.sha256).hexdigest()
    raw = urlencode({"user": user, "hash": digest})
    with pytest.raises(miniapp_auth.InitDataError, match="auth_date"):
        miniapp_auth.verify_telegram_init_data(raw, TEST_BOT_TOKEN, now=NOW)


def test_wrong_64_character_hash_is_rejected_after_comparison():
    raw = signed_init_data()
    changed = raw[:-1] + ("0" if raw[-1] != "0" else "1")
    with pytest.raises(miniapp_auth.InitDataError, match="signature"):
        miniapp_auth.verify_telegram_init_data(changed, TEST_BOT_TOKEN, now=NOW)
