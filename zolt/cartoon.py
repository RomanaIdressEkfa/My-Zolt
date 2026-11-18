"""make_cartoon_video: a 2D cartoon story reel drawn by code (cartoon_engine.html).

Characters are vector drawings, so they look exactly the same in every scene (no broken AI
pictures). The model only describes each scene: background, time of day, who is where, their
pose/emotion, props. Voice: free Microsoft Bangla voice; subtitles timed per sentence.
Frames are rendered in Edge (Playwright) and piped into ffmpeg.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from pathlib import Path

from .tools import ToolContext, _schema, register_tools
from .video import VIDEO_DIR, VOICES, _duration, _ffmpeg, _run_ffmpeg, _subtitle_chunks, _voice

log = logging.getLogger("zolt.cartoon")

ENGINE = Path(__file__).with_name("cartoon_engine.html")
W, H, FPS = 1080, 1920, 24
GAP = 0.55  # pause after each scene's narration
CLOTH = {"red": "#d63a3a", "blue": "#2f6fd6", "green": "#2e9e5b", "yellow": "#e8b931", "purple": "#7b4bc4",
         "orange": "#ec7a2a", "white": "#f4f1e8", "brown": "#8a5a36", "pink": "#e86fa6", "teal": "#1f9c9c",
         "black": "#3a3a3a", "grey": "#8b8f96", "maroon": "#8e2436"}
SECOND = {"man": "#8a5a36", "old_man": "#7d878e", "woman": "#e8b931", "old_woman": "#d63a3a", "girl": "#ffffff",
          "boy": "#2f6fd6"}
TYPES = ["man", "woman", "girl", "boy", "old_man", "old_woman"]
BACKGROUNDS = ["home", "kitchen", "street", "village", "shop", "office", "hospital", "school"]
POSES = ["stand", "walk", "sit", "sew", "give", "angry", "cry", "happy", "think", "pray"]
EMOTIONS = ["neutral", "happy", "sad", "angry", "surprised", "crying"]
PROPS = ["rickshaw", "lantern", "rice_plate", "dress", "chador", "money", "phone", "letter", "tea_cup", "medicine",
         "sewing", "school_bag"]


async def make_cartoon_video(inp: dict, ctx: ToolContext) -> str:
    scenes = [s for s in inp.get("scenes") or [] if (s.get("narration") or "").strip()]
    cast_in = inp.get("characters") or []
    if not scenes or not cast_in:
        return "Give characters (the cast) and 6-9 scenes, then call again."
    words = sum(len(s["narration"].split()) for s in scenes)
    if len(scenes) < 6 or words < 120:
        return (f"Script too short for a 1-minute reel: {len(scenes)} scenes, {words} words. Rewrite with 6-9 scenes "
                "and 140-170 Bangla words of narration in total, then call make_cartoon_video again.")
    cast = {}
    for i, c in enumerate(cast_in):
        ctype = c.get("type") if c.get("type") in TYPES else "man"
        cloth = CLOTH.get((c.get("clothes_color") or "").lower(), "#2f6fd6" if ctype in ("man", "boy") else "#2e9e5b")
        if ctype == "old_man":
            cloth = "#f4f1e8"
        cast[c.get("id") or f"c{i}"] = {"type": ctype, "cloth": cloth, "cloth2": SECOND[ctype], "seed": i + 1,
                                        "mustache": ctype == "man"}
    unknown = sorted({ch.get("id") for s in scenes for ch in s.get("characters") or []} - set(cast))
    if unknown:
        return f"These scene character ids are not in characters: {', '.join(unknown)}. Use the cast ids exactly."

    title = (inp.get("title") or "গল্প").strip()
    series = (inp.get("series_name") or "").strip()
    episode = (inp.get("episode") or "").strip()
    voice = VOICES.get((inp.get("voice") or "female").lower(), VOICES["female"])
    work = VIDEO_DIR / ("cartoon-" + time.strftime("%Y%m%d-%H%M%S"))
    work.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    if ctx.bot:
        await ctx.bot.send_message(ctx.chat_id, f"🎬 কার্টুন ভিডিও বানানো শুরু করলাম ({len(scenes)}টা দৃশ্য)। "
                                                "আনুমানিক ৩-৪ মিনিট লাগবে, শেষ হলে এখানেই পাঠাব, ম্যাম।")

    # 1) voices (2 at a time; the free service fails now and then and _voice retries)
    gate = asyncio.Semaphore(2)

    async def speak(i: int, s: dict):
        async with gate:
            return await _voice(s["narration"].strip(), voice, work / f"voice{i}.mp3")

    timings = await asyncio.gather(*(speak(i, s) for i, s in enumerate(scenes)))

    # 2) timeline
    t, subs, timeline = 0.0, [], []
    for i, s in enumerate(scenes):
        spoken = _duration(work / f"voice{i}.mp3")
        length = spoken + GAP + (0.6 if i == 0 else 0)   # a beat for the title on the first scene
        offset = t + (0.6 if i == 0 else 0)
        for a, b, text in _subtitle_chunks(s["narration"].strip(), timings[i], spoken):
            subs.append([round(offset + a, 2), round(offset + b, 2), text])
        timeline.append({"start": round(t, 3), "end": round(t + length, 3), "audio_offset": offset,
                         "background": s.get("background") if s.get("background") in BACKGROUNDS else "home",
                         "time": s.get("time_of_day") if s.get("time_of_day") in ("day", "evening", "night") else "day",
                         "camera": s.get("camera") or "zoom_in", "weather": s.get("weather") or "none",
                         "props": [p for p in s.get("props") or [] if p in PROPS],
                         "characters": [{"id": ch.get("id"), "pose": ch.get("pose") if ch.get("pose") in POSES else "stand",
                                         "emotion": ch.get("emotion") if ch.get("emotion") in EMOTIONS else "neutral",
                                         "position": ch.get("position") or "center", "facing": ch.get("facing") or "right",
                                         "talking": bool(ch.get("talking"))} for ch in (s.get("characters") or [])[:3]]})
        t += length
    total = t
    data = {"cast": cast, "scenes": timeline, "subs": subs, "title": title, "episode": episode, "series": series,
            "total": total}

    # 3) audio track: each scene's voice placed at its offset
    inputs, chain = [], []
    for i, sc in enumerate(timeline):
        inputs += ["-i", str(work / f"voice{i}.mp3")]
        ms = int(sc["audio_offset"] * 1000)
        chain.append(f"[{i}:a]adelay={ms}|{ms}[a{i}]")
    chain.append("".join(f"[a{i}]" for i in range(len(timeline))) + f"amix=inputs={len(timeline)}:normalize=0,"
                 f"apad=whole_dur={total:.2f}[aout]")
    audio = work / "audio.m4a"
    await asyncio.to_thread(_run_ffmpeg, [*inputs, "-filter_complex", ";".join(chain), "-map", "[aout]",
                                          "-t", f"{total:.2f}", "-c:a", "aac", "-b:a", "160k", str(audio)])

    # 4) frames -> ffmpeg
    safe = re.sub(r"[^\w\-]+", "-", inp.get("file_name") or "cartoon", flags=re.UNICODE).strip("-") or "cartoon"
    final = work / f"{safe}.mp4"
    cover = work / "cover.jpg"
    proc = subprocess.Popen([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                             "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-",
                             "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast",
                             "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest",
                             "-movflags", "+faststart", str(final)], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": W, "height": H})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(ENGINE.as_uri(), wait_until="networkidle")
        await page.evaluate("document.fonts.ready")
        await page.evaluate(f"setup({json.dumps(data, ensure_ascii=False)})")
        frames = int(total * FPS)
        try:
            for f in range(frames):
                await page.evaluate(f"render({f / FPS:.4f})")
                shot = await page.screenshot(type="jpeg", quality=90)
                await asyncio.to_thread(proc.stdin.write, shot)
        finally:
            proc.stdin.close()
        await page.evaluate("render(1.2, true)")
        await page.screenshot(path=str(cover), type="jpeg", quality=92)
        await browser.close()
    code = await asyncio.to_thread(proc.wait)
    if code != 0 or errors:
        raise RuntimeError(f"render failed: {(proc.stderr.read() or b'').decode()[-400:]} {errors[:2]}")
    size_mb = final.stat().st_size / 1e6

    if ctx.bot:
        with cover.open("rb") as f:
            await ctx.bot.send_photo(ctx.chat_id, f, caption="🖼️ Thumbnail / cover")
        if size_mb < 49:
            with final.open("rb") as f, cover.open("rb") as thumb:
                await ctx.bot.send_video(ctx.chat_id, f, caption=f"🎬 {title}" + (f" — {episode}" if episode else ""),
                                         width=W, height=H, duration=int(total), supports_streaming=True,
                                         thumbnail=thumb, read_timeout=300, write_timeout=300)
    took = int(time.monotonic() - started)
    return (f"Cartoon video ready and sent to her on Telegram: {final} ({total:.0f}s, {size_mb:.1f} MB, made in "
            f"{took}s). Cover: {cover}. Tell her it is ready to post as a Facebook Reel.")


_CHAR = {"type": "object", "additionalProperties": False, "required": ["id", "type", "clothes_color"],
         "properties": {"id": {"type": "string", "description": "short English id used in scenes, e.g. father"},
                        "type": {"type": "string", "enum": TYPES},
                        "clothes_color": {"type": "string", "enum": list(CLOTH)}}}
_IN_SCENE = {"type": "object", "additionalProperties": False,
             "required": ["id", "pose", "emotion", "position", "facing", "talking"],
             "properties": {"id": {"type": "string"}, "pose": {"type": "string", "enum": POSES},
                            "emotion": {"type": "string", "enum": EMOTIONS},
                            "position": {"type": "string", "enum": ["left", "center", "right"]},
                            "facing": {"type": "string", "enum": ["left", "right"]},
                            "talking": {"type": "boolean", "description": "true while this character's words are the narration"}}}
_SCENE = {"type": "object", "additionalProperties": False,
          "required": ["narration", "background", "time_of_day", "characters", "props", "camera"],
          "properties": {"narration": {"type": "string", "description": "What the voice says in this scene (Bangla)."},
                         "background": {"type": "string", "enum": BACKGROUNDS},
                         "time_of_day": {"type": "string", "enum": ["day", "evening", "night"]},
                         "characters": {"type": "array", "items": _IN_SCENE, "maxItems": 3,
                                        "description": "1-3 characters; put them at different positions, facing each other"},
                         "props": {"type": "array", "items": {"type": "string", "enum": PROPS}},
                         "camera": {"type": "string", "enum": ["zoom_in", "zoom_out", "pan_left", "pan_right", "still"]}}}

register_tools([({
    "name": "make_cartoon_video",
    "description": "Her preferred video style. Make a 2D cartoon story reel (Facebook Reel / YouTube Short, 1080x1920 "
                   "mp4): code-drawn Bangladeshi cartoon characters that look the same in every scene, home/kitchen/"
                   "street/village/shop/office/hospital/school backgrounds, poses and emotions (walk, cry with tears, "
                   "angry, happy, sew, give...), talking mouths, Bangla voiceover and subtitles, title card and cover. "
                   "Sends video + cover to her on Telegram (~3-4 minutes). For ~60 s write 6-9 scenes, 140-170 Bangla "
                   "words in total, strong hook first, emotional last line.",
    "input_schema": _schema({
        "title": {"type": "string", "description": "Short catchy Bangla title"},
        "series_name": {"type": "string", "description": "Page/series name on top, e.g. Hidden Chapters"},
        "episode": {"type": "string", "description": "e.g. 'পর্ব ১'; may be empty"},
        "voice": {"type": "string", "enum": ["female", "male"]},
        "file_name": {"type": "string", "description": "Short English file name"},
        "characters": {"type": "array", "items": _CHAR, "minItems": 1, "maxItems": 6},
        "scenes": {"type": "array", "items": _SCENE, "minItems": 1},
    }, ["title", "series_name", "episode", "voice", "file_name", "characters", "scenes"]),
}, make_cartoon_video)])
