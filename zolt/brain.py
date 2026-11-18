"""Zolt's brain: tries model providers in order and falls back when one runs out."""
from __future__ import annotations

import logging
import re
import os
import time
from collections import defaultdict
from typing import Any

from .providers import Provider, ProviderUnavailable, build_providers
from .tools import ToolContext

log = logging.getLogger("zolt.brain")

DEFAULT_ORDER = "claude,gemini,custom,groq,openrouter,mistral"
COOLDOWN_SECONDS = 10 * 60  # skip a failing provider for a while
RATE_LIMIT_COOLDOWN = 2 * 60  # free per-minute limits reset quickly
TRANSCRIPT_TURNS = 8  # recent exchanges shared with whichever model answers next
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
        # Model-neutral record of the chat, so a model that takes over knows what was said.
        self.transcript: dict[int, list[tuple[str, str]]] = defaultdict(list)

    def configured(self) -> list[Provider]:
        return [self.providers[n] for n in self.order if self.providers[n].available()]

    def reset(self, chat_id: int) -> None:
        for p in self.providers.values():
            p.reset(chat_id)
        self.last_used.pop(chat_id, None)
        self.transcript.pop(chat_id, None)

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
                reply = await provider.handle(chat_id, self._with_context(provider, chat_id, content, ctx), ctx)
            except ProviderUnavailable as e:
                log.warning("%s unavailable: %s", provider.label, e)
                provider.reset(chat_id)
                if "per-day" in str(e) or "per day" in str(e).lower():
                    pause = 60 * 60  # daily quota used up; don't keep knocking
                else:
                    pause = RATE_LIMIT_COOLDOWN if "RateLimit" in str(e) else COOLDOWN_SECONDS
                self.cooldown_until[provider.name] = time.monotonic() + pause
                failures.append(provider.label)
                if ctx.steps:
                    log.info("Handing the unfinished task (%d steps done) to the next model", len(ctx.steps))
                continue

            previous = self.last_used.get(chat_id)
            self.last_used[chat_id] = provider.name
            if failures or (previous and previous != provider.name):
                log.info("Answered by %s (unavailable: %s)", provider.label, ", ".join(failures) or previous)
            reply = await self._clean_language(reply)
            self._remember_turn(chat_id, content, reply, ctx)
            return reply

        log.warning("All models unavailable: %s", ", ".join(failures))
        if ctx.tools_run:
            return ("⚠️ কাজটা মাঝপথে থেমে গেছে (সব AI model ব্যস্ত)। এর মধ্যে যা হয়েছে: "
                    f"{', '.join(ctx.tools_run)}। কয়েক মিনিট পরে \"বাকিটা করো\" বলুন, ম্যাম।")
        return "😔 ম্যাম, এই মুহূর্তে internet বা AI service সাড়া দিচ্ছে না। কয়েক মিনিট পরে আবার বলুন।"

    def _with_context(self, provider: Provider, chat_id: int, content: list[dict[str, Any]],
                      ctx: ToolContext) -> list[dict[str, Any]]:
        """Give a model that wasn't part of the chat so far the recent conversation, and the
        steps already done if it is taking over a half-finished task."""
        parts = []
        missed = not provider.histories.get(chat_id) or self.last_used.get(chat_id) != provider.name
        if missed and self.transcript.get(chat_id):
            lines = "\n".join(f"{who}: {text}" for who, text in self.transcript[chat_id])
            parts.append("Recent conversation with her (continue it naturally):\n" + lines)
        if ctx.steps:
            parts.append("You are taking over her current request from another assistant that stopped midway. "
                         "These steps are ALREADY DONE - do not repeat them, continue from here:\n"
                         + "\n".join(ctx.steps[-15:]))
        if not parts:
            return content
        return [{"type": "text", "text": "\n\n".join(parts) + "\n\n--- Her message:"}, *content]

    def _remember_turn(self, chat_id: int, content: list[dict[str, Any]], reply: str, ctx: ToolContext) -> None:
        said = " ".join(b["text"] for b in content if b.get("type") == "text").strip()
        if any(b.get("type") == "image" for b in content):
            said = ("[photo] " + said).strip()
        done = f" [tools: {', '.join(dict.fromkeys(ctx.tools_run))}]" if ctx.tools_run else ""
        turns = self.transcript[chat_id]
        turns += [("Her", said[:600]), ("You", reply[:600] + done)]
        del turns[:-2 * TRANSCRIPT_TURNS]

    async def _clean_language(self, reply: str) -> str:
        """Free models sometimes slip in Thai/Spanish/Chinese words. Detect that and
        have a fast model rewrite the reply in clean Bangla (+ English tech words)."""
        if not has_foreign_script(reply):
            return reply
        import litellm
        for provider in self.configured():
            if not hasattr(provider, "models") or not provider.models:
                continue
            try:
                r = await litellm.acompletion(
                    model=provider.models[0], api_key=os.getenv(provider.key_env, "").strip(),
                    timeout=40, num_retries=0, messages=[
                        {"role": "system", "content": CLEANUP_PROMPT},
                        {"role": "user", "content": reply}])
                fixed = (r.choices[0].message.content or "").strip()
                if fixed and not has_foreign_script(fixed):
                    log.info("Cleaned mixed-language reply with %s", provider.label)
                    return fixed
            except Exception as e:
                log.warning("Language cleanup with %s failed: %s", provider.label, e)
        return FOREIGN_RE.sub("", reply)  # last resort: just drop the stray characters


CLEANUP_PROMPT = ("Rewrite the message in natural Bangla (Bengali script). Keep technical terms, names, numbers, "
                  "URLs, code, file paths and Markdown formatting exactly. Replace any word from other languages "
                  "(Thai, Spanish, Chinese, Hindi, etc.) with the right Bangla word. Do not add or remove information. "
                  "Output only the rewritten message.")
# Scripts/letters that never belong in her replies (Bangla, English, digits, emoji are fine).
_FOREIGN_RANGES = [(0x0E00, 0x0E7F), (0x0400, 0x04FF), (0x4E00, 0x9FFF), (0x3040, 0x30FF), (0xAC00, 0xD7AF),
                   (0x0600, 0x06FF), (0x0900, 0x0963), (0x0966, 0x097F), (0x0370, 0x03FF),
                   (0x00C0, 0x00D6), (0x00D8, 0x00F6), (0x00F8, 0x00FF)]  # Bangla danda U+0964/5 allowed
FOREIGN_RE = re.compile("[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in _FOREIGN_RANGES) + "]")


def has_foreign_script(text: str) -> bool:
    return bool(FOREIGN_RE.search(text))
