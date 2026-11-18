"""make_video: turn a story into a vertical (9:16) reel for Facebook/YouTube Shorts.

Everything is free and runs on the PC (no GPU needed):
  pictures  - Pollinations.ai text-to-image (no key)
  voice     - Microsoft Edge neural Bangla voices (edge-tts), with sentence timings for subtitles
  text      - Bangla subtitles / title cards rendered by Edge (Playwright), so Bangla letters join correctly
  video     - ffmpeg (bundled by imageio-ffmpeg): slow zoom on each picture + overlays + voice
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from .memory import DATA_DIR
from .tools import ToolContext, _schema, register_tools

log = logging.getLogger("zolt.video")

VIDEO_DIR = DATA_DIR / "outbox" / "videos"
W, H, FPS = 1080, 1920, 30
VOICES = {"female": "bn-BD-NabanitaNeural", "male": "bn-BD-PradeepNeural"}
DEFAULT_STYLE = ("emotional cinematic digital painting, Bangladeshi characters in everyday Bangladeshi clothes and "
                 "homes, soft dramatic lighting, detailed, vertical composition, no text, no watermark")
FONT = "'Hind Siliguri', 'Nirmala UI', 'Vrinda', sans-serif"


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", *args],
                          capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg: " + (proc.stderr or "")[-600:])


def _duration(path: Path) -> float:
    proc = subprocess.run([_ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", proc.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 5.0


# ---------------------------------------------------------------- pictures

async def _picture(prompt: str, style: str, seed: int, out: Path) -> bool:
    import httpx
    url = (f"https://image.pollinations.ai/prompt/{quote(prompt + ', ' + style)}"
           f"?width={W}&height={H}&nologo=true&seed={seed}")
    # Free use: 1 picture per ~45 s per IP. A free key from enter.pollinations.ai lifts that.
    key = os.getenv("POLLINATIONS_KEY", "").strip()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(timeout=150, follow_redirects=True, headers=headers) as client:
        for attempt in range(8):
            wait = 10.0
            try:
                r = await client.get(url)
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                    out.write_bytes(r.content)
                    return True
                m = re.search(r"[Rr]etry after (\d+)", r.text)
                wait = float(m.group(1)) + 2 if m else 15.0 + attempt * 10
                log.info("Picture %s: HTTP %s, waiting %.0fs", out.name, r.status_code, wait)
            except httpx.HTTPError as e:
                log.warning("Picture attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(min(wait, 90))
    return False


def _data_uri(path: Path) -> str:
    import base64
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()


def _gradient(out: Path) -> None:
    """Offline / picture service down: a plain dark gradient keeps the video going."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        shade = int(25 + 40 * y / H)
        draw.line([(0, y), (W, y)], fill=(shade, shade // 2, shade + 15))
    img.save(out, "JPEG")


# ---------------------------------------------------------------- voice

async def _voice(text: str, voice: str, out: Path) -> list[tuple[float, float, str]]:
    """Save the narration and return sentence timings [(start, end, text)] in seconds."""
    import edge_tts
    for attempt in range(6):  # the free service fails now and then at random
        sentences: list[tuple[float, float, str]] = []
        try:
            with out.open("wb") as f:
                # note: the Bangla voices return no audio when rate/pitch is changed
                async for chunk in edge_tts.Communicate(text, voice).stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                    elif chunk["type"] in ("SentenceBoundary", "WordBoundary"):
                        start = chunk["offset"] / 1e7
                        sentences.append((start, start + chunk["duration"] / 1e7, chunk["text"]))
            return sentences
        except Exception as e:
            if attempt == 5:
                raise
            log.warning("Voice attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(2 + attempt * 2)
    return []


def _subtitle_chunks(narration: str, timings: list[tuple[float, float, str]], total: float,
                     max_chars: int = 42) -> list[tuple[float, float, str]]:
    """Short on-screen lines with start/end times."""
    if not timings:
        timings = [(0.0, total, narration)]
    chunks = []
    for i, (start, end, text) in enumerate(timings):
        end = timings[i + 1][0] if i + 1 < len(timings) else max(end, total)
        words, lines, line = text.split(), [], ""
        for word in words:
            if line and len(line) + 1 + len(word) > max_chars:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        if line:
            lines.append(line)
        # two lines per card; time split by length
        cards = [" ".join(lines[j:j + 2]) for j in range(0, len(lines), 2)] or [text]
        size = sum(len(c) for c in cards) or 1
        t = start
        for card in cards:
            span = (end - start) * len(card) / size
            chunks.append((t, t + span, card))
            t += span
    return chunks


# ---------------------------------------------------------------- text overlays (rendered by Edge)

def _overlay_html(body: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Hind+Siliguri:wght@600;700&display=swap" rel="stylesheet">
<style>html,body{{margin:0;width:{W}px;height:{H}px;background:transparent;font-family:{FONT};overflow:hidden}}
.top{{position:absolute;top:70px;left:0;right:0;text-align:center}}
.series{{display:inline-block;background:rgba(0,0,0,.55);color:#ffd84d;font-size:44px;font-weight:700;
 padding:10px 34px;border-radius:40px}}
.ep{{display:inline-block;margin-left:14px;background:#e53935;color:#fff;font-size:44px;font-weight:700;
 padding:10px 30px;border-radius:40px}}
.sub{{position:absolute;bottom:260px;left:60px;right:60px;text-align:center}}
.sub span{{background:rgba(0,0,0,.62);color:#fff;font-size:62px;font-weight:700;line-height:1.55;
 padding:8px 26px;border-radius:18px;box-decoration-break:clone;-webkit-box-decoration-break:clone;
 text-shadow:0 3px 8px rgba(0,0,0,.6)}}
.cover{{position:absolute;inset:0;background-size:auto 107%;background-position:center top}}
.shade{{position:absolute;inset:0;background:linear-gradient(transparent 45%,rgba(0,0,0,.85))}}
.title{{position:absolute;bottom:300px;left:50px;right:50px;text-align:center;color:#fff;font-size:120px;
 font-weight:700;line-height:1.25;text-shadow:0 6px 18px #000,0 0 4px #000}}
.badge{{position:absolute;bottom:170px;left:0;right:0;text-align:center}}
.badge span{{background:#e53935;color:#fff;font-size:70px;font-weight:700;padding:12px 50px;border-radius:50px}}
</style></head><body>{body}</body></html>"""


async def _render(page, body: str, out: Path, transparent: bool = True) -> None:
    await page.set_content(_overlay_html(body), wait_until="networkidle")
    await page.evaluate("document.fonts.ready")
    await page.screenshot(path=str(out), omit_background=transparent,
                          type="png" if transparent else "jpeg", **({} if transparent else {"quality": 92}))


# ---------------------------------------------------------------- scenes -> video

def _scene_clip(i: int, picture: Path, audio: Path, top: Path | None, subs: list[tuple[float, float, Path]],
                length: float, out: Path) -> None:
    frames = int(length * FPS) + 2
    # alternate slow zoom-in / zoom-out so the story feels alive
    zoom = "min(1+0.0009*on,1.18)" if i % 2 == 0 else "max(1.18-0.0009*on,1)"
    inputs = ["-loop", "1", "-i", str(picture), "-i", str(audio)]
    overlays = ([top] if top else []) + [p for _, _, p in subs]
    for p in overlays:
        inputs += ["-i", str(p)]
    chain = [f"[0:v]crop=iw:ih*0.94:0:0,scale=1620:2880:force_original_aspect_ratio=increase,crop=1620:2880,"
             f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
             f"fade=t=in:st=0:d=0.35[v0]"]
    last, n = "v0", 2
    if top:
        chain.append(f"[{last}][{n}:v]overlay=0:0[v{n}]")
        last, n = f"v{n}", n + 1
    for start, end, _ in subs:
        chain.append(f"[{last}][{n}:v]overlay=0:0:enable='between(t,{start:.2f},{end:.2f})'[v{n}]")
        last, n = f"v{n}", n + 1
    _run_ffmpeg([*inputs, "-filter_complex", ";".join(chain), "-map", f"[{last}]", "-map", "1:a",
                 "-af", "apad", "-t", f"{length:.2f}", "-r", str(FPS),
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "128k", "-ar", "44100", str(out)])


async def make_video(inp: dict, ctx: ToolContext) -> str:
    scenes = [s for s in inp.get("scenes") or [] if (s.get("narration") or "").strip()]
    if not scenes:
        return "No scenes given. Write 6-10 scenes (narration in Bangla + image_prompt in English) and call again."
    words = sum(len(s["narration"].split()) for s in scenes)
    if not inp.get("_short_ok") and (len(scenes) < 6 or words < 120):
        return (f"Script too short for a 1-minute reel: {len(scenes)} scenes, {words} words. Rewrite it with 6-9 "
                "scenes and 140-170 Bangla words of narration in total (keep the story, add detail and emotion), "
                "then call make_video again. If she asked for a shorter video, keep it short and set more scenes "
                "anyway (at least 6).")
    title = (inp.get("title") or "গল্প").strip()
    series = (inp.get("series_name") or "").strip()
    episode = (inp.get("episode") or "").strip()
    style = (inp.get("style") or "").strip() or DEFAULT_STYLE
    voice = VOICES.get((inp.get("voice") or "female").lower(), VOICES["female"])

    work = VIDEO_DIR / time.strftime("%Y%m%d-%H%M%S")
    work.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    if ctx.bot:
        await ctx.bot.send_message(ctx.chat_id, f"🎬 ভিডিও বানানো শুরু করলাম ({len(scenes)}টা দৃশ্য)। "
                                                f"আনুমানিক {len(scenes) * 50 // 60 + 2} মিনিট লাগবে, "
                                                "শেষ হলে এখানেই পাঠিয়ে দেব, ম্যাম।")

    # 1) pictures and voices, in parallel (a few pictures at a time to be polite to the free service)
    seed = random.randint(1, 10**6)
    gate = asyncio.Semaphore(1)  # the free picture service allows one at a time

    async def picture(i: int, s: dict) -> bool:
        async with gate:
            return await _picture((s.get("image_prompt") or title).strip(), style, seed + i, work / f"pic{i}.jpg")

    pictures = asyncio.gather(*(picture(i, s) for i, s in enumerate(scenes)))
    voice_gate = asyncio.Semaphore(2)

    async def speak(i: int, s: dict):
        async with voice_gate:
            return await _voice(s["narration"].strip(), voice, work / f"voice{i}.mp3")

    voices = asyncio.gather(*(speak(i, s) for i, s in enumerate(scenes)))
    got, timings = await asyncio.gather(pictures, voices)
    pics = [work / f"pic{i}.jpg" for i in range(len(scenes))]
    for i, ok in enumerate(got):
        if not ok:  # reuse the nearest picture that did arrive, so the story keeps its look
            near = sorted((j for j, k in enumerate(got) if k), key=lambda j: abs(j - i))
            if near:
                shutil.copy(pics[near[0]], pics[i])
            else:
                _gradient(pics[i])

    # 2) text overlays
    from playwright.async_api import async_playwright
    scene_subs: list[list[tuple[float, float, Path]]] = []
    lengths: list[float] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": W, "height": H})
        top = None
        if series or episode:
            top = work / "top.png"
            await _render(page, '<div class="top">' + (f'<span class="series">{html.escape(series)}</span>' if series else "")
                          + (f'<span class="ep">{html.escape(episode)}</span>' if episode else "") + "</div>", top)
        for i, s in enumerate(scenes):
            audio = work / f"voice{i}.mp3"
            spoken = _duration(audio)
            lengths.append(spoken + 0.45)
            subs = []
            for j, (a, b, text) in enumerate(_subtitle_chunks(s["narration"].strip(), timings[i], spoken)):
                png = work / f"sub{i}_{j}.png"
                await _render(page, f'<div class="sub"><span>{html.escape(text)}</span></div>', png)
                subs.append((a, b, png))
            scene_subs.append(subs)
        cover = work / "cover.jpg"
        await _render(page, f'<div class="cover" style="background-image:url(\'{_data_uri(pics[0])}\')"></div>'
                      '<div class="shade"></div>' + (top and '<div class="top">'
                      + (f'<span class="series">{html.escape(series)}</span>' if series else "") + "</div>" or "")
                      + f'<div class="title">{html.escape(title)}</div>'
                      + (f'<div class="badge"><span>{html.escape(episode)}</span></div>' if episode else ""),
                      cover, transparent=False)
        await browser.close()

    # 3) one clip per scene, then join them
    clips = []
    for i in range(len(scenes)):
        clip = work / f"scene{i}.mp4"
        await asyncio.to_thread(_scene_clip, i, pics[i], work / f"voice{i}.mp3", top, scene_subs[i], lengths[i], clip)
        clips.append(clip)
    (work / "list.txt").write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
    safe = re.sub(r"[^\w\-]+", "-", inp.get("file_name") or "reel", flags=re.UNICODE).strip("-") or "reel"
    final = work / f"{safe}.mp4"
    await asyncio.to_thread(_run_ffmpeg, ["-f", "concat", "-safe", "0", "-i", str(work / "list.txt"),
                                          "-c", "copy", "-movflags", "+faststart", str(final)])
    total = _duration(final)
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
    return (f"Video ready and sent to her on Telegram: {final} ({total:.0f}s, {size_mb:.1f} MB, 1080x1920, "
            f"made in {took}s). Cover image: {cover}. Tell her it is ready; she can post it as a Facebook Reel.")


SCENE = {"type": "object", "additionalProperties": False, "required": ["narration", "image_prompt"],
         "properties": {"narration": {"type": "string", "description": "What the voice says in this scene, in Bangla."},
                        "image_prompt": {"type": "string", "description": "English description of the picture for "
                                         "this scene: who, where, emotion, action. Describe recurring characters "
                                         "the same way in every scene."}}}

register_tools([({
    "name": "make_video",
    "description": "Make a finished vertical video (Facebook Reel / YouTube Short, 1080x1920 mp4) from a story and "
                   "send it plus a thumbnail to her on Telegram: AI pictures per scene, Bangla voiceover, Bangla "
                   "subtitles, series name + episode label, slow zoom motion. Takes a few minutes. For ~60 seconds "
                   "write 6-9 scenes with about 140-170 Bangla words of narration in total; open with a strong hook.",
    "input_schema": _schema({
        "title": {"type": "string", "description": "Short catchy Bangla title for the cover, e.g. 'সহ্য কর'"},
        "series_name": {"type": "string", "description": "Page or series name shown on top, e.g. 'Hidden Chapters'; may be empty"},
        "episode": {"type": "string", "description": "Episode label, e.g. 'পর্ব ১' or 'শেষ পর্ব'; may be empty"},
        "voice": {"type": "string", "enum": ["female", "male"]},
        "style": {"type": "string", "description": "Picture style in English; empty = emotional cinematic painting"},
        "file_name": {"type": "string", "description": "Short English file name without extension"},
        "scenes": {"type": "array", "items": SCENE, "minItems": 1},
    }, ["title", "series_name", "episode", "voice", "style", "file_name", "scenes"]),
}, make_video)])
