from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from text_resources import get_text


def test_weather_content_helpers_and_stub_callback_contract(fake_context):
    from handlers import weather

    moscow_alias = weather.MOSCOW_ALIASES[0]
    assert weather._geo_query_name(moscow_alias) == weather.CITY_GEO_QUERY[moscow_alias]
    assert weather._bonus_photo(moscow_alias) in weather.MOSCOW_PHOTO_IDS
    result_id = "abcdefghijklmnopQRST"
    markup = weather._stub_keyboard(result_id)
    assert markup.inline_keyboard[0][0].callback_data == "wxabcdefghijklmnop"
    weather._store_attach(fake_context, result_id, "photo", "file-id", "caption")
    assert weather._peek_attach(fake_context, result_id) == ("photo", "file-id", "caption")
    assert weather._pop_attach(fake_context, None, result_id[:16]) == ("photo", "file-id", "caption")


@pytest.mark.asyncio
async def test_empty_inline_weather_query_does_not_call_network(fake_context, monkeypatch):
    from handlers import weather

    inline_query = SimpleNamespace(query="", answer=AsyncMock())
    monkeypatch.setattr(weather, "_fetch_weather_html", lambda _city: (_ for _ in ()).throw(AssertionError("network")))
    await weather.weather_inline_query(SimpleNamespace(inline_query=inline_query), fake_context)
    inline_query.answer.assert_awaited_once_with([], cache_time=1, is_personal=True)


def test_weather_fetch_uses_mocked_http(monkeypatch):
    from handlers import weather

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    api_name = weather.CITY_GEO_QUERY[weather.MOSCOW_ALIASES[0]]
    responses = iter([
        Response({"results": [{"latitude": 55.75, "longitude": 37.61, "name": api_name}]}),
        Response({"current_weather": {"weathercode": 0, "temperature": 20, "windspeed": 3}}),
    ])
    monkeypatch.setattr(weather.requests, "get", lambda *args, **kwargs: next(responses))
    text, city = weather._fetch_weather_html(weather.MOSCOW_ALIASES[0])
    assert city == api_name
    assert text == (
        f"<b>Погода в {api_name}</b> \n\n"
        "☀️ <b>Ясно</b>\n"
        "🌡️ Температура: <b>20°C</b> (ощущается как 20°C)\n"
        "💨 Ветер: <b>3 м/с</b>\n"
    )


def test_weather_text_resources_preserve_full_condition_mapping_and_multiline_template():
    from handlers import weather

    assert weather.WEATHER_CODES == {
        0: ("☀️", "Ясно"),
        1: ("🌤️", "Преимущественно ясно"),
        2: ("⛅", "Переменная облачность"),
        3: ("☁️", "Пасмурно"),
        45: ("🌫️", "Туман"),
        48: ("🌫️", "Оседающий туман"),
        51: ("🌧️", "Лёгкая морось"),
        53: ("🌧️", "Морось"),
        55: ("🌧️", "Плотная морось"),
        61: ("☔", "Слабый дождь"),
        63: ("☔", "Умеренный дождь"),
        65: ("🌧️", "Сильный дождь"),
        71: ("❄️", "Слабый снег"),
        73: ("❄️", "Снегопад"),
        75: ("❄️", "Сильный снегопад"),
        80: ("🌦️", "Ливень"),
        95: ("⛈️", "Гроза"),
    }
    assert get_text("weather.fallback.emoji") == "🌡️"
    assert get_text("weather.fallback.description") == "Неизвестно"
    assert get_text(
        "weather.forecast.template",
        city="Тестоград",
        country="RU",
        emoji="☀️",
        description="Ясно",
        temperature=20,
        apparent_temperature=19,
        wind_speed=3,
    ) == (
        "<b>Погода в Тестоград</b> RU\n\n"
        "☀️ <b>Ясно</b>\n"
        "🌡️ Температура: <b>20°C</b> (ощущается как 19°C)\n"
        "💨 Ветер: <b>3 м/с</b>\n"
    )


@pytest.mark.asyncio
async def test_weather_inline_not_found_preserves_title_and_message(monkeypatch, fake_context):
    from handlers import weather

    inline_query = SimpleNamespace(query="Тестоград", answer=AsyncMock())
    monkeypatch.setattr(weather, "_fetch_weather_html", lambda _city: None)
    monkeypatch.setattr(weather, "_bonus_gif", lambda *_args: None)

    await weather.weather_inline_query(SimpleNamespace(inline_query=inline_query), fake_context)

    result = inline_query.answer.await_args.args[0][0]
    assert result.title == "Город «Тестоград» не найден"
    assert result.input_message_content.message_text == "❌ Город 'Тестоград' не найден."


@pytest.mark.asyncio
async def test_weather_inline_error_preserves_title_and_message(monkeypatch, fake_context):
    from handlers import weather

    inline_query = SimpleNamespace(query="Тестоград", answer=AsyncMock())
    monkeypatch.setattr(
        weather,
        "_fetch_weather_html",
        lambda _city: (_ for _ in ()).throw(RuntimeError("network")),
    )

    await weather.weather_inline_query(SimpleNamespace(inline_query=inline_query), fake_context)

    result = inline_query.answer.await_args.args[0][0]
    assert result.title == "Не удалось получить погоду"
    assert result.input_message_content.message_text == "⚠️ Не удалось получить данные о погоде."
