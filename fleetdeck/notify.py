"""Push notifications: ntfy, Gotify, Discord, Telegram or a generic JSON webhook."""

from __future__ import annotations

import logging

import httpx

from .config import NotifyConfig

log = logging.getLogger(__name__)


class Notifier:
    def __init__(
        self, targets: list[NotifyConfig], public_url: str | None = None, client: httpx.AsyncClient | None = None
    ):
        self.targets = targets
        self.public_url = public_url
        self.client = client or httpx.AsyncClient(timeout=20)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def send(self, title: str, body: str) -> dict[str, str]:
        """Send to every configured target. Returns the error per target that failed (empty on success)."""
        errors: dict[str, str] = {}
        for i, t in enumerate(self.targets):
            name = t.name or f"{t.type} #{i + 1}"
            try:
                r = await self._send_one(t, title, body)
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text.strip()[:200]}")
            except Exception as e:  # noqa: BLE001 - one broken target must not stop the others
                log.warning("notification to %s failed: %s", name, e)
                errors[name] = str(e) or type(e).__name__
        return errors

    async def _send_one(self, t: NotifyConfig, title: str, body: str) -> httpx.Response:
        link = self.public_url
        if t.type == "ntfy":
            # The title travels in a header, and HTTP headers are ASCII.
            headers = {"Title": title.encode("ascii", "replace").decode(), "Tags": "package"}
            if link:
                headers["Click"] = link
            if t.token:
                headers["Authorization"] = f"Bearer {t.token}"
            return await self.client.post(t.url, content=body.encode(), headers=headers)
        if t.type == "gotify":
            return await self.client.post(
                t.url.rstrip("/") + "/message",
                headers={"X-Gotify-Key": t.token or ""},
                json={"title": title, "message": body + (f"\n\n{link}" if link else ""), "priority": 5},
            )
        if t.type == "discord":
            text = f"**{title}**\n{body}" + (f"\n{link}" if link else "")
            return await self.client.post(t.url, json={"content": text[:2000]})
        if t.type == "telegram":
            text = f"{title}\n\n{body}" + (f"\n\n{link}" if link else "")
            return await self.client.post(
                f"https://api.telegram.org/bot{t.token}/sendMessage",
                json={"chat_id": t.chat_id, "text": text[:4096], "disable_web_page_preview": True},
            )
        return await self.client.post(t.url, json={"title": title, "message": body, "url": link})
