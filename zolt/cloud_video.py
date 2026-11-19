"""Cloud video factory: runs on GitHub Actions when her PC is off.

    python -m zolt.cloud_video "<her request>"

The Cloudflare Worker starts this with her request. Zolt's brain writes the script and calls
make_cartoon_video (the only tool offered in cloud mode), which renders the cartoon on this Linux
machine and sends the video, cover and her upload texts to Telegram.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

os.environ.setdefault("ZOLT_CLOUD", "1")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
for noisy in ("httpx", "LiteLLM", "litellm"):
    logging.getLogger(noisy).setLevel(logging.ERROR)
log = logging.getLogger("zolt.cloud")


def _load_memory() -> None:
    """Her saved preferences (page name, LESSONs) live on the PC; the Worker keeps a copy for us."""
    import httpx

    from .memory import DATA_DIR
    url, token = os.getenv("CLOUD_URL", "").rstrip("/"), os.getenv("PC_TOKEN", "")
    if not url or not token:
        return
    try:
        r = httpx.get(f"{url}/memory", headers={"x-zolt-token": token}, timeout=20)
        if r.status_code == 200 and r.text.strip():
            DATA_DIR.mkdir(exist_ok=True)
            (DATA_DIR / "memory.md").write_text(r.text, encoding="utf-8")
            log.info("Loaded her memory (%d chars)", len(r.text))
    except Exception as e:
        log.warning("Could not load memory: %s", e)


async def main(request: str) -> None:
    _load_memory()
    from telegram import Bot
    from telegram.request import HTTPXRequest

    from . import providers  # noqa: F401  (registers all tools)
    from .brain import Brain
    from .config import CONFIG
    from .tools import ToolContext

    bot = Bot(CONFIG.telegram_token, request=HTTPXRequest(read_timeout=600, write_timeout=600, media_write_timeout=600))
    chat = CONFIG.allowed_user_id
    async with bot:
        ctx = ToolContext(bot=bot, chat_id=chat, approvals=None)
        note = ("(Cloud video factory: her PC is off. Only make_cartoon_video is available. Write the script and "
                "make the video now, then give her the upload texts.)\n\n")
        try:
            reply = await Brain().handle(chat, [{"type": "text", "text": note + request}], ctx)
        except Exception as e:
            log.exception("Cloud video failed")
            await bot.send_message(chat, f"⚠️ ম্যাম, cloud-এ video বানাতে সমস্যা হয়েছে: {type(e).__name__}. "
                                         "PC চালু হলে আবার চেষ্টা করব।")
            raise
        if "make_cartoon_video" not in ctx.tools_run:
            await bot.send_message(chat, "⚠️ ম্যাম, এবার video তৈরি হয়নি। একটু বিস্তারিত গল্প দিয়ে আবার বলবেন?")
        for i in range(0, len(reply), 4000):
            await bot.send_message(chat, reply[i:i + 4000])


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or os.getenv("ZOLT_REQUEST", "")))
