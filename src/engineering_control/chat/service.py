"""The chat channel as a process: read what the administrator sends, tell them what is new.

It has no listener. Two loops run side by side: one asks the transport for updates and
handles each; the other, every few seconds, tells the administrator what is new. Without a
token or an administrator the process stays up and says so, so that a cluster where the
bot is not set up yet still deploys.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Final

import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from engineering_control.chat.channel import ChatChannel
from engineering_control.chat.telegram import Telegram, TelegramError
from engineering_control.config import Settings, get_settings
from engineering_control.logging import configure_logging
from engineering_control.sandbox.queues import Budgets, Queues
from engineering_control.sandbox.runs import Runs
from engineering_control.secret_files import read_mounted_secret
from engineering_control.supervisor.conversation import Conversation
from engineering_control.supervisor.definition import Definitions
from engineering_control.supervisor.service import toolbox_from

LOG = structlog.get_logger(__name__)
DELIVER_SECONDS: Final = 5.0
RETRY_SECONDS: Final = 15.0
NOT_SET_UP_SECONDS: Final = 60.0


def token_from(settings: Settings) -> str | None:
    """The bot's token from its mounted file, or None when there is none to read."""
    if settings.telegram_token_file is None or not settings.telegram_token_file.exists():
        return None
    raw = read_mounted_secret(settings.telegram_token_file, label="chat bot token", maximum=200)
    token = raw.decode("ascii", errors="replace").strip()
    return token or None


async def read_updates(channel: ChatChannel, telegram: Telegram, stop: asyncio.Event) -> None:
    """Handle every update once, in order. How far it has read survives a restart."""
    stored = channel.remembered("offset")
    offset = int(stored) if stored and stored.isdigit() else None
    while not stop.is_set():
        try:
            updates = await telegram.updates(offset)
        except TelegramError as error:
            LOG.warning("chat_transport_error", error=str(error))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), RETRY_SECONDS)
            continue
        for update in updates:
            identifier = update.get("update_id")
            try:
                await channel.handle(update)
            except TelegramError as error:
                LOG.warning("chat_transport_error", error=str(error))
            except Exception as error:  # noqa: BLE001 - one message must not stop the channel
                LOG.error("chat_update_error", error=repr(error)[:300])
            if isinstance(identifier, int):
                # Moved on even after an error: an update that cannot be handled is not
                # handled again and again.
                offset = identifier + 1
                await asyncio.to_thread(channel.remember, "offset", str(offset))


async def tell_news(channel: ChatChannel, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await channel.deliver()
        except TelegramError as error:
            LOG.warning("chat_transport_error", error=str(error))
        except Exception as error:  # noqa: BLE001 - the database may be away for a moment
            LOG.error("chat_deliver_error", error=repr(error)[:300])
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), DELIVER_SECONDS)


async def serve(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    configure_logging(resolved.log_level)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stop.set)
    while not stop.is_set():
        token = token_from(resolved)
        if token is not None and resolved.telegram_administrator_id is not None:
            break
        LOG.warning(
            "chat_not_set_up",
            # Named so that the log's redaction of secrets leaves the answer readable.
            bot_secret_mounted=token is not None,
            administrator_named=resolved.telegram_administrator_id is not None,
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), NOT_SET_UP_SECONDS)
    if stop.is_set() or token is None or resolved.telegram_administrator_id is None:
        return
    database_url = resolved.database_url.get_secret_value()
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=3,
        max_overflow=2,
        connect_args={"connect_timeout": 5} if database_url.startswith("postgresql") else {},
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    runs = Runs(sessions)
    telegram = Telegram(token, base_url=resolved.telegram_api_url)
    channel = ChatChannel(
        sessions,
        telegram,
        administrator_id=resolved.telegram_administrator_id,
        conversation=Conversation(sessions, Definitions(sessions)),
        toolbox=toolbox_from(sessions, None),
        runs=runs,
        queues=Queues(sessions, runs, Budgets(sessions)),
        console_url=resolved.console_public_url,
        digest_hour=resolved.chat_digest_hour,
    )
    LOG.info("chat_started", since=channel.since().isoformat())
    tasks = [
        asyncio.create_task(read_updates(channel, telegram, stop)),
        asyncio.create_task(tell_news(channel, stop)),
    ]
    try:
        # A request for updates waits up to half a minute: on a stop it is not waited for.
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await telegram.aclose()
        engine.dispose()


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
