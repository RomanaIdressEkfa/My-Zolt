"""Ask the owner for a yes/no on Telegram and wait for the button press.

A third button trusts Zolt for TRUST_MINUTES, so a multi-step job (edit, build,
push) doesn't need a tap for every step. Power actions always ask.
"""
from __future__ import annotations

import asyncio
import time
import uuid

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

APPROVAL_TIMEOUT_SECONDS = 15 * 60
TRUST_MINUTES = 10


class Approvals:
    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._trusted_until: dict[int, float] = {}

    def trusted(self, chat_id: int) -> bool:
        return self._trusted_until.get(chat_id, 0) > time.monotonic()

    def revoke(self, chat_id: int) -> None:
        self._trusted_until.pop(chat_id, None)

    async def ask(self, bot: Bot, chat_id: int, question: str, allow_trust: bool = True) -> bool:
        if allow_trust and self.trusted(chat_id):
            await bot.send_message(chat_id, f"✅ (অটো-অনুমতি)\n{question[:600]}")
            return True

        approval_id = uuid.uuid4().hex[:12]
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = future
        rows = [[
            InlineKeyboardButton("✅ হ্যাঁ", callback_data=f"ap:{approval_id}:y"),
            InlineKeyboardButton("❌ না", callback_data=f"ap:{approval_id}:n"),
        ]]
        if allow_trust:
            rows.append([InlineKeyboardButton(f"⚡ {TRUST_MINUTES} মিনিট সব অনুমতি",
                                              callback_data=f"ap:{approval_id}:t")])
        message = await bot.send_message(
            chat_id, f"⚠️ অনুমতি লাগবে\n\n{question}", reply_markup=InlineKeyboardMarkup(rows)
        )
        try:
            choice = await asyncio.wait_for(future, APPROVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await message.edit_text(f"⌛ সময় শেষ, কাজটা করা হয়নি।\n\n{question}")
            return False
        finally:
            self._pending.pop(approval_id, None)
        if choice == "t":
            self._trusted_until[chat_id] = time.monotonic() + TRUST_MINUTES * 60
        return choice in ("y", "t")

    def resolve(self, approval_id: str, choice: str) -> bool:
        future = self._pending.get(approval_id)
        if future is None or future.done():
            return False
        future.set_result(choice)
        return True
