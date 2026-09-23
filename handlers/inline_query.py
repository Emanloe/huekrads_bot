"""A single response for all inline choices offered by the bot."""

from telegram import Update
from telegram.ext import ContextTypes

from handlers.elite_ball import build_elite_ball_inline_result
from handlers.weather import build_weather_inline_result


async def inline_query_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    if inline_query is None:
        return

    query = (inline_query.query or "").strip()
    if not query:
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    weather_result, cache_time = build_weather_inline_result(query, context)
    await inline_query.answer(
        [weather_result, build_elite_ball_inline_result()],
        cache_time=cache_time,
        is_personal=True,
    )
