"""Background watchers: website uptime and new Gmail messages. Runs every 5 minutes
and messages the owner only when something changes."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from telegram.ext import Application, ContextTypes

from .config import CONFIG
from .memory import DATA_DIR

log = logging.getLogger("zolt.monitors")
SITES = DATA_DIR / "monitors.json"
GMAIL_SEEN = DATA_DIR / "gmail_seen.json"
INTERVAL = 5 * 60


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path, data) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_site(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    sites: dict[str, Any] = _load(SITES, {})
    sites[url] = {"up": None}
    _save(SITES, sites)
    return f"Watching {url} every 5 minutes. She'll get a message if it goes down or comes back."


def remove_site(url: str) -> str:
    sites = _load(SITES, {})
    gone = [u for u in sites if url.lower() in u.lower()]
    for u in gone:
        sites.pop(u)
    _save(SITES, sites)
    return f"Stopped watching: {', '.join(gone) or 'nothing matched'}"


def list_sites() -> str:
    sites = _load(SITES, {})
    if not sites:
        return "No websites are being watched."
    return "\n".join(f"{u}: {'UP' if v['up'] else 'DOWN' if v['up'] is False else 'not checked yet'}"
                     for u, v in sites.items())


async def _check_sites(bot) -> None:
    sites = _load(SITES, {})
    if not sites:
        return
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": "Zolt uptime check"}) as client:
        for url, state in sites.items():
            try:
                r = await client.get(url)
                up, detail = r.status_code < 500, f"HTTP {r.status_code}"
            except httpx.HTTPError as e:
                up, detail = False, type(e).__name__
            if state.get("up") is not None and up != state["up"]:
                msg = f"✅ {url} আবার চালু হয়েছে।" if up else f"🚨 ম্যাম, {url} down! ({detail})"
                await bot.send_message(CONFIG.allowed_user_id, msg)
            state["up"] = up
    _save(SITES, sites)


async def _check_gmail(bot) -> None:
    from . import google_api
    if not google_api.ready():
        return
    try:
        new = await google_api.unread_since(set(_load(GMAIL_SEEN, [])))
    except Exception as e:
        log.warning("Gmail check failed: %s", e)
        return
    if not new:
        return
    seen = _load(GMAIL_SEEN, [])
    lines = []
    for m in new[:5]:
        seen.append(m["id"])
        lines.append(f"• {m['from'][:60]}\n  {m['subject'][:100]}")
    _save(GMAIL_SEEN, seen[-500:])
    await bot.send_message(CONFIG.allowed_user_id, "📧 নতুন email এসেছে, ম্যাম:\n\n" + "\n".join(lines))


async def _tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not CONFIG.allowed_user_id:
        return
    checks = [_check_sites]
    if os.getenv("EMAIL_ALERTS", "off").lower() == "on":  # new-email alerts are off unless she turns them on
        checks.append(_check_gmail)
    for check in checks:
        try:
            await check(context.bot)
        except Exception:
            log.exception("Monitor check failed")


def start(app: Application) -> None:
    app.job_queue.run_repeating(_tick, interval=INTERVAL, first=60, name="monitors")
