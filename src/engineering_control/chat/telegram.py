"""Telegram as the chat channel's transport: reading updates and sending messages.

The platform asks Telegram for updates (long polling); Telegram never calls the platform,
so the cluster needs no route from outside for it. The bot's token is part of every
request's address, so no address is ever logged or put into an error.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Final

import httpx

MESSAGE_CHARACTERS: Final = 3800
POLL_SECONDS: Final = 25
_BOLD: Final = re.compile(r"\*\*([^*\n]+)\*\*")
_CODE: Final = re.compile(r"`([^`\n]+)`")
_HEADING: Final = re.compile(r"^#{1,6}\s+", re.MULTILINE)


def quiet_addresses() -> None:
    """Keep request addresses out of the log. The HTTP client names the address of every
    request at the informational level, and Telegram's address holds the bot's token."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


class TelegramError(RuntimeError):
    """Telegram refused or did not answer. The message never holds the token."""


def to_html(text: str) -> str:
    """The supervisor's plain sentences with bold and code, as Telegram's HTML. Everything
    else is escaped and shown as the text it is."""
    escaped = html.escape(_HEADING.sub("", text), quote=False)
    return _CODE.sub(r"<code>\1</code>", _BOLD.sub(r"<b>\1</b>", escaped))


def chunks(text: str, limit: int = MESSAGE_CHARACTERS) -> list[str]:
    """Split a long text at paragraph or line ends so that each part fits one message."""
    parts: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


class Telegram:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        quiet_addresses()
        self._client = httpx.AsyncClient(
            base_url=f"{base_url}/bot{token}/",
            timeout=httpx.Timeout(POLL_SECONDS + 15, connect=10),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, **payload: Any) -> Any:
        try:
            response = await self._client.post(method, json=payload)
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            # Never the error's own text: it names the address, which holds the token.
            raise TelegramError(f"Telegram did not answer ({type(error).__name__})") from None
        if not isinstance(body, dict) or not body.get("ok"):
            said = body.get("description") if isinstance(body, dict) else None
            raise TelegramError(f"Telegram refused {method}: {str(said)[:200]}")
        return body.get("result")

    async def updates(self, offset: int | None) -> list[dict[str, Any]]:
        """The updates after ``offset``, waiting for one when there is none yet."""
        payload: dict[str, Any] = {
            "timeout": POLL_SECONDS,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        found = await self._call("getUpdates", **payload)
        return [update for update in found or [] if isinstance(update, dict)]

    async def send(
        self, chat_id: int, text: str, *, buttons: list[tuple[str, str]] | None = None
    ) -> None:
        """Send a text, in as many messages as it needs. Buttons go under the last one."""
        parts = chunks(text) or ["(nothing to say)"]
        for index, part in enumerate(parts):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": to_html(part),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if buttons and index == len(parts) - 1:
                payload["reply_markup"] = {
                    "inline_keyboard": [
                        [{"text": label, "callback_data": data} for label, data in buttons]
                    ]
                }
            await self._call("sendMessage", **payload)

    async def answer_button(self, callback_id: str, text: str) -> None:
        await self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:180])

    async def clear_buttons(self, chat_id: int, message_id: int) -> None:
        await self._call(
            "editMessageReplyMarkup",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup={"inline_keyboard": []},
        )
