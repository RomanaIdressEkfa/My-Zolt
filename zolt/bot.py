"""Telegram side of Zolt: receives messages/photos, asks the brain, sends replies."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from collections import defaultdict
from datetime import time as dtime
from pathlib import Path

from telegram import BotCommand, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, TypeHandler,
    filters,
)

from . import mcp_client, monitors, reminders, tools_more
from .approvals import Approvals
from .brain import Brain
from .config import CONFIG
from .tools import ToolContext

log = logging.getLogger("zolt")

approvals = Approvals()
brain = Brain()
chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
running: dict[int, asyncio.Task] = {}
MORNING_PROMPT = ("Morning brief for today, in short Bangla bullet points: 1) today's reminders (list_reminders), "
                  "2) watched websites status (watch_website list), 3) today's calendar events if connected, "
                  "4) PC status (system_status: disk and internet only). Skip anything not set up without complaining. "
                  "End with one friendly line wishing her a productive day.")


def _is_owner(update: Update) -> bool:
    user = update.effective_user
    return user is not None and CONFIG.allowed_user_id is not None and user.id == CONFIG.allowed_user_id


async def _reject(update: Update) -> None:
    user = update.effective_user
    if CONFIG.allowed_user_id is None and update.effective_message:
        # First-time setup: tell the owner her ID so she can put it in .env.
        await update.effective_message.reply_text(
            f"👋 আপনার Telegram ID: {user.id}\n\n"
            f".env ফাইলে ALLOWED_TELEGRAM_USER_ID={user.id} লিখে {CONFIG.agent_name}-কে আবার চালু করুন।"
        )
    else:
        log.warning("Ignored message from unauthorized user %s", user.id if user else "?")


async def _send_long(update: Update, text: str) -> None:
    for i in range(0, len(text), 4000):
        await update.effective_message.reply_text(text[i:i + 4000])


async def _keep_typing(bot, chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), 4.5)
        except asyncio.TimeoutError:
            pass


async def run_brain(bot, app, chat_id: int, content: list[dict]) -> str:
    ctx = ToolContext(bot=bot, chat_id=chat_id, approvals=approvals, app=app)
    started = time.monotonic()
    async with chat_locks[chat_id]:
        stop = asyncio.Event()
        typing = asyncio.create_task(_keep_typing(bot, chat_id, stop))
        log.info("Thinking...")
        task = asyncio.create_task(brain.handle(chat_id, content, ctx))
        running[chat_id] = task
        try:
            reply = await task
            log.info("Reply ready (%d chars), tools used: %s", len(reply), ctx.tools_run or "none")
        except asyncio.CancelledError:
            if not task.cancelled():
                raise  # the handler itself is being cancelled (shutdown)
            log.info("Task stopped by owner")
            brain.reset(chat_id)
            reply = f"🛑 থামানো হয়েছে। এর মধ্যে যা হয়েছিল: {', '.join(ctx.tools_run) or 'কিছু না'}।"
        except Exception:
            log.exception("Brain failed")
            brain.reset(chat_id)
            reply = "⚠️ ম্যাম, একটা সমস্যা হয়েছে। PC-তে Zolt-এর লগ দেখুন।"
        finally:
            stop.set()
            await typing
    try:
        from .history import log_conversation
        said = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
        if any(b.get("type") == "image" for b in content):
            said = "[photo] " + said
        log_conversation(said, reply, ctx.tools_run, brain.last_used.get(chat_id, ""), time.monotonic() - started)
    except OSError:
        pass
    return reply


async def _send_text(bot, chat_id: int, text: str) -> None:
    for i in range(0, len(text), 4000):
        await bot.send_message(chat_id, text[i:i + 4000])


async def _think(update: Update, context: ContextTypes.DEFAULT_TYPE, content: list[dict], speak: bool = False) -> None:
    if chat_locks[update.effective_chat.id].locked():
        await update.effective_message.reply_text(
            "⏳ আগের কাজটা চলছে, ম্যাম। ওটা শেষ হলেই এটা করব। এখনই থামাতে চাইলে লিখুন: থামো")
    reply = await run_brain(context.bot, context.application, update.effective_chat.id, content)
    await _send_long(update, reply)
    if speak and os.getenv("VOICE_REPLIES", "on").lower() != "off":
        await _speak(context.bot, update.effective_chat.id, reply)


BTN_PROJECTS, BTN_SHOT, BTN_MODELS, BTN_STOP, BTN_NEW = "📂 Projects", "📸 Screenshot", "🧠 Models", "🛑 থামাও", "🆕 নতুন"
KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_PROJECTS, BTN_SHOT, BTN_STOP]],
    resize_keyboard=True, is_persistent=True, input_field_placeholder="Zolt-কে বলুন...",
)
QUICK_PROMPTS = {BTN_PROJECTS: "amar project gula dekhao", BTN_SHOT: "screenshot dao"}
# Plain words that mean "stop what you're doing" (whole message only).
STOP_WORDS = {"stop", "thamo", "thamao", "ruko", "cancel", "bas", "থামো", "থামাও", "থাম", "বাদ দাও", "stop koro",
              "kaj thamao", "কাজ থামাও"}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return await _reject(update)
    text = update.message.text.strip()
    if text == BTN_STOP or text.lower().strip(" !.।") in STOP_WORDS:
        return await on_stop(update, context)
    if text == BTN_NEW:
        return await on_new(update, context)
    if text == BTN_MODELS:
        return await on_models(update, context)
    await _think(update, context, [{"type": "text", "text": QUICK_PROMPTS.get(text, text)}])


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return await _reject(update)
    photo = update.message.photo[-1]  # largest size
    file = await photo.get_file()
    data = bytes(await file.download_as_bytearray())
    caption = update.message.caption or "এই ছবিটা দেখো।"
    await _think(update, context, [
        {"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(data).decode(),
        }},
        {"type": "text", "text": caption},
    ])


async def _transcribe(data: bytes) -> str:
    """Speech-to-text with Groq's free Whisper."""
    import io

    import litellm

    litellm.suppress_debug_info = True
    audio = io.BytesIO(data)
    audio.name = "voice.ogg"
    result = await litellm.atranscription(
        model=os.getenv("VOICE_MODEL", "groq/whisper-large-v3"),
        file=audio, api_key=os.getenv("GROQ_API_KEY", "").strip(), timeout=120,
    )
    return (result.text or "").strip()


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return await _reject(update)
    if not os.getenv("GROQ_API_KEY", "").strip():
        await update.message.reply_text("🎙️ Voice বুঝতে .env-এ GROQ_API_KEY লাগবে (free)। আপাতত লিখে বলুন, ম্যাম।")
        return
    media = update.message.voice or update.message.audio
    file = await media.get_file()
    data = bytes(await file.download_as_bytearray())
    try:
        text = await _transcribe(data)
    except Exception as e:
        log.warning("Transcription failed: %s", e)
        await update.message.reply_text("🎙️ Voice বুঝতে পারিনি, ম্যাম। আবার বলুন বা লিখে দিন।")
        return
    if not text:
        await update.message.reply_text("🎙️ কিছু শুনতে পাইনি, ম্যাম।")
        return
    await update.message.reply_text(f"🎙️ শুনলাম: {text}")
    await _think(update, context, [{"type": "text", "text": text}], speak=True)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Files she sends: saved to data/inbox, text extracted and handed to the brain."""
    from .documents import INBOX, extract_text
    if not _is_owner(update):
        return await _reject(update)
    doc = update.message.document
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("📎 ফাইলটা ২০MB-এর বেশি, Telegram bot এত বড় ফাইল নামাতে পারে না।")
        return
    INBOX.mkdir(parents=True, exist_ok=True)
    name = Path(doc.file_name or f"file_{doc.file_unique_id}").name
    path = INBOX / name
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(custom_path=str(path))
    text = await asyncio.to_thread(extract_text, path, 20000)
    caption = update.message.caption or "এই ফাইলটা দেখো আর বুঝে নাও, তারপর আমাকে ছোট করে বলো এতে কী আছে।"
    await _think(update, context, [{"type": "text", "text":
        f"{caption}\n\n[She sent a file, saved at {path}]\n--- content ---\n{text}"}])


async def _speak(bot, chat_id: int, text: str) -> None:
    """Send the reply as a Bangla voice note too (free Microsoft Edge voices)."""
    import re
    import tempfile

    import edge_tts
    clean = re.sub(r"[*_`#>\[\]()|~]|https?://\S+", " ", text)
    clean = re.sub(r"[^\w\sঀ-৿.,?!।:-]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()[:700]
    if not clean:
        return
    voice = os.getenv("TTS_VOICE", "bn-BD-NabanitaNeural")
    out = Path(tempfile.gettempdir()) / f"zolt_reply_{chat_id}.mp3"
    try:
        await edge_tts.Communicate(clean, voice).save(str(out))
        with out.open("rb") as f:
            await bot.send_voice(chat_id, f)
    except Exception as e:
        log.warning("Voice reply failed: %s", e)


_task_counter = 0


async def _launch_parallel(ctx: ToolContext, task_text: str) -> int:
    """Run a job on its own conversation, in parallel with the main chat."""
    global _task_counter
    _task_counter += 1
    number = _task_counter
    side_id = -(abs(ctx.chat_id) * 1000 + number)  # separate history in the brain

    async def job():
        side_ctx = ToolContext(bot=ctx.bot, chat_id=ctx.chat_id, approvals=approvals, app=ctx.app)
        try:
            reply = await brain.handle(side_id, [{"type": "text", "text": task_text}], side_ctx)
        except Exception as e:
            log.exception("Parallel task failed")
            reply = f"⚠️ সমস্যা হয়েছে: {e}"
        brain.reset(side_id)
        await _send_text(ctx.bot, ctx.chat_id, f"✅ ব্যাকগ্রাউন্ড কাজ #{number} শেষ:\n\n{reply}")

    await ctx.bot.send_message(ctx.chat_id, f"🧵 ব্যাকগ্রাউন্ড কাজ #{number} শুরু: {task_text[:200]}")
    asyncio.create_task(job())
    return number


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    kind = ("photo" if msg and msg.photo else
            "voice" if msg and (msg.voice or msg.audio) else
            "video-note" if msg and msg.video_note else
            "command" if msg and msg.text and msg.text.startswith("/") else
            "text" if msg and msg.text else
            "button" if update.callback_query else "other")
    log.info("Incoming %s (owner: %s)", kind, _is_owner(update))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Handler error: %s", context.error, exc_info=context.error)


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return await _reject(update)
    await update.message.reply_text(
        f"👋 আসসালামু আলাইকুম ম্যাম! আমি {CONFIG.agent_name}।\n\n"
        "যা বলতে পারেন:\n"
        "• PC off / restart / lock করো\n"
        "• Fiverr বা GitHub খোলো\n"
        "• VS Code বা Laragon খোলো\n"
        "• স্ক্রিনশট দাও\n"
        "• আমার project গুলো দেখাও\n"
        "• <project> push করো\n"
        "• Apollo-র composer.json দেখে Laravel version বলো\n"
        "• web-এ খুঁজে বলো ...\n"
        "• প্রতিদিন সকাল ১০টায় outreach-এর কথা মনে করিয়ে দিও\n"
        "• মনে রাখো: ...\n"
        "• 🎙️ voice message বা 📷 ছবি পাঠান\n\n"
        "কিছু লিখে command দিতে হবে না, নিচের বাটনগুলোই যথেষ্ট 👇\n"
        "৩০ মিনিট কথা না হলে আমি নিজেই নতুন করে শুরু করি।",
        reply_markup=KEYBOARD,
    )


async def on_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return await _reject(update)
    brain.reset(update.effective_chat.id)
    await update.message.reply_text("🆕 নতুন কথোপকথন শুরু।")


async def on_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return await _reject(update)
    chat_id = update.effective_chat.id
    approvals.revoke(chat_id)
    task = running.get(chat_id)
    if task and not task.done():
        task.cancel()
        await update.message.reply_text("🛑 চলমান কাজ থামিয়ে দিলাম, ম্যাম। অটো-অনুমতিও বন্ধ।")
    else:
        await update.message.reply_text("এখন কোনো কাজ চলছে না। অটো-অনুমতি বন্ধ করা হলো।")


async def on_models(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return await _reject(update)
    await update.message.reply_text("🧠 Zolt-এর model গুলো (এই ক্রমে চেষ্টা করে):\n\n" + brain.status())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_owner(update):
        await query.answer("Not allowed")
        return
    _, approval_id, choice = query.data.split(":")
    if approvals.resolve(approval_id, choice):
        await query.answer("ঠিক আছে")
        note = {"y": "✅ অনুমতি দেওয়া হয়েছে", "n": "❌ বাতিল",
                "t": "⚡ ১০ মিনিট সব অনুমতি (থামাতে /stop)"}[choice]
        await query.edit_message_text(f"{query.message.text}\n\n{note}")
    else:
        await query.answer("এই অনুরোধের সময় শেষ")


async def _post_init(app: Application) -> None:
    async def run_task(chat_id: int, text: str) -> None:
        reply = await run_brain(app.bot, app, chat_id, [{"type": "text", "text": text}])
        await _send_text(app.bot, chat_id, reply)

    reminders.task_runner = run_task
    reminders.restore_all(app)
    tools_more.parallel_launcher = _launch_parallel
    monitors.start(app)
    try:
        loaded = await mcp_client.start()
        if loaded:
            log.info("MCP tools loaded: %d", len(loaded))
    except Exception as e:
        log.warning("MCP start failed: %s", e)
    brief = os.getenv("MORNING_BRIEF_TIME", "09:00").strip()
    if brief and CONFIG.allowed_user_id:
        hh, mm = (int(x) for x in brief.split(":"))
        async def morning(context):
            await run_task(CONFIG.allowed_user_id, MORNING_PROMPT)
        app.job_queue.run_daily(morning, dtime(hh, mm, tzinfo=reminders.LOCAL_TZ), name="morning-brief")
    try:  # the "/" menu in Telegram, so nothing has to be typed
        await app.bot.set_my_commands([
            BotCommand("stop", "🛑 চলমান কাজ থামাও"),
            BotCommand("new", "🆕 নতুন করে শুরু"),
            BotCommand("models", "🧠 কোন AI model চালু"),
            BotCommand("start", "👋 কী কী পারি"),
        ])
    except Exception as e:
        log.warning("Could not set command menu: %s", e)
    import time as _time

    import psutil
    just_booted = _time.time() - psutil.boot_time() < 15 * 60
    if CONFIG.allowed_user_id and just_booted:  # announce only after the PC starts, not on every restart
        try:
            await app.bot.send_message(CONFIG.allowed_user_id, f"⚡ {CONFIG.agent_name} চালু হয়েছে, ম্যাম।",
                                       reply_markup=KEYBOARD)
        except Exception as e:
            log.warning("Could not send startup message: %s", e)


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    for noisy in ("httpx", "LiteLLM", "litellm", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if CONFIG.missing():
        raise SystemExit(".env ফাইলে TELEGRAM_BOT_TOKEN দিন  (.env.example দেখুন)")
    models = [p.label for p in brain.configured()]
    if models:
        log.info("AI models (in order): %s", " -> ".join(models))
    else:
        # Still start, so the owner can get her Telegram ID; the brain replies that a key is missing.
        log.warning("কোনো AI model-এর key নেই। .env-এ অন্তত একটা দিন (যেমন GEMINI_API_KEY)।")

    # concurrent_updates lets the approval button arrive while a task is waiting on it.
    app = (Application.builder().token(CONFIG.telegram_token).concurrent_updates(True)
           .post_init(_post_init).build())
    app.add_handler(TypeHandler(Update, log_update), group=-1)
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("new", on_new))
    app.add_handler(CommandHandler("models", on_models))
    app.add_handler(CommandHandler("stop", on_stop))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^ap:"))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("%s is running. Owner ID: %s", CONFIG.agent_name, CONFIG.allowed_user_id or "NOT SET")
    if os.getenv("CLOUD_URL", "").strip():
        asyncio.run(_run_with_cloud(app))   # Telegram -> Cloudflare Worker -> this PC
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES)


async def _run_with_cloud(app: Application) -> None:
    """Telegram's webhook points at the Zolt Cloudflare Worker (always on). While this PC runs we tell the
    Worker every second that we're here and take the updates it holds for us; when the PC is off the
    Worker answers by itself and starts cloud videos."""
    import httpx

    from .memory import read_memory
    url = os.getenv("CLOUD_URL", "").strip().rstrip("/")
    headers = {"x-zolt-token": os.getenv("PC_TOKEN", "").strip()}
    await app.initialize()
    if app.post_init:
        await app.post_init(app)
    await app.start()
    log.info("Connected to Zolt cloud: %s", url)
    last_memory, failures = "", 0
    from collections import deque
    seen_ids: deque = deque(maxlen=2000)
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            try:
                memory = read_memory()
                body = {"memory": memory} if memory != last_memory else {}
                r = await client.post(f"{url}/pc/sync", headers=headers, json=body)
                r.raise_for_status()
                last_memory, failures = memory, 0
                for raw in r.json().get("updates", []):
                    uid = raw.get("update_id")
                    if uid in seen_ids:      # never handle the same message twice
                        continue
                    seen_ids.append(uid)
                    # handle in the background so long jobs never stop the heartbeat
                    asyncio.create_task(app.process_update(Update.de_json(raw, app.bot)))
            except Exception as e:
                failures += 1
                if failures in (1, 10) or failures % 60 == 0:
                    log.warning("Cloud sync failed (%d): %s", failures, e)
            await asyncio.sleep(1.0)
