"""Reminders and scheduled tasks that survive restarts (data/reminders.json).

A reminder either just messages the owner, or (as_task=True) hands its text to
Zolt's brain as a command at that time, e.g. "every day 10am: ask me about outreach".
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, time as dtime, timedelta
from typing import Any, Awaitable, Callable

from telegram.ext import Application, ContextTypes

from .memory import DATA_DIR

log = logging.getLogger("zolt.reminders")
FILE = DATA_DIR / "reminders.json"
LOCAL_TZ = datetime.now().astimezone().tzinfo  # PC's timezone (job queue would assume UTC)


def _now() -> datetime:
    return datetime.now(LOCAL_TZ)

# Set by bot.py: runs a text command through the brain and sends the reply.
task_runner: Callable[[int, str], Awaitable[None]] | None = None


def _load() -> list[dict[str, Any]]:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(items: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


async def _fire(context: ContextTypes.DEFAULT_TYPE) -> None:
    item = context.job.data
    if item.get("as_task") and task_runner:
        await context.bot.send_message(item["chat_id"], f"⏰ নির্ধারিত কাজ শুরু: {item['text']}")
        await task_runner(item["chat_id"], item["text"])
    else:
        await context.bot.send_message(item["chat_id"], f"⏰ ম্যাম, মনে করিয়ে দিচ্ছি:\n{item['text']}")
    if item["repeat"] == "none":
        _save([i for i in _load() if i["id"] != item["id"]])


def _schedule(app: Application, item: dict[str, Any]) -> bool:
    when = datetime.fromisoformat(item["when"])
    if when.tzinfo is None:
        when = when.replace(tzinfo=LOCAL_TZ)
    if item["repeat"] == "daily":
        app.job_queue.run_daily(_fire, dtime(when.hour, when.minute, tzinfo=LOCAL_TZ), data=item, name=item["id"])
    elif item["repeat"] == "weekdays":  # Sun-Thu, Bangladesh work week
        app.job_queue.run_daily(_fire, dtime(when.hour, when.minute, tzinfo=LOCAL_TZ), days=(0, 1, 2, 3, 4),
                                data=item, name=item["id"])
    else:
        if when < _now() - timedelta(minutes=5):
            return False  # missed while the PC was off
        app.job_queue.run_once(_fire, max(when, _now()), data=item, name=item["id"])
    return True


def restore_all(app: Application) -> None:
    items = _load()
    kept = [i for i in items if _schedule(app, i)]
    if len(kept) != len(items):
        _save(kept)
    log.info("Restored %d reminder(s)", len(kept))


def add(app: Application, chat_id: int, when: datetime, text: str, repeat: str, as_task: bool) -> str:
    item = {"id": uuid.uuid4().hex[:8], "chat_id": chat_id, "when": when.isoformat(timespec="minutes"),
            "text": text, "repeat": repeat, "as_task": as_task}
    if not _schedule(app, item):
        return "That time is already in the past."
    _save([*_load(), item])
    return f"Scheduled (id {item['id']}) for {item['when']}, repeat: {repeat}, as_task: {as_task}."


def listing() -> str:
    items = _load()
    if not items:
        return "No reminders."
    return "\n".join(f"{i['id']}: {i['when']} repeat={i['repeat']} task={i.get('as_task')} - {i['text']}"
                     for i in items)


def cancel(app: Application, reminder_id: str) -> str:
    items = _load()
    if not any(i["id"] == reminder_id for i in items):
        return f"No reminder with id {reminder_id}."
    for job in app.job_queue.get_jobs_by_name(reminder_id):
        job.schedule_removal()
    _save([i for i in items if i["id"] != reminder_id])
    return f"Cancelled {reminder_id}."
