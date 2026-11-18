"""Tools Zolt can use on this PC. Each tool: a JSON schema for Claude + an async handler."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import Bot

from .approvals import Approvals
from .config import CONFIG


@dataclass
class ToolContext:
    bot: Bot
    chat_id: int
    approvals: Approvals
    tools_run: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)  # "tool(args) -> result" lines, for handing a task to another model
    app: Any = None  # telegram Application, for reminders

    async def confirm(self, question: str, allow_trust: bool = True) -> bool:
        return await self.approvals.ask(self.bot, self.chat_id, question, allow_trust)


DECLINED = "The owner declined this action (or did not answer in time). Do not retry it unless she asks again."


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


async def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 180) -> tuple[int, str]:
    """Run and wait. On timeout the whole process tree is killed (no orphaned servers)."""
    def _go() -> tuple[int, str]:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            out, _ = proc.communicate(timeout=timeout)
            return proc.returncode, (out or "").strip()
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            out, _ = proc.communicate()
            return -1, ((out or "").strip()[-3000:] + f"\n[Stopped after {timeout}s - it was still running. "
                        "If this is a server/watcher/GUI app, run it with background=true.]")
    return await asyncio.to_thread(_go)


def _start_background(command: str, cwd: Path) -> int:
    """Start a long-running command (dev server, watcher, GUI app) in its own window and return its PID."""
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NoExit", "-Command", command], cwd=cwd,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return proc.pid


def _project_path(name: str) -> Path:
    """Resolve a project name/path and make sure it stays inside PROJECTS_DIR."""
    base = CONFIG.projects_dir.resolve()
    candidate = Path(name)
    path = (candidate if candidate.is_absolute() else base / candidate).resolve()
    if path != base and base not in path.parents:
        raise ValueError(f"'{name}' is outside the projects folder {base}")
    if not path.is_dir():
        raise ValueError(f"Project folder not found: {path}")
    return path


# ---------------------------------------------------------------- PC power

async def power_action(inp: dict, ctx: ToolContext) -> str:
    action = inp["action"]
    delay = max(10, min(int(inp.get("delay_seconds", 30)), 3600))
    commands = {
        "shutdown": ["shutdown", "/s", "/t", str(delay)],
        "restart": ["shutdown", "/r", "/t", str(delay)],
        "cancel": ["shutdown", "/a"],
        "lock": ["rundll32.exe", "user32.dll,LockWorkStation"],
        "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
    }
    labels = {"shutdown": "PC বন্ধ (shutdown)", "restart": "PC restart", "sleep": "PC sleep"}
    if action in labels:
        when = f" {delay} সেকেন্ড পরে" if action in ("shutdown", "restart") else ""
        if not await ctx.confirm(f"{labels[action]} করব{when}?\nবন্ধ হলে Zolt-ও বন্ধ হয়ে যাবে।", allow_trust=False):
            return DECLINED
    code, out = await _run(commands[action])
    if code != 0:
        return f"Failed ({code}): {out}"
    return f"Done: {action}" + (f" in {delay} seconds" if action in ("shutdown", "restart") else "")


# ---------------------------------------------------------------- open things

async def open_website(inp: dict, ctx: ToolContext) -> str:
    url = inp["url"].strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    ok = await asyncio.to_thread(webbrowser.open, url)
    return f"Opened {url}" if ok else f"Could not open {url}"


async def open_app(inp: dict, ctx: ToolContext) -> str:
    name = inp["name"].strip().lower()
    target = CONFIG.apps.get(name)
    if target is None:
        return f"Unknown app '{name}'. Known apps: {', '.join(sorted(CONFIG.apps))}"
    try:
        # `start` resolves App Paths (chrome, msedge, code) like the Run dialog does.
        await asyncio.to_thread(
            subprocess.Popen, ["cmd", "/c", "start", "", target],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except OSError as e:
        return f"Failed to open {name}: {e}"
    return f"Opened {name}"


async def open_folder(inp: dict, ctx: ToolContext) -> str:
    try:
        path = _project_path(inp["project"])
    except ValueError as e:
        return str(e)
    if inp.get("in_vscode"):
        await asyncio.to_thread(
            subprocess.Popen, ["cmd", "/c", "code", str(path)],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return f"Opened {path} in VS Code"
    os.startfile(path)  # noqa: S606 - opens in Explorer
    return f"Opened {path} in Explorer"


# ---------------------------------------------------------------- screenshot

async def take_screenshot(inp: dict, ctx: ToolContext) -> str:
    from PIL import ImageGrab

    def _grab() -> Path:
        img = ImageGrab.grab(all_screens=True)
        img.thumbnail((2560, 2560))
        path = Path(tempfile.gettempdir()) / "zolt_screenshot.png"
        img.save(path)
        return path

    path = await asyncio.to_thread(_grab)
    with path.open("rb") as f:
        await ctx.bot.send_photo(ctx.chat_id, f, caption="🖥️ এখনকার স্ক্রিন")
    return "Screenshot sent to the owner on Telegram."


# ---------------------------------------------------------------- projects & git

async def list_projects(inp: dict, ctx: ToolContext) -> str:
    base = CONFIG.projects_dir
    if not base.is_dir():
        return f"Projects folder not found: {base}"
    rows = []
    for p in sorted(base.iterdir()):
        if p.is_dir() and not p.name.startswith("."):
            rows.append(f"{p.name}{'  [git]' if (p / '.git').exists() else ''}")
    return f"Projects in {base}:\n" + "\n".join(rows)


async def git_status(inp: dict, ctx: ToolContext) -> str:
    try:
        path = _project_path(inp["project"])
    except ValueError as e:
        return str(e)
    code, out = await _run(["git", "status", "-sb"], cwd=path)
    if code != 0:
        return f"git status failed: {out}"
    _, log = await _run(["git", "log", "--oneline", "-5"], cwd=path)
    return f"{path}\n{out}\n\nRecent commits:\n{log}"


async def git_push(inp: dict, ctx: ToolContext) -> str:
    try:
        path = _project_path(inp["project"])
    except ValueError as e:
        return str(e)
    if not (path / ".git").exists():
        return f"{path} is not a git repository."

    _, changes = await _run(["git", "status", "--porcelain"], cwd=path)
    _, branch = await _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    files = changes.splitlines()
    preview = "\n".join(files[:15]) + (f"\n... আরও {len(files) - 15}টা" if len(files) > 15 else "")
    message = inp["commit_message"].strip()

    question = (
        f"📦 Project: {path.name}\n🌿 Branch: {branch}\n📝 Commit: {message}\n\n"
        f"পরিবর্তিত ফাইল ({len(files)}):\n{preview or '(নতুন কোনো পরিবর্তন নেই, শুধু push)'}\n\n"
        "GitHub-এ push করব?"
    )
    if not await ctx.confirm(question):
        return DECLINED

    log = []
    if files:
        for cmd in (["git", "add", "-A"], ["git", "commit", "-m", message]):
            code, out = await _run(cmd, cwd=path)
            log.append(f"$ {' '.join(cmd)}\n{out}")
            if code != 0:
                return "Failed:\n" + "\n\n".join(log)
    code, out = await _run(["git", "push"], cwd=path, timeout=300)
    log.append(f"$ git push\n{out}")
    return ("Pushed successfully:\n" if code == 0 else "Push failed:\n") + "\n\n".join(log)


# ---------------------------------------------------------------- shell

async def run_command(inp: dict, ctx: ToolContext) -> str:
    command = inp["command"].strip()
    try:
        cwd = _project_path(inp["project"]) if inp.get("project") else CONFIG.projects_dir
    except ValueError as e:
        return str(e)
    background = bool(inp.get("background"))
    label = "আলাদা window-এ চালু করব (চলতে থাকবে)" if background else "চালাব"
    if not await ctx.confirm(f"💻 এই command {label}?\n📁 {cwd}\n\n{command}"):
        return DECLINED
    if background:
        pid = await asyncio.to_thread(_start_background, command, cwd)
        return f"Started in its own window (PID {pid}); it keeps running. Do not wait for it."
    code, out = await _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                           cwd=cwd, timeout=180)
    if len(out) > 12000:
        out = out[:4000] + "\n...[cut]...\n" + out[-8000:]
    return f"exit code {code}\n{out or '(no output)'}"


# ---------------------------------------------------------------- files

def _safe_path(raw: str) -> Path:
    """Files may live in the projects folder or the user's home folder."""
    path = Path(os.path.expandvars(raw)).expanduser()
    if not path.is_absolute():
        path = CONFIG.projects_dir / path
    path = path.resolve()
    roots = [CONFIG.projects_dir.resolve(), Path.home().resolve()]
    if not any(path == r or r in path.parents for r in roots):
        raise ValueError(f"{path} is outside the allowed folders ({', '.join(map(str, roots))})")
    if any(part.lower() in (".env", ".ssh", ".git") for part in path.parts[-2:]) or path.name.startswith(".env"):
        raise ValueError("Secret/config files (.env, .ssh, .git internals) are off limits.")
    return path


async def list_dir(inp: dict, ctx: ToolContext) -> str:
    try:
        path = _safe_path(inp["path"])
    except ValueError as e:
        return str(e)
    if not path.is_dir():
        return f"Not a folder: {path}"
    rows = []
    for p in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:300]:
        rows.append(f"{p.name}/" if p.is_dir() else f"{p.name}  ({p.stat().st_size} bytes)")
    return f"{path}\n" + "\n".join(rows)


async def read_file(inp: dict, ctx: ToolContext) -> str:
    try:
        path = _safe_path(inp["path"])
    except ValueError as e:
        return str(e)
    if not path.is_file():
        return f"Not a file: {path}"
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, int(inp.get("start_line") or 1))
    chunk = lines[start - 1:start - 1 + 400]
    body = "\n".join(f"{start + i}: {l}" for i, l in enumerate(chunk))
    more = f"\n... ({len(lines)} lines total; read more with start_line)" if start - 1 + 400 < len(lines) else ""
    return f"{path}\n{body}{more}"


async def write_file(inp: dict, ctx: ToolContext) -> str:
    try:
        path = _safe_path(inp["path"])
    except ValueError as e:
        return str(e)
    content = inp["content"]
    exists = path.exists()
    preview = content if len(content) < 1500 else content[:1500] + f"\n...(মোট {len(content)} অক্ষর)"
    verb = "বদলে লিখব (পুরোনো লেখা মুছে যাবে)" if exists else "নতুন তৈরি করব"
    if not await ctx.confirm(f"📝 ফাইল {verb}?\n{path}\n\n{preview}"):
        return DECLINED
    from .history import backup
    backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {path}"


async def edit_file(inp: dict, ctx: ToolContext) -> str:
    try:
        path = _safe_path(inp["path"])
    except ValueError as e:
        return str(e)
    if not path.is_file():
        return f"Not a file: {path}"
    text = path.read_text(encoding="utf-8")
    old, new = inp["old_text"], inp["new_text"]
    count = text.count(old)
    if count != 1:
        return f"old_text must appear exactly once, found {count} times. Read the file and use a longer, unique snippet."
    if not await ctx.confirm(f"✏️ ফাইলে পরিবর্তন করব?\n{path}\n\n➖ আগে:\n{old[:700]}\n\n➕ পরে:\n{new[:700]}"):
        return DECLINED
    from .history import backup
    backup(path)
    path.write_text(text.replace(old, new), encoding="utf-8")
    return f"Edited {path}"


async def search_files(inp: dict, ctx: ToolContext) -> str:
    try:
        root = _project_path(inp["project"])
    except ValueError as e:
        return str(e)
    code, out = await _run(["git", "grep", "-n", "-I", "--untracked", "-i", inp["text"]], cwd=root)
    if code not in (0, 1) or "not a git repository" in out:
        code, out = await _run(["findstr", "/s", "/n", "/i", "/c:" + inp["text"], "*.php", "*.js", "*.ts",
                                "*.vue", "*.blade.php", "*.css", "*.json", "*.md", "*.py", "*.html"], cwd=root)
    lines = out.splitlines()
    return "\n".join(lines[:150]) + (f"\n... {len(lines) - 150} more" if len(lines) > 150 else "") or "No matches."


# ---------------------------------------------------------------- web

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
}


def _strip_tags(s: str) -> str:
    import html
    import re
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


async def _bing(query: str) -> list[tuple[str, str, str]]:
    import base64
    import re
    from urllib.parse import parse_qs, urlparse

    import httpx

    async with httpx.AsyncClient(headers=_BROWSER_HEADERS, timeout=20, follow_redirects=True) as client:
        r = await client.get("https://www.bing.com/search", params={"q": query})
    out = []
    for block in re.findall(r'<li class="b_algo"(.*?)</li>', r.text, flags=re.S)[:8]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.S)
        if not m:
            continue
        url, title = m.group(1).replace("&amp;", "&"), _strip_tags(m.group(2))
        if "bing.com/ck/a" in url:  # tracking redirect: real URL is base64 in "u"
            u = parse_qs(urlparse(url).query).get("u", [""])[0]
            if u.startswith("a1"):
                try:
                    url = base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode()
                except ValueError:
                    pass
        p = re.search(r"<p[^>]*>(.*?)</p>", block, flags=re.S)
        out.append((title, url, _strip_tags(p.group(1)) if p else ""))
    return out


async def web_search(inp: dict, ctx: ToolContext) -> str:
    query = inp["query"]
    results: list[tuple[str, str, str]] = []
    try:
        results = await _bing(query)
    except Exception as e:
        logging.getLogger("zolt.tools").warning("Bing search failed: %s", e)
    if not results:
        try:
            from ddgs import DDGS
            raw = await asyncio.to_thread(lambda: list(DDGS().text(query, max_results=6)))
            results = [(r.get("title", ""), r.get("href", ""), r.get("body", "")) for r in raw]
        except Exception:
            pass
    if not results:
        return "No search results (search engines may be blocking automated requests right now)."
    return "\n\n".join(f"{t}\n{u}\n{s}" for t, u, s in results)


async def fetch_url(inp: dict, ctx: ToolContext) -> str:
    import html
    import re

    import httpx

    url = inp["url"]
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30,
                                     headers={"User-Agent": "Mozilla/5.0 (Zolt personal agent)"}) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        return f"Fetch failed: {e}"
    text = r.text
    if "html" in r.headers.get("content-type", ""):
        text = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html.unescape(re.sub(r"[ \t\r\f\v]+", " ", text))
        text = re.sub(r"\n\s*\n+", "\n", text)
    return f"{r.status_code} {url}\n{text.strip()[:15000]}"


# ---------------------------------------------------------------- memory & reminders

async def remember_fact(inp: dict, ctx: ToolContext) -> str:
    from .memory import remember
    return remember(inp["fact"])


async def forget_fact(inp: dict, ctx: ToolContext) -> str:
    from .memory import forget
    return forget(inp["text"])


async def set_reminder(inp: dict, ctx: ToolContext) -> str:
    from datetime import datetime

    from . import reminders
    if ctx.app is None:
        return "Reminders are not available right now."
    try:
        when = datetime.fromisoformat(inp["when"])
    except ValueError:
        return "when must be a local date-time like 2026-11-18T21:30"
    return reminders.add(ctx.app, ctx.chat_id, when, inp["text"], inp["repeat"], inp["as_task"])


async def list_reminders(inp: dict, ctx: ToolContext) -> str:
    from . import reminders
    return reminders.listing()


async def cancel_reminder(inp: dict, ctx: ToolContext) -> str:
    from . import reminders
    if ctx.app is None:
        return "Reminders are not available right now."
    return reminders.cancel(ctx.app, inp["id"])


# ---------------------------------------------------------------- browser

def _browser_error(e: Exception) -> str:
    return f"Browser error: {type(e).__name__}: {str(e)[:300]}. Take a fresh browser_read and try another element."


async def browser_open(inp: dict, ctx: ToolContext) -> str:
    from .browser import BROWSER
    try:
        return await BROWSER.goto(inp["url"])
    except Exception as e:
        return _browser_error(e)


async def browser_read(inp: dict, ctx: ToolContext) -> str:
    from .browser import BROWSER
    try:
        return await BROWSER.snapshot()
    except Exception as e:
        return _browser_error(e)


async def browser_click(inp: dict, ctx: ToolContext) -> str:
    from .browser import BROWSER, RISKY_WORDS
    ref = int(inp["ref"])
    try:
        info = await BROWSER.element_info(ref)
        if info is None:
            return f"No element [{ref}] on the page now. Call browser_read first."
        label = info["label"].lower()
        if any(w in label for w in RISKY_WORDS):
            page_url = BROWSER.page.url if BROWSER.page else ""
            if not await ctx.confirm(f"🌐 Browser-এ এই বাটনে ক্লিক করব?\n\n\"{info['label']}\"\n{page_url[:120]}"):
                return DECLINED
        return await BROWSER.click(ref)
    except Exception as e:
        return _browser_error(e)


async def browser_type(inp: dict, ctx: ToolContext) -> str:
    from .browser import BROWSER
    ref = int(inp["ref"])
    try:
        info = await BROWSER.element_info(ref)
        if info is None:
            return f"No element [{ref}] on the page now. Call browser_read first."
        secret = (info["type"] == "password" or "password" in info["autocomplete"] or "cc-" in info["autocomplete"]
                  or any(w in info["name"] for w in ("pass", "card", "cvv", "otp")))
        if secret:
            return ("REFUSED: this is a password/payment/OTP field. Never type secrets. Ask the owner to log in "
                    "or enter it herself in the Zolt browser window on the PC, then continue.")
        return await BROWSER.type(ref, inp["text"], bool(inp["press_enter"]))
    except Exception as e:
        return _browser_error(e)


async def browser_key(inp: dict, ctx: ToolContext) -> str:
    from .browser import BROWSER
    try:
        return await BROWSER.press(inp["key"])
    except Exception as e:
        return _browser_error(e)


async def browser_scroll(inp: dict, ctx: ToolContext) -> str:
    from .browser import BROWSER
    try:
        return await BROWSER.scroll(inp["direction"] == "down")
    except Exception as e:
        return _browser_error(e)


async def browser_back(inp: dict, ctx: ToolContext) -> str:
    from .browser import BROWSER
    try:
        return await BROWSER.back()
    except Exception as e:
        return _browser_error(e)


async def browser_screenshot(inp: dict, ctx: ToolContext) -> str:
    from .browser import BROWSER
    try:
        png = await BROWSER.screenshot()
    except Exception as e:
        return _browser_error(e)
    await ctx.bot.send_photo(ctx.chat_id, png, caption=inp.get("caption") or "🌐 Browser")
    return "Browser screenshot sent to the owner."


# ---------------------------------------------------------------- registry

Handler = Callable[[dict, ToolContext], Awaitable[str]]

TOOLS: list[tuple[dict[str, Any], Handler]] = [
    ({
        "name": "power_action",
        "description": "Control this Windows PC's power: shutdown, restart, sleep, lock the screen, or cancel a scheduled shutdown/restart. The owner is asked to approve shutdown/restart/sleep on Telegram automatically.",
        "input_schema": _schema({
            "action": {"type": "string", "enum": ["shutdown", "restart", "sleep", "lock", "cancel"]},
            "delay_seconds": {"type": "integer", "description": "Delay before shutdown/restart (10-3600). Use 30 unless asked."},
        }, ["action", "delay_seconds"]),
    }, power_action),
    ({
        "name": "open_website",
        "description": "Open a website in the PC's default browser, e.g. fiverr.com, github.com, a project's live URL.",
        "input_schema": _schema({"url": {"type": "string"}}, ["url"]),
    }, open_website),
    ({
        "name": "open_app",
        "description": "Open a desktop app by its friendly name. Known names: " + ", ".join(sorted(CONFIG.apps)) + ".",
        "input_schema": _schema({"name": {"type": "string"}}, ["name"]),
    }, open_app),
    ({
        "name": "open_project_folder",
        "description": "Open a project folder (inside the projects directory) in Explorer or VS Code.",
        "input_schema": _schema({
            "project": {"type": "string", "description": "Folder name inside the projects directory."},
            "in_vscode": {"type": "boolean"},
        }, ["project", "in_vscode"]),
    }, open_folder),
    ({
        "name": "take_screenshot",
        "description": "Take a screenshot of the PC screen and send it to the owner on Telegram.",
        "input_schema": _schema({}, []),
    }, take_screenshot),
    ({
        "name": "list_projects",
        "description": "List project folders in the projects directory and show which are git repositories.",
        "input_schema": _schema({}, []),
    }, list_projects),
    ({
        "name": "git_status",
        "description": "Show git status and the last 5 commits of a project.",
        "input_schema": _schema({"project": {"type": "string"}}, ["project"]),
    }, git_status),
    ({
        "name": "git_push",
        "description": "Commit all current changes in a project (git add -A + commit) and push to its remote. The owner approves on Telegram automatically. Write a short, clear English commit message describing the changes.",
        "input_schema": _schema({
            "project": {"type": "string"},
            "commit_message": {"type": "string"},
        }, ["project", "commit_message"]),
    }, git_push),
    ({
        "name": "run_command",
        "description": "Run a PowerShell command on the PC (e.g. php artisan migrate, composer install, npm run build, git log). The owner approves each command on Telegram. Prefer the dedicated tools when one fits (open_project_folder, open_website, open_app, git_*). Commands that never finish on their own - dev servers (php artisan serve, npm run dev), watchers, queue workers, GUI apps - MUST use background=true. Normal commands stop after 3 minutes. Never run destructive commands (deleting files, formatting, killing system processes) unless she explicitly asked.",
        "input_schema": _schema({
            "command": {"type": "string"},
            "project": {"type": "string", "description": "Project folder to run in, or empty string for the projects directory."},
            "background": {"type": "boolean", "description": "true = start in its own window and return immediately (servers, watchers, apps)."},
        }, ["command", "project", "background"]),
    }, run_command),
    ({
        "name": "list_dir",
        "description": "List files and folders at a path (inside the projects folder or the user's home folder, e.g. C:\\Users\\<name>\\Desktop).",
        "input_schema": _schema({"path": {"type": "string"}}, ["path"]),
    }, list_dir),
    ({
        "name": "read_file",
        "description": "Read a text/code file with line numbers, 400 lines at a time.",
        "input_schema": _schema({
            "path": {"type": "string"},
            "start_line": {"type": "integer", "description": "1 for the beginning."},
        }, ["path", "start_line"]),
    }, read_file),
    ({
        "name": "edit_file",
        "description": "Change part of a file by replacing old_text (must appear exactly once - copy it from read_file, without line numbers) with new_text. The owner approves on Telegram. Use for code changes in her projects.",
        "input_schema": _schema({
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        }, ["path", "old_text", "new_text"]),
    }, edit_file),
    ({
        "name": "write_file",
        "description": "Create a new file or fully overwrite one with the given content. The owner approves on Telegram. Prefer edit_file for small changes to existing files.",
        "input_schema": _schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    }, write_file),
    ({
        "name": "search_in_project",
        "description": "Search a project's files for a text (case-insensitive). Returns file:line matches.",
        "input_schema": _schema({"project": {"type": "string"}, "text": {"type": "string"}}, ["project", "text"]),
    }, search_files),
    ({
        "name": "web_search",
        "description": "Search the internet (DuckDuckGo). Use for current information, docs, prices, news, error messages.",
        "input_schema": _schema({"query": {"type": "string"}}, ["query"]),
    }, web_search),
    ({
        "name": "fetch_url",
        "description": "Download a web page and return its text, e.g. to read docs or a result from web_search.",
        "input_schema": _schema({"url": {"type": "string"}}, ["url"]),
    }, fetch_url),
    ({
        "name": "remember",
        "description": "Save a lasting fact about the owner, her projects, accounts or preferences to long-term memory (e.g. 'Apollo is deployed on cPanel', 'prefers SSLCommerz'). Use whenever she tells you something worth remembering or says 'mone rakho'. Never store passwords or keys.",
        "input_schema": _schema({"fact": {"type": "string"}}, ["fact"]),
    }, remember_fact),
    ({
        "name": "forget",
        "description": "Delete memory lines containing the given text.",
        "input_schema": _schema({"text": {"type": "string"}}, ["text"]),
    }, forget_fact),
    ({
        "name": "set_reminder",
        "description": "Schedule a reminder or a recurring task. as_task=false just sends her the text at that time; as_task=true makes you run the text as a command at that time (e.g. 'ask her which country and Gmail to use for today's outreach'). repeat: none, daily, or weekdays (Sun-Thu).",
        "input_schema": _schema({
            "when": {"type": "string", "description": "Local date-time, ISO format, e.g. 2026-11-18T21:30. For daily/weekdays only the time matters."},
            "text": {"type": "string"},
            "repeat": {"type": "string", "enum": ["none", "daily", "weekdays"]},
            "as_task": {"type": "boolean"},
        }, ["when", "text", "repeat", "as_task"]),
    }, set_reminder),
    ({
        "name": "list_reminders",
        "description": "List scheduled reminders and recurring tasks with their ids.",
        "input_schema": _schema({}, []),
    }, list_reminders),
    ({
        "name": "cancel_reminder",
        "description": "Cancel a reminder by id (get ids from list_reminders).",
        "input_schema": _schema({"id": {"type": "string"}}, ["id"]),
    }, cancel_reminder),
    ({
        "name": "browser_open",
        "description": "Open a URL in Zolt's own browser window (Microsoft Edge with a saved Zolt profile - sites stay logged in after she logs in once). Returns a snapshot: page text plus numbered ELEMENTS. Use this (not open_website) whenever YOU need to read or operate a website: Gmail, Fiverr, GitHub, Facebook, her live sites.",
        "input_schema": _schema({"url": {"type": "string"}}, ["url"]),
    }, browser_open),
    ({
        "name": "browser_read",
        "description": "Get a fresh snapshot of the current browser page (text + numbered elements). Do this after the page changes or before choosing an element.",
        "input_schema": _schema({}, []),
    }, browser_read),
    ({
        "name": "browser_click",
        "description": "Click element [ref] from the latest snapshot. Buttons like Send/Post/Delete/Pay/Order ask the owner first automatically.",
        "input_schema": _schema({"ref": {"type": "integer"}}, ["ref"]),
    }, browser_click),
    ({
        "name": "browser_type",
        "description": "Type text into input/textarea/editor [ref]; press_enter=true to submit search boxes. Password, card and OTP fields are refused - if a login is needed, ask her to log in herself in the Zolt browser window on the PC.",
        "input_schema": _schema({
            "ref": {"type": "integer"}, "text": {"type": "string"}, "press_enter": {"type": "boolean"},
        }, ["ref", "text", "press_enter"]),
    }, browser_type),
    ({
        "name": "browser_key",
        "description": "Press a keyboard key in the browser, e.g. Enter, Escape, Tab, PageDown.",
        "input_schema": _schema({"key": {"type": "string"}}, ["key"]),
    }, browser_key),
    ({
        "name": "browser_scroll",
        "description": "Scroll the browser page up or down to see more.",
        "input_schema": _schema({"direction": {"type": "string", "enum": ["up", "down"]}}, ["direction"]),
    }, browser_scroll),
    ({
        "name": "browser_back",
        "description": "Go back to the previous page in the browser.",
        "input_schema": _schema({}, []),
    }, browser_back),
    ({
        "name": "browser_screenshot",
        "description": "Send her a screenshot of the browser page on Telegram (e.g. to show a result or a draft before sending).",
        "input_schema": _schema({"caption": {"type": "string"}}, ["caption"]),
    }, browser_screenshot),
]

TOOL_SCHEMAS = [{**schema, "strict": True} for schema, _ in TOOLS]
_HANDLERS = {schema["name"]: handler for schema, handler in TOOLS}


def _openai_tool(schema: dict[str, Any]) -> dict[str, Any]:
    fn: dict[str, Any] = {"name": schema["name"], "description": schema["description"]}
    props = schema["input_schema"]["properties"]
    if props:  # some providers (Gemini) reject an empty properties object
        fn["parameters"] = {"type": "object", "properties": props,
                            "required": schema["input_schema"]["required"]}
    return {"type": "function", "function": fn}


OPENAI_TOOL_SCHEMAS = [_openai_tool(schema) for schema, _ in TOOLS]


async def run_tool(name: str, tool_input: dict, ctx: ToolContext) -> tuple[str, bool]:
    """Returns (result_text, is_error)."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return f"Unknown tool: {name}", True
    ctx.tools_run.append(name)
    logging.getLogger("zolt.tools").info("Tool %s %s", name, str(tool_input)[:200])
    try:
        result, is_error = await handler(tool_input, ctx), False
    except Exception as e:  # report tool failures back to the model instead of crashing
        result, is_error = f"{type(e).__name__}: {e}", True
    ctx.steps.append(f"{name}({str(tool_input)[:300]}) -> {'ERROR: ' if is_error else ''}{str(result)[:600]}")
    try:
        from .history import log_action
        log_action(name, tool_input, result, is_error)
    except OSError:
        pass
    return result, is_error


def register_tools(new: list[tuple[dict[str, Any], Handler]], strict: bool = True) -> None:
    """Add tools at runtime (extra modules, MCP servers). Providers hold these same lists."""
    for schema, handler in new:
        if schema["name"] in _HANDLERS:
            continue
        TOOLS.append((schema, handler))
        TOOL_SCHEMAS.append({**schema, "strict": True} if strict else schema)
        OPENAI_TOOL_SCHEMAS.append(_openai_tool(schema))
        _HANDLERS[schema["name"]] = handler


