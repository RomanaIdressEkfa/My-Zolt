"""Zolt's brain: tries model providers in order and falls back when one runs out."""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from .providers import Provider, ProviderUnavailable, build_providers
from .tools import ToolContext

log = logging.getLogger("zolt.brain")

DEFAULT_ORDER = "claude,gemini,custom,groq,openrouter,mistral"
COOLDOWN_SECONDS = 10 * 60  # skip a failing provider for a while
IDLE_RESET_SECONDS = 30 * 60  # after this much silence, start a fresh conversation automatically
NO_VISION = set(os.getenv("NO_VISION_PROVIDERS", "groq").split(","))


class Brain:
    def __init__(self) -> None:
        self.providers = build_providers()
        order = os.getenv("MODEL_ORDER", DEFAULT_ORDER)
        self.order = [n.strip() for n in order.split(",") if n.strip() in self.providers]
        self.cooldown_until: dict[str, float] = {}
        self.last_used: dict[int, str] = {}
        self.last_seen: dict[int, float] = {}

    def configured(self) -> list[Provider]:
        return [self.providers[n] for n in self.order if self.providers[n].available()]

    def reset(self, chat_id: int) -> None:
        for p in self.providers.values():
            p.reset(chat_id)
        self.last_used.pop(chat_id, None)

    def status(self) -> str:
        now = time.monotonic()
        lines = []
        for n in self.order:
            p = self.providers[n]
            if not p.available():
                state = "⚪ key নেই"
            elif self.cooldown_until.get(n, 0) > now:
                state = f"🟡 বিরতিতে ({int((self.cooldown_until[n] - now) / 60) + 1} মিনিট)"
            else:
                state = "🟢 চালু"
            lines.append(f"{p.label}: {state}")
        return "\n".join(lines)

    async def handle(self, chat_id: int, content: list[dict[str, Any]], ctx: ToolContext) -> str:
        now = time.monotonic()
        if now - self.last_seen.get(chat_id, now) > IDLE_RESET_SECONDS:
            log.info("Idle for a while - starting a fresh conversation")
            self.reset(chat_id)
        self.last_seen[chat_id] = now

        candidates = self.configured()
        if not candidates:
            return ("🔑 ম্যাম, কোনো AI model-এর key বসানো নেই। .env-এ অন্তত একটা দিন "
                    "(যেমন GEMINI_API_KEY), তারপর Zolt restart করুন।")

        if any(block.get("type") == "image" for block in content):
            # Images: try vision-capable providers first (Groq's text models can't see).
            candidates = [p for p in candidates if p.name not in NO_VISION] + \
                         [p for p in candidates if p.name in NO_VISION]

        now = time.monotonic()
        ready = [p for p in candidates if self.cooldown_until.get(p.name, 0) <= now]
        # If everything is cooling down, try anyway - limits may have reset.
        failures = []
        for provider in ready or candidates:
            try:
                reply = await provider.handle(chat_id, content, ctx)
            except ProviderUnavailable as e:
                log.warning("%s unavailable: %s", provider.label, e)
                provider.reset(chat_id)
                self.cooldown_until[provider.name] = time.monotonic() + COOLDOWN_SECONDS
                failures.append(provider.label)
                if ctx.tools_run:
                    # Something already happened on the PC; don't redo it with another model.
                    return ("⚠️ কাজটা মাঝপথে থেমে গেছে। এর মধ্যে যা হয়েছে: "
                            f"{', '.join(ctx.tools_run)}। বাকিটা আবার বলুন, ম্যাম।")
                continue

            previous = self.last_used.get(chat_id)
            self.last_used[chat_id] = provider.name
            if failures or (previous and previous != provider.name):
                log.info("Answered by %s (unavailable: %s)", provider.label, ", ".join(failures) or previous)
            return reply

        log.warning("All models unavailable: %s", ", ".join(failures))
        return "😔 ম্যাম, এই মুহূর্তে internet বা AI service সাড়া দিচ্ছে না। কয়েক মিনিট পরে আবার বলুন।"
