"""Push channels: ntfy, Pushover, Discord, Telegram and email.

Each channel turns a batch of notifications into one message per channel run,
so a busy scan is one buzz, not forty. Credentials are plain settings; a
channel with none configured is simply off.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

from hd.config import Settings
from hd.logging import get_logger
from hd.notifiers.base import Notification, http_post

log = get_logger("notifiers.channels")

MAX_LINES = 10


def _summary(items: list[Notification]) -> tuple[str, str]:
    title = f"{len(items)} clearance deal{'s' if len(items) != 1 else ''}"
    shown = items[:MAX_LINES]
    lines = [f"{n.headline()}\n{n.body()}" + (f"\n{n.url}" if n.url else "") for n in shown]
    if len(items) > MAX_LINES:
        lines.append(f"…and {len(items) - MAX_LINES} more")
    return title, "\n\n".join(lines)


def _ok(status: int) -> bool:
    return 200 <= status < 300


class NtfyNotifier:
    name = "ntfy"

    def enabled(self, settings: Settings) -> bool:
        return bool(settings.ntfy_topic)

    async def send(self, settings: Settings, items: list[Notification]) -> bool:
        title, body = _summary(items)
        url = f"{settings.ntfy_url.rstrip('/')}/{settings.ntfy_topic}"
        headers = {"Title": title, "Tags": "label"}
        if items[0].url:
            headers["Click"] = items[0].url
        return _ok(await http_post(url, data=body.encode(), headers=headers))


class PushoverNotifier:
    name = "pushover"

    def enabled(self, settings: Settings) -> bool:
        return bool(settings.pushover_token and settings.pushover_user)

    async def send(self, settings: Settings, items: list[Notification]) -> bool:
        title, body = _summary(items)
        form = {
            "token": settings.pushover_token,
            "user": settings.pushover_user,
            "title": title,
            "message": body[:1024],
        }
        if items[0].url:
            form["url"] = items[0].url
        return _ok(await http_post("https://api.pushover.net/1/messages.json", form=form))


class DiscordNotifier:
    name = "discord"

    def enabled(self, settings: Settings) -> bool:
        return settings.discord_webhook_url.startswith("https://")

    async def send(self, settings: Settings, items: list[Notification]) -> bool:
        title, body = _summary(items)
        content = f"**{title}**\n{body}"[:1900]
        return _ok(await http_post(settings.discord_webhook_url, json_body={"content": content}))


class TelegramNotifier:
    name = "telegram"

    def enabled(self, settings: Settings) -> bool:
        return bool(settings.telegram_bot_token and settings.telegram_chat_id)

    async def send(self, settings: Settings, items: list[Notification]) -> bool:
        title, body = _summary(items)
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": settings.telegram_chat_id,
            "text": f"{title}\n\n{body}"[:4000],
            "disable_web_page_preview": True,
        }
        return _ok(await http_post(url, json_body=payload))


class EmailNotifier:
    name = "email"

    def enabled(self, settings: Settings) -> bool:
        return bool(settings.email_smtp_host and settings.email_to)

    async def send(self, settings: Settings, items: list[Notification]) -> bool:
        title, body = _summary(items)
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = settings.email_from or settings.email_user or settings.email_to
        msg["To"] = settings.email_to
        msg.set_content(body)

        def _send() -> bool:
            try:
                with smtplib.SMTP(settings.email_smtp_host, settings.email_smtp_port, timeout=15) as s:
                    s.starttls()
                    if settings.email_user:
                        s.login(settings.email_user, settings.email_password)
                    s.send_message(msg)
                return True
            except (smtplib.SMTPException, OSError) as exc:
                log.warning("Email notification failed", error=str(exc))
                return False

        return await asyncio.to_thread(_send)


ALL_NOTIFIERS = [
    NtfyNotifier(),
    PushoverNotifier(),
    DiscordNotifier(),
    TelegramNotifier(),
    EmailNotifier(),
]
