"""More of Zolt's tools: files, history/undo, system, monitors, coding (tests/branches/PRs),
deploy, database, parallel tasks, webhooks (n8n), desktop control, Gmail/Calendar."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import CONFIG
from .memory import DATA_DIR
from .tools import DECLINED, ToolContext, _project_path, _run, _safe_path, _schema, register_tools

# Set by bot.py: launches a task on its own conversation and reports back when done.
parallel_launcher: Callable[[ToolContext, str], Awaitable[int]] | None = None


# ---------------------------------------------------------------- files & documents

async def send_file(inp: dict, ctx: ToolContext) -> str:
    try:
        path = _safe_path(inp["path"])
    except ValueError as e:
        return str(e)
    if not path.is_file():
        return f"Not a file: {path}"
    if path.stat().st_size > 49 * 1024 * 1024:
        return "File is larger than Telegram's 50 MB bot limit."
    with path.open("rb") as f:
        await ctx.bot.send_document(ctx.chat_id, f, filename=path.name, caption=inp.get("caption") or None)
    return f"Sent {path.name} to her on Telegram."


async def read_document(inp: dict, ctx: ToolContext) -> str:
    from .documents import extract_text
    try:
        path = _safe_path(inp["path"])
    except ValueError as e:
        return str(e)
    if not path.is_file():
        return f"Not a file: {path}"
    return f"{path}\n\n{extract_text(path)}"


async def make_document(inp: dict, ctx: ToolContext) -> str:
    from . import documents
    kind, name, content = inp["kind"], inp["file_name"] or "zolt-document", inp["content"]
    if kind == "pdf":
        out = await documents.make_pdf(content, name)
    elif kind == "docx":
        out = await asyncio.to_thread(documents.make_docx, inp.get("title", ""), content, name)
    else:
        out = await asyncio.to_thread(documents.make_xlsx, content, name)
    if inp.get("send", True):
        with out.open("rb") as f:
            await ctx.bot.send_document(ctx.chat_id, f, filename=out.name)
        return f"Created {out} and sent it to her."
    return f"Created {out}"


async def search_my_documents(inp: dict, ctx: ToolContext) -> str:
    from .documents import search_docs
    return await asyncio.to_thread(search_docs, inp["query"])


# ---------------------------------------------------------------- history & undo

async def show_history(inp: dict, ctx: ToolContext) -> str:
    from .history import recent
    return recent(int(inp.get("count") or 20))


async def undo_changes(inp: dict, ctx: ToolContext) -> str:
    from .history import undo_last
    steps = max(1, int(inp.get("steps") or 1))
    if not await ctx.confirm(f"↩️ শেষ {steps}টা ফাইল-পরিবর্তন undo করব?"):
        return DECLINED
    return undo_last(steps)


# ---------------------------------------------------------------- system

async def system_status(inp: dict, ctx: ToolContext) -> str:
    import psutil

    def go():
        vm, disk = psutil.virtual_memory(), shutil.disk_usage("C:\\")
        bat = psutil.sensors_battery()
        procs = sorted(psutil.process_iter(["name", "memory_info"]),
                       key=lambda p: (p.info["memory_info"].rss if p.info["memory_info"] else 0), reverse=True)[:6]
        top = ", ".join(f"{p.info['name']} {p.info['memory_info'].rss // 2**20}MB" for p in procs if p.info["memory_info"])
        lines = [
            f"CPU: {psutil.cpu_percent(interval=1)}%",
            f"RAM: {vm.percent}% used ({vm.used // 2**30}/{vm.total // 2**30} GB)",
            f"Disk C: {disk.free // 2**30} GB free of {disk.total // 2**30} GB",
            f"Battery: {bat.percent}% {'charging' if bat.power_plugged else 'on battery'}" if bat else "Battery: none (desktop)",
            f"Top memory: {top}",
        ]
        return "\n".join(lines)
    text = await asyncio.to_thread(go)
    code, _ = await _run(["ping", "-n", "1", "-w", "2000", "8.8.8.8"], timeout=10)
    return text + f"\nInternet: {'connected' if code == 0 else 'NOT reachable'}"


async def clipboard(inp: dict, ctx: ToolContext) -> str:
    if inp["action"] == "get":
        code, out = await _run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"], timeout=15)
        return out[:8000] or "(clipboard is empty)"
    text = inp.get("text", "")
    proc = await asyncio.create_subprocess_exec(
        "powershell", "-NoProfile", "-Command", "$input | Set-Clipboard", stdin=asyncio.subprocess.PIPE)
    await proc.communicate(text.encode("utf-8"))
    return "Copied to the PC clipboard."


# ---------------------------------------------------------------- monitors

async def watch_website(inp: dict, ctx: ToolContext) -> str:
    from . import monitors
    action = inp["action"]
    if action == "add":
        return monitors.add_site(inp["url"])
    if action == "remove":
        return monitors.remove_site(inp["url"])
    return monitors.list_sites()


# ---------------------------------------------------------------- coding

async def run_tests(inp: dict, ctx: ToolContext) -> str:
    try:
        path = _project_path(inp["project"])
    except ValueError as e:
        return str(e)
    if (path / "artisan").exists():
        cmd = "php artisan test"
    elif (path / "vendor/bin/phpunit").exists():
        cmd = r"vendor\bin\phpunit"
    elif (path / "package.json").exists() and '"test"' in (path / "package.json").read_text(encoding="utf-8", errors="ignore"):
        cmd = "npm test --silent"
    elif any(path.glob("test*.py")) or (path / "tests").is_dir():
        cmd = "python -m pytest -q"
    else:
        return "No test setup found in this project."
    if inp.get("filter"):
        cmd += f" --filter {inp['filter']}" if "artisan" in cmd or "phpunit" in cmd else f" {inp['filter']}"
    code, out = await _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], cwd=path, timeout=300)
    return f"$ {cmd}\nexit {code}\n{out[-8000:]}"


async def git_diff(inp: dict, ctx: ToolContext) -> str:
    try:
        path = _project_path(inp["project"])
    except ValueError as e:
        return str(e)
    _, stat = await _run(["git", "diff", "--stat", "HEAD"], cwd=path)
    _, diff = await _run(["git", "diff", "HEAD"], cwd=path)
    return f"{stat}\n\n{diff[:15000]}" if stat else "No uncommitted changes."


async def git_branch(inp: dict, ctx: ToolContext) -> str:
    try:
        path = _project_path(inp["project"])
    except ValueError as e:
        return str(e)
    name = inp["name"].strip()
    if not name:
        _, out = await _run(["git", "branch", "-a"], cwd=path)
        return out
    if not re.fullmatch(r"[\w./-]+", name):
        return "Invalid branch name."
    args = ["git", "switch", "-c", name] if inp["create"] else ["git", "switch", name]
    code, out = await _run(args, cwd=path)
    return out or f"On branch {name}"


async def create_pull_request(inp: dict, ctx: ToolContext) -> str:
    try:
        path = _project_path(inp["project"])
    except ValueError as e:
        return str(e)
    code, auth = await _run(["gh", "auth", "status"], cwd=path, timeout=30)
    if code != 0:
        return "GitHub CLI is not logged in. She needs to run 'gh auth login' once on the PC."
    _, branch = await _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    if branch in ("main", "master"):
        return "Current branch is main/master - create a feature branch first (git_branch create=true)."
    if not await ctx.confirm(f"🔀 GitHub-এ Pull Request খুলব?\n📦 {path.name}  🌿 {branch}\n📝 {inp['title']}"):
        return DECLINED
    code, out = await _run(["git", "push", "-u", "origin", branch], cwd=path, timeout=300)
    if code != 0:
        return f"Push failed:\n{out}"
    code, out = await _run(["gh", "pr", "create", "--title", inp["title"], "--body", inp["body"] or inp["title"]],
                           cwd=path, timeout=120)
    return out


# ---------------------------------------------------------------- deploy

DEPLOY_FILE = DATA_DIR / "deploy.json"


def _deploy_cfg() -> dict[str, Any]:
    try:
        return json.loads(DEPLOY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def set_deploy_target(inp: dict, ctx: ToolContext) -> str:
    cfg = _deploy_cfg()
    commands = [c.strip() for c in inp["commands"].split("\n") if c.strip()] or [
        "git pull", "composer install --no-dev --optimize-autoloader", "php artisan migrate --force",
        "php artisan optimize"]
    cfg[inp["project"]] = {"ssh": inp["ssh"], "port": int(inp["port"] or 22), "path": inp["path"], "commands": commands}
    DATA_DIR.mkdir(exist_ok=True)
    DEPLOY_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return f"Saved deploy target for {inp['project']}: {inp['ssh']}:{inp['path']} -> {commands}"


async def deploy_project(inp: dict, ctx: ToolContext) -> str:
    target = _deploy_cfg().get(inp["project"])
    if not target:
        return ("No deploy target saved for this project. Ask her for the SSH login (user@host), port and project "
                "path on the server, then use set_deploy_target. SSH must use a key (no password prompts).")
    remote = f"cd {target['path']} && " + " && ".join(target["commands"])
    if not await ctx.confirm(f"🚀 Live server-এ deploy করব?\n{target['ssh']}:{target['path']}\n\n" + "\n".join(target["commands"])):
        return DECLINED
    code, out = await _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-p", str(target["port"]),
                            target["ssh"], remote], timeout=600)
    return f"exit {code}\n{out[-8000:]}"


# ---------------------------------------------------------------- database

def _env_values(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values
    return {k: (v or "") for k, v in dotenv_values(path / ".env").items()}


async def db_query(inp: dict, ctx: ToolContext) -> str:
    import pymysql
    try:
        path = _project_path(inp["project"])
    except ValueError as e:
        return str(e)
    env = _env_values(path)
    if env.get("DB_CONNECTION", "mysql") not in ("mysql", "mariadb"):
        return f"Only MySQL/MariaDB is supported (project uses {env.get('DB_CONNECTION')})."
    sql = inp["sql"].strip().rstrip(";")
    read_only = re.match(r"(?is)^\s*(select|show|describe|desc|explain)\b", sql) and ";" not in sql
    if not read_only and not await ctx.confirm(f"🗄️ Database-এ পরিবর্তন করব?\n{path.name} / {env.get('DB_DATABASE')}\n\n{sql[:800]}"):
        return DECLINED

    def go():
        conn = pymysql.connect(host=env.get("DB_HOST", "127.0.0.1"), port=int(env.get("DB_PORT") or 3306),
                               user=env.get("DB_USERNAME", "root"), password=env.get("DB_PASSWORD", ""),
                               database=env.get("DB_DATABASE"), connect_timeout=10, charset="utf8mb4")
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                if cur.description:
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchmany(50)
                    lines = [" | ".join(cols)] + [" | ".join(str(v)[:60] for v in r) for r in rows]
                    return "\n".join(lines) + ("\n...(first 50 rows)" if len(rows) == 50 else "")
                conn.commit()
                return f"OK, {cur.rowcount} row(s) affected."
        finally:
            conn.close()
    try:
        return await asyncio.to_thread(go)
    except pymysql.MySQLError as e:
        return f"Database error: {e} (is Laragon's MySQL running?)"


# ---------------------------------------------------------------- parallel tasks & webhooks

async def start_parallel_task(inp: dict, ctx: ToolContext) -> str:
    if parallel_launcher is None:
        return "Parallel tasks are not available right now."
    number = await parallel_launcher(ctx, inp["task"])
    return f"Started background task #{number}. It reports to her by itself when done; you can continue."


async def call_webhook(inp: dict, ctx: ToolContext) -> str:
    import httpx
    url = inp["url"].strip()
    try:
        payload = json.loads(inp["json_body"] or "{}")
    except ValueError:
        return "json_body must be valid JSON."
    if not await ctx.confirm(f"🔗 Webhook চালাব (যেমন n8n workflow)?\n{url}\n{json.dumps(payload, ensure_ascii=False)[:400]}"):
        return DECLINED
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(url, json=payload)
    return f"HTTP {r.status_code}\n{r.text[:5000]}"


# ---------------------------------------------------------------- desktop control

async def desktop_windows(inp: dict, ctx: ToolContext) -> str:
    from . import desktop
    return await desktop.windows()


async def desktop_read(inp: dict, ctx: ToolContext) -> str:
    from . import desktop
    return await desktop.read(inp["window"])


async def desktop_click(inp: dict, ctx: ToolContext) -> str:
    from . import desktop
    from .browser import RISKY_WORDS
    ref = int(inp["ref"])
    label = (await desktop.label(ref)).lower()
    if any(w in label for w in RISKY_WORDS) and not await ctx.confirm(f"🖱️ Desktop-এ এই বাটনে ক্লিক করব?\n\"{label[:80]}\""):
        return DECLINED
    return await desktop.click(ref)


async def desktop_type(inp: dict, ctx: ToolContext) -> str:
    from . import desktop
    return await desktop.type_text(inp["text"], int(inp.get("ref") or 0))


async def desktop_hotkey(inp: dict, ctx: ToolContext) -> str:
    from . import desktop
    return await desktop.hotkey(inp["keys"])


# ---------------------------------------------------------------- Gmail & Calendar

async def gmail(inp: dict, ctx: ToolContext) -> str:
    from . import google_api as g
    action = inp["action"]
    if action == "search":
        return await g.gmail_search(inp["query"] or "in:inbox", 10)
    if action == "read":
        return await g.gmail_read(inp["message_id"])
    if action == "draft":
        return await g.gmail_draft(inp["to"], inp["subject"], inp["body"])
    if action == "send":
        if not await ctx.confirm(f"📧 এই email পাঠাব?\nTo: {inp['to']}\nSubject: {inp['subject']}\n\n{inp['body'][:1200]}"):
            return DECLINED
        return await g.gmail_send(inp["to"], inp["subject"], inp["body"])
    return "Unknown action."


async def calendar(inp: dict, ctx: ToolContext) -> str:
    from . import google_api as g
    if inp["action"] == "list":
        return await g.calendar_events(int(inp.get("days") or 7))
    if not await ctx.confirm(f"📅 Calendar-এ event যোগ করব?\n{inp['title']}\n{inp['start']} → {inp['end']}"):
        return DECLINED
    return await g.calendar_add(inp["title"], inp["start"], inp["end"])


# ---------------------------------------------------------------- registry

S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}

MORE_TOOLS = [
    ({"name": "send_file", "description": "Send a file from the PC to her on Telegram (code, PDF, zip, image, anything under 50 MB).",
      "input_schema": _schema({"path": S, "caption": S}, ["path", "caption"])}, send_file),
    ({"name": "read_document", "description": "Read the text of a PDF, Word (.docx), Excel (.xlsx), CSV, text/code file or list a zip's contents. Files she sends on Telegram are saved in data/inbox.",
      "input_schema": _schema({"path": S}, ["path"])}, read_document),
    ({"name": "make_document", "description": "Create a file and send it to her: kind=pdf (content is HTML - use it for invoices, reports, letters; Bangla works), docx (content is plain text, '## ' headings, '- ' bullets), xlsx (content is CSV, first row = headers).",
      "input_schema": _schema({"kind": {"type": "string", "enum": ["pdf", "docx", "xlsx"]}, "file_name": S, "title": S,
                               "content": S, "send": B}, ["kind", "file_name", "title", "content", "send"])}, make_document),
    ({"name": "search_my_documents", "description": "Keyword-search her saved documents (data/docs) and files she sent on Telegram, e.g. her CV, client requirements, invoices.",
      "input_schema": _schema({"query": S}, ["query"])}, search_my_documents),
    ({"name": "show_history", "description": "Show the latest actions Zolt performed (tool, input, success).",
      "input_schema": _schema({"count": I}, ["count"])}, show_history),
    ({"name": "undo_changes", "description": "Undo the last file edits/writes Zolt made (restores backups; deletes files Zolt newly created).",
      "input_schema": _schema({"steps": I}, ["steps"])}, undo_changes),
    ({"name": "system_status", "description": "PC status: CPU, RAM, disk space, battery, top memory-using programs, internet connectivity.",
      "input_schema": _schema({}, [])}, system_status),
    ({"name": "clipboard", "description": "Get the PC clipboard text, or set it (action=set, text=...).",
      "input_schema": _schema({"action": {"type": "string", "enum": ["get", "set"]}, "text": S}, ["action", "text"])}, clipboard),
    ({"name": "watch_website", "description": "Uptime monitor: add/remove/list websites checked every 5 minutes; she gets a Telegram alert when one goes down or comes back.",
      "input_schema": _schema({"action": {"type": "string", "enum": ["add", "remove", "list"]}, "url": S}, ["action", "url"])}, watch_website),
    ({"name": "run_tests", "description": "Run a project's automated tests (php artisan test / phpunit / npm test / pytest). Use after code changes; if tests fail, read the errors, fix, and run again.",
      "input_schema": _schema({"project": S, "filter": S}, ["project", "filter"])}, run_tests),
    ({"name": "git_diff", "description": "Show uncommitted changes in a project (review your edits before committing).",
      "input_schema": _schema({"project": S}, ["project"])}, git_diff),
    ({"name": "git_branch", "description": "List branches (name empty), switch to a branch, or create one (create=true) for a new feature/fix.",
      "input_schema": _schema({"project": S, "name": S, "create": B}, ["project", "name", "create"])}, git_branch),
    ({"name": "create_pull_request", "description": "Push the current feature branch and open a GitHub Pull Request with gh. Commit first (git_push commits too).",
      "input_schema": _schema({"project": S, "title": S, "body": S}, ["project", "title", "body"])}, create_pull_request),
    ({"name": "set_deploy_target", "description": "Save how to deploy a project over SSH: ssh=user@host, port, path on server, commands (one per line; empty = Laravel default: git pull, composer install, migrate, optimize).",
      "input_schema": _schema({"project": S, "ssh": S, "port": I, "path": S, "commands": S}, ["project", "ssh", "port", "path", "commands"])}, set_deploy_target),
    ({"name": "deploy_project", "description": "Deploy a project to its live server over SSH (key-based) using the saved deploy target. Asks her first.",
      "input_schema": _schema({"project": S}, ["project"])}, deploy_project),
    ({"name": "db_query", "description": "Run SQL on a Laravel project's MySQL database (credentials read from the project's .env, never shown). SELECT/SHOW/DESCRIBE run directly; changes ask her first.",
      "input_schema": _schema({"project": S, "sql": S}, ["project", "sql"])}, db_query),
    ({"name": "start_parallel_task", "description": "Start an independent job in the background with its own conversation (e.g. work on another project at the same time). It reports to her when finished. Give a complete, self-contained instruction.",
      "input_schema": _schema({"task": S}, ["task"])}, start_parallel_task),
    ({"name": "call_webhook", "description": "POST JSON to a webhook URL - e.g. trigger one of her n8n workflows. Asks her first.",
      "input_schema": _schema({"url": S, "json_body": S}, ["url", "json_body"])}, call_webhook),
    ({"name": "desktop_windows", "description": "List open desktop windows (titles).",
      "input_schema": _schema({}, [])}, desktop_windows),
    ({"name": "desktop_read", "description": "Focus a desktop app window (by part of its title) and list its controls as numbered refs.",
      "input_schema": _schema({"window": S}, ["window"])}, desktop_read),
    ({"name": "desktop_click", "description": "Click control [ref] from the last desktop_read.",
      "input_schema": _schema({"ref": I}, ["ref"])}, desktop_click),
    ({"name": "desktop_type", "description": "Type text into control [ref] (or into the focused control when ref=0).",
      "input_schema": _schema({"text": S, "ref": I}, ["text", "ref"])}, desktop_type),
    ({"name": "desktop_hotkey", "description": "Press a key combo in the focused app, e.g. ctrl+s, alt+f4, ctrl+shift+n, enter.",
      "input_schema": _schema({"keys": S}, ["keys"])}, desktop_hotkey),
    ({"name": "gmail", "description": "Gmail via Google's API: search (Gmail query like 'from:client is:unread'), read (message_id), draft, send (asks her). Prefer this over the browser for email.",
      "input_schema": _schema({"action": {"type": "string", "enum": ["search", "read", "draft", "send"]}, "query": S,
                               "message_id": S, "to": S, "subject": S, "body": S},
                              ["action", "query", "message_id", "to", "subject", "body"])}, gmail),
    ({"name": "calendar", "description": "Google Calendar: list upcoming events (days) or add an event (title, start/end as local ISO like 2026-11-20T15:00; asks her).",
      "input_schema": _schema({"action": {"type": "string", "enum": ["list", "add"]}, "days": I, "title": S, "start": S, "end": S},
                              ["action", "days", "title", "start", "end"])}, calendar),
]

register_tools(MORE_TOOLS)
