"""PTB polling and HTTP share one managed event loop and shut down cleanly."""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import bot


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_http", [False, True])
async def test_managed_ptb_http_lifecycle_order(fail_http):
    events = []

    class Updater:
        async def start_polling(self, **kwargs):
            assert kwargs == {"drop_pending_updates": False}
            events.append("polling_start")

        async def stop(self):
            events.append("polling_stop")

    class Application:
        updater = Updater()

        async def initialize(self):
            events.append("initialize")

        async def post_init(self, application):
            assert application is self
            events.append("recovery")

        async def start(self):
            events.append("jobs_start")

        async def stop(self):
            events.append("jobs_stop")

        async def shutdown(self):
            events.append("shutdown")

    class Server:
        async def serve(self):
            events.append("http_serve")
            if fail_http:
                raise RuntimeError("listen failed")

    if fail_http:
        with pytest.raises(RuntimeError, match="listen failed"):
            await bot.run_ptb_and_http(Application(), Server())
    else:
        await bot.run_ptb_and_http(Application(), Server())
    assert events == [
        "initialize", "recovery", "polling_start", "jobs_start", "http_serve",
        "polling_stop", "jobs_stop", "shutdown",
    ]


@pytest.mark.asyncio
async def test_http_wiring_passes_existing_ptb_bot_and_job_queue(monkeypatch):
    telegram_bot = object()
    job_queue = object()
    application = SimpleNamespace(
        bot=telegram_bot, job_queue=job_queue, post_init=None,
        initialize=AsyncMock(), start=AsyncMock(), stop=AsyncMock(), shutdown=AsyncMock(),
        updater=SimpleNamespace(start_polling=AsyncMock(), stop=AsyncMock()),
    )
    api = object()
    create = Mock(return_value=api)
    config = Mock(return_value=object())
    server = Mock(return_value=SimpleNamespace(serve=AsyncMock()))
    monkeypatch.setattr(bot, "create_miniapp_api", create)
    monkeypatch.setattr(bot.uvicorn, "Config", config)
    monkeypatch.setattr(bot.uvicorn, "Server", server)

    await bot.run_ptb_and_http(application)

    create.assert_called_once_with(telegram_bot=telegram_bot, job_queue=job_queue)
    assert config.call_args.args[0] is api
    assert config.call_args.kwargs["workers"] == 1
    server.return_value.serve.assert_awaited_once()
