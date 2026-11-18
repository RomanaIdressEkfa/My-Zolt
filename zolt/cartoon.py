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
import os
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


LENGTHS = {"1 minute": (6, 130), "2 minutes": (11, 260), "3 minutes": (16, 390)}  # min scenes, min words
MALE = {"man", "boy", "old_man"}


async def _eleven(text: str, voice_id: str, out: Path) -> list[tuple[float, float, str]]:
    """ElevenLabs voice (natural, free tier ~10k characters a month). Returns sentence timings."""
    import base64

    import httpx
    model = os.getenv("ELEVENLABS_MODEL", "eleven_v3")
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps",
                              params={"output_format": "mp3_44100_128"},
                              headers={"xi-api-key": os.getenv("ELEVENLABS_API_KEY", "").strip()},
                              json={"text": text, "model_id": model})
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs {r.status_code}: {r.text[:200]}")
    body = r.json()
    out.write_bytes(base64.b64decode(body["audio_base64"]))
    al = body.get("alignment") or {}
    chars, starts, ends = al.get("characters") or [], al.get("character_start_times_seconds") or [], \
        al.get("character_end_times_seconds") or []
    sentences, buf, s0 = [], "", None
    for ch, a, b in zip(chars, starts, ends):
        s0 = a if s0 is None else s0
        buf += ch
        if ch in "।?!.":
            sentences.append((s0, b, buf.strip()))
            buf, s0 = "", None
    if buf.strip() and s0 is not None:
        sentences.append((s0, ends[-1], buf.strip()))
    return sentences


# Google Gemini TTS: natural, emotional voices that speak Bangla; free with her Gemini key.
GEMINI_TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts",
                     "gemini-3.8-flash-lite-tts"]
GEMINI_VOICES = {"man": ["Charon", "Orus"], "woman": ["Kore", "Aoede"], "girl": ["Leda", "Despina"],
                 "boy": ["Puck", "Achird"], "old_man": ["Algenib", "Rasalgethi"], "old_woman": ["Gacrux", "Vindemiatrix"],
                 "narrator_female": ["Sulafat"], "narrator_male": ["Iapetus"]}
STYLE = {"neutral": "naturally", "happy": "happily, smiling", "sad": "sadly, with a heavy heart",
         "angry": "angrily, raising the voice", "surprised": "with shock and surprise",
         "crying": "while crying, voice breaking", "narrator": "like a warm, emotional storyteller"}
_gemini_dead: set[str] = set()   # models whose quota ran out in this run


async def _gemini_tts(text: str, voice: str, style: str, out: Path) -> list:
    import base64
    import wave

    import httpx
    key = os.getenv("GEMINI_API_KEY", "").strip()
    # Only the line itself: these models read any instruction aloud and don't accept a system prompt.
    # The emotion comes from the words; `style` is kept for voices that support it later.
    body = {"contents": [{"parts": [{"text": text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}
    async with httpx.AsyncClient(timeout=120) as client:
        for model in [m for m in GEMINI_TTS_MODELS if m not in _gemini_dead]:
            for attempt in range(3):
                r = await client.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                      params={"key": key}, json=body)
                if r.status_code == 200:
                    try:
                        pcm = base64.b64decode(r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
                    except (KeyError, IndexError):
                        break   # no audio in the answer: try the next model
                    with wave.open(str(out), "wb") as w:
                        w.setnchannels(1)
                        w.setsampwidth(2)
                        w.setframerate(24000)
                        w.writeframes(pcm)
                    return []   # no word timings; subtitles are spread over the line
                delay = re.search(r'"retryDelay":\s*"(\d+)', r.text)
                per_minute = "PerDay" not in r.text and (delay is None or int(delay.group(1)) <= 60)
                if r.status_code == 429 and attempt < 2 and per_minute:
                    await asyncio.sleep((int(delay.group(1)) if delay else 20) + 1)   # per-minute limit: wait it out
                    continue
                if r.status_code == 429:
                    _gemini_dead.add(model)   # daily quota used up
                log.warning("Gemini TTS %s: HTTP %s", model, r.status_code)
                break
    raise RuntimeError("all Gemini voices unavailable")


async def make_cartoon_video(inp: dict, ctx: ToolContext) -> str:
    scenes = [s for s in inp.get("scenes") or [] if any((ln.get("text") or "").strip() for ln in s.get("lines") or [])]
    cast_in = inp.get("characters") or []
    if not scenes or not cast_in:
        return "Give characters (the cast) and the scenes with their lines, then call again."
    length_key = inp.get("length") if inp.get("length") in LENGTHS else "2 minutes"
    min_scenes, min_words = LENGTHS[length_key]
    words = sum(len((ln.get("text") or "").split()) for s in scenes for ln in s.get("lines") or [])
    if len(scenes) < min_scenes or words < min_words:
        return (f"Script too short for {length_key}: {len(scenes)} scenes, {words} words. Rewrite with at least "
                f"{min_scenes} scenes and about {min_words + 30} Bangla words in total (keep the story, add dialogue, "
                "detail and emotion), then call make_cartoon_video again.")
    cast = {}
    for i, c in enumerate(cast_in):
        ctype = c.get("type") if c.get("type") in TYPES else "man"
        cloth = CLOTH.get((c.get("clothes_color") or "").lower(), "#2f6fd6" if ctype in ("man", "boy") else "#2e9e5b")
        if ctype == "old_man":
            cloth = "#f4f1e8"
        cast[c.get("id") or f"c{i}"] = {"type": ctype, "cloth": cloth, "cloth2": SECOND[ctype], "seed": i + 1,
                                        "mustache": ctype == "man", "name": (c.get("name") or "").strip()}
    used = {ch.get("id") for s in scenes for ch in s.get("characters") or []} | \
           {ln.get("speaker") for s in scenes for ln in s.get("lines") or []}
    unknown = sorted(x for x in used - set(cast) - {"narrator", None})
    if unknown:
        return f"These ids are not in characters: {', '.join(unknown)}. Use the cast ids exactly (or 'narrator')."

    title = (inp.get("title") or "গল্প").strip()
    series = (inp.get("series_name") or "").strip()
    episode = (inp.get("episode") or "").strip()
    narrator_gender = "male" if (inp.get("voice") or "").lower() == "male" else "female"
    work = VIDEO_DIR / ("cartoon-" + time.strftime("%Y%m%d-%H%M%S"))
    work.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    eleven = bool(os.getenv("ELEVENLABS_API_KEY", "").strip())
    if ctx.bot:
        await ctx.bot.send_message(ctx.chat_id, f"🎬 কার্টুন ভিডিও ({length_key}, {len(scenes)}টা দৃশ্য) বানানো শুরু করলাম। "
                                                f"আনুমানিক {3 + 2 * list(LENGTHS).index(length_key)} মিনিট লাগবে, ম্যাম।")

    def gender(speaker: str) -> str:
        return ("male" if cast[speaker]["type"] in MALE else "female") if speaker in cast else narrator_gender

    # 1) one voice clip per line, in the speaker's voice
    jobs = [(si, li, ln) for si, s in enumerate(scenes) for li, ln in enumerate(s.get("lines") or [])
            if (ln.get("text") or "").strip()]
    gate = asyncio.Semaphore(2)
    eleven_ok = [eleven]

    # every character gets their own Google voice (a second man gets another man's voice, etc.)
    gvoice, seen = {}, {}
    for cid, c in cast.items():
        options = GEMINI_VOICES[c["type"]]
        gvoice[cid] = options[seen.get(c["type"], 0) % len(options)]
        seen[c["type"]] = seen.get(c["type"], 0) + 1
    gvoice["narrator"] = GEMINI_VOICES[f"narrator_{narrator_gender}"][0]
    gemini_ok = [bool(os.getenv("GEMINI_API_KEY", "").strip())]

    source: dict[tuple[int, int], str] = {}

    def plausible(path: Path, text: str) -> bool:
        """Catch empty / runaway clips: Bangla speech is roughly 0.25-1.1 s per word."""
        n = max(1, len(text.split()))
        return 0.25 * n <= _duration(path) <= 1.1 * n + 3

    async def speak(si: int, li: int, ln: dict, force_free: bool = False):
        out = work / f"v{si}_{li}.mp3"
        text = ln["text"].strip()
        speaker = ln.get("speaker") if ln.get("speaker") in cast else "narrator"
        g = gender(speaker)
        mood = next((ch.get("emotion") for ch in scenes[si].get("characters") or [] if ch.get("id") == speaker), "neutral")
        if (next((ch.get("pose") for ch in scenes[si].get("characters") or [] if ch.get("id") == speaker), "") == "cry"):
            mood = "crying"
        style = STYLE["narrator"] if speaker == "narrator" else STYLE.get(mood, STYLE["neutral"])
        async with gate:
            if force_free:
                source[(si, li)] = "free"
                return await _voice(text, VOICES[g], out)
            if eleven_ok[0]:
                vid = os.getenv(f"ELEVENLABS_VOICE_{g.upper()}", "") or \
                    {"male": "pNInz6obpgDQGcFmaJgB", "female": "21m00Tcm4TlvDq8ikWAM"}[g]
                try:
                    tm = await _eleven(text, vid, out)
                    if plausible(out, text):
                        source[(si, li)] = "eleven"
                        return tm
                    log.warning("ElevenLabs clip looks wrong (%.1fs); trying another voice", _duration(out))
                except Exception as e:
                    log.warning("ElevenLabs failed, using the free voice from now on: %s", e)
                    eleven_ok[0] = False
            if gemini_ok[0]:
                try:
                    for _ in range(2):
                        tm = await _gemini_tts(text, gvoice[speaker], style, out)
                        if plausible(out, text):
                            source[(si, li)] = "google"
                            return tm
                        log.warning("Google clip looks wrong (%.1fs), retrying", _duration(out))
                except Exception as e:
                    log.warning("Google voices unavailable, using Microsoft voices: %s", e)
                    gemini_ok[0] = False
            source[(si, li)] = "free"
            return await _voice(text, VOICES[g], out)

    timings = await asyncio.gather(*(speak(si, li, ln) for si, li, ln in jobs))
    # One voice per character for the whole video: if a quota ran out midway, redo that
    # character's other lines in the fallback voice too, so nobody's voice changes.
    def who(si: int, li: int) -> str:
        sp = scenes[si]["lines"][li].get("speaker")
        return sp if sp in cast else "narrator"
    mixed = {who(si, li) for si, li, _ in jobs if source.get((si, li)) == "free"}
    redo = [k for k, (si, li, ln) in enumerate(jobs) if who(si, li) in mixed and source.get((si, li)) != "free"]
    if redo:
        log.info("Re-voicing %d lines so each character keeps one voice", len(redo))
        again = await asyncio.gather(*(speak(*jobs[k], force_free=True) for k in redo))
        timings = list(timings)
        for k, tm in zip(redo, again):
            timings[k] = tm
    by_line = {(si, li): tm for (si, li, _), tm in zip(jobs, timings)}

    # 2) timeline: lines one after another, a short breath between them
    t, timeline, clips = 0.0, [], []
    for si, s in enumerate(scenes):
        start = t
        t += 0.9 if si == 0 else 0.25        # room for the title card / scene fade-in
        lines = []
        for li, ln in enumerate(s.get("lines") or []):
            if (si, li) not in by_line:
                continue
            path = work / f"v{si}_{li}.mp3"
            spoken = _duration(path)
            speaker = ln.get("speaker") if ln.get("speaker") in cast else "narrator"
            chunks = [[round(t + a, 2), round(t + b + .12, 2), txt] for a, b, txt in
                      _subtitle_chunks(ln["text"].strip(), by_line[(si, li)], spoken, max_chars=34)]
            lines.append({"speaker": speaker, "start": round(t, 2), "end": round(t + spoken + .2, 2), "chunks": chunks})
            clips.append((path, t))
            t += spoken + 0.3
        t += 0.35
        timeline.append({"start": round(start, 3), "end": round(t, 3), "lines": lines,
                         "background": s.get("background") if s.get("background") in BACKGROUNDS else "home",
                         "time": s.get("time_of_day") if s.get("time_of_day") in ("day", "evening", "night") else "day",
                         "camera": s.get("camera") or "zoom_in", "weather": s.get("weather") or "none",
                         "props": [p for p in s.get("props") or [] if p in PROPS],
                         "characters": [{"id": ch.get("id"), "pose": ch.get("pose") if ch.get("pose") in POSES else "stand",
                                         "emotion": ch.get("emotion") if ch.get("emotion") in EMOTIONS else "neutral",
                                         "position": ch.get("position") or "center", "facing": ch.get("facing") or "right"}
                                        for ch in (s.get("characters") or [])[:3]]})
    total = t
    data = {"cast": cast, "scenes": timeline, "subs": [], "title": title, "episode": episode, "series": series,
            "total": total}

    # 3) audio track: every clip placed at its start time
    inputs, chain = [], []
    for k, (path, at) in enumerate(clips):
        inputs += ["-i", str(path)]
        ms = int(at * 1000)
        chain.append(f"[{k}:a]aresample=44100,adelay={ms}|{ms}[a{k}]")
    chain.append("".join(f"[a{k}]" for k in range(len(clips))) + f"amix=inputs={len(clips)}:normalize=0,"
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


_CHAR = {"type": "object", "additionalProperties": False, "required": ["id", "type", "name", "clothes_color"],
         "properties": {"id": {"type": "string", "description": "short English id used in scenes, e.g. father"},
                        "type": {"type": "string", "enum": TYPES},
                        "name": {"type": "string", "description": "Bangla name or role shown on speech bubbles, e.g. 'বাবা', 'মীরা'"},
                        "clothes_color": {"type": "string", "enum": list(CLOTH)}}}
_IN_SCENE = {"type": "object", "additionalProperties": False,
             "required": ["id", "pose", "emotion", "position", "facing"],
             "properties": {"id": {"type": "string"}, "pose": {"type": "string", "enum": POSES},
                            "emotion": {"type": "string", "enum": EMOTIONS},
                            "position": {"type": "string", "enum": ["left", "center", "right"]},
                            "facing": {"type": "string", "enum": ["left", "right"]}}}
_LINE = {"type": "object", "additionalProperties": False, "required": ["speaker", "text"],
         "properties": {"speaker": {"type": "string", "description": "'narrator' or the id of a character IN this scene"},
                        "text": {"type": "string", "description": "Bangla. Characters speak natural spoken dialogue; "
                                 "the narrator only bridges (short). Keep each line under ~20 words."}}}
_SCENE = {"type": "object", "additionalProperties": False,
          "required": ["lines", "background", "time_of_day", "characters", "props", "camera"],
          "properties": {"lines": {"type": "array", "items": _LINE, "minItems": 1,
                                   "description": "What is said in this scene, in order: mostly the characters' own "
                                                  "dialogue (shown as speech bubbles), a little narrator"},
                         "background": {"type": "string", "enum": BACKGROUNDS},
                         "time_of_day": {"type": "string", "enum": ["day", "evening", "night"]},
                         "characters": {"type": "array", "items": _IN_SCENE, "maxItems": 3,
                                        "description": "1-3 characters; different positions, facing each other"},
                         "props": {"type": "array", "items": {"type": "string", "enum": PROPS}},
                         "camera": {"type": "string", "enum": ["zoom_in", "zoom_out", "pan_left", "pan_right", "still"]}}}

register_tools([({
    "name": "make_cartoon_video",
    "description": "Her preferred video style. Make a 2D cartoon story reel (1080x1920 mp4 for Facebook Reels / "
                   "YouTube Shorts): code-drawn Bangladeshi cartoon characters that look the same in every scene, "
                   "home/kitchen/street/village/shop/office/hospital/school backgrounds, poses and emotions (walk, cry "
                   "with tears, angry, happy, sew, give...). The story is told mainly through the characters' DIALOGUE "
                   "in comic speech bubbles, each character in their own voice (male/female), with short narrator "
                   "lines in a story box. Title card + cover; sent to her on Telegram. Length: 1 minute = 6-9 scenes "
                   "~160 words; 2 minutes = 11-16 scenes ~300 words; 3 minutes = 16-24 scenes ~430 words. Strong "
                   "hook first, emotional ending.",
    "input_schema": _schema({
        "title": {"type": "string", "description": "Short catchy Bangla title"},
        "series_name": {"type": "string", "description": "Page/series name on top, e.g. Hidden Chapters"},
        "episode": {"type": "string", "description": "e.g. 'পর্ব ১'; may be empty"},
        "length": {"type": "string", "enum": list(LENGTHS), "description": "default 1 minute unless she asks"},
        "voice": {"type": "string", "enum": ["female", "male"], "description": "narrator's voice"},
        "file_name": {"type": "string", "description": "Short English file name"},
        "characters": {"type": "array", "items": _CHAR, "minItems": 1, "maxItems": 6},
        "scenes": {"type": "array", "items": _SCENE, "minItems": 1},
    }, ["title", "series_name", "episode", "length", "voice", "file_name", "characters", "scenes"]),
}, make_cartoon_video)])
