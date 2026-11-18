"""Send free models only the tools a message needs.

All 54 tool definitions are ~5k tokens per request, which burns through free
tokens-per-minute limits. Tools are grouped; a small core set is always sent,
groups are added by keywords in her message, and the model can load any group
itself with load_tools. Groups stay loaded for the rest of the conversation.
"""
from __future__ import annotations

import re
from typing import Any

GROUPS: dict[str, set[str]] = {
    "core": {"list_projects", "open_app", "open_website", "open_project_folder", "take_screenshot",
             "web_search", "fetch_url", "remember", "set_reminder", "system_status", "show_history", "load_tools"},
    "files": {"list_dir", "read_file", "edit_file", "write_file", "search_in_project", "read_document",
              "make_document", "send_file", "search_my_documents", "undo_changes", "show_history"},
    "code": {"git_status", "git_push", "git_diff", "git_branch", "create_pull_request", "run_tests",
             "run_command", "db_query", "deploy_project", "set_deploy_target", "read_file", "edit_file",
             "search_in_project"},
    "browser": {"browser_open", "browser_read", "browser_click", "browser_type", "browser_key",
                "browser_scroll", "browser_back", "browser_screenshot"},
    "desktop": {"desktop_windows", "desktop_read", "desktop_click", "desktop_type", "desktop_hotkey", "clipboard"},
    "google": {"gmail", "calendar"},
    "media": {"make_video", "make_document", "send_file"},
    "system": {"power_action", "watch_website", "list_reminders", "cancel_reminder", "forget",
               "start_parallel_task", "call_webhook", "show_history", "undo_changes"},
}

KEYWORDS: dict[str, str] = {
    "files": r"file|folder|pdf|docx?|word|excel|xlsx|csv|invoice|report|porho|pore|poro|read|edit|bodl|change|"
             r"likh|write|undo|history|ki ki korecho|document|cv|ফাইল|পড়|লিখ|বদল|পাঠাও|send|zip|composer|\.json|\.php|\.env",
    "code": r"git|push|commit|branch|pull request|\bpr\b|test|deploy|server|live|database|\bdb\b|query|order|"
            r"artisan|composer|npm|command|chalao|run|install|migrat|bug|error|fix|thik|feature|laravel|code|kod",
    "browser": r"browser|login|fiverr|facebook|\bfb\b|linkedin|upwork|click|form|site e|website e|page e|পেজ",
    "desktop": r"desktop|window|app er|clipboard|copy|paste|photoshop|excel e|word e|notepad e",
    "media": r"video|vdo|reel|reels|short|tiktok|youtube|ভিডিও|ভিডিও|রিল|গল্প|golpo|story|kahini|কাহিনি",
    "google": r"e-?mail|mail|gmail|inbox|calendar|meeting|event|মেইল|ইমেইল|মিটিং",
    "system": r"shutdown|restart|\boff\b|bondho|lock|sleep|reminder|cancel|monitor|down|watch|webhook|n8n|"
              r"parallel|ek ?sathe|background|forget|bhule|বন্ধ|রিস্টার্ট|মনে করি",
}

LOAD_TOOLS_SCHEMA: dict[str, Any] = {
    "name": "load_tools",
    "description": "Load more tools when the ones you have can't do the job. Groups: files (read/edit/create files, "
                   "documents, PDFs, undo), code (git, tests, PRs, run commands, database, deploy), browser (operate "
                   "websites yourself), desktop (control desktop apps, clipboard), google (Gmail, Calendar), media (make videos/reels from a story), system "
                   "(power off/restart/lock, reminders list/cancel, website monitors, webhooks/n8n, parallel tasks).",
    "input_schema": {"type": "object", "additionalProperties": False, "required": ["group"],
                     "properties": {"group": {"type": "string", "enum": [g for g in GROUPS if g != "core"]}}},
}


def groups_for(text: str) -> set[str]:
    low = text.lower()
    return {"core"} | {g for g, pattern in KEYWORDS.items() if re.search(pattern, low)}


def allowed_names(groups: set[str]) -> set[str]:
    names: set[str] = set()
    for g in groups:
        names |= GROUPS.get(g, set())
    if "mcp" in groups:
        names.add("mcp_*")
    return names


def select(all_openai_tools: list[dict[str, Any]], groups: set[str]) -> list[dict[str, Any]]:
    names = allowed_names(groups)
    return [t for t in all_openai_tools
            if t["function"]["name"] in names
            or ("mcp_*" in names and t["function"]["name"].startswith("mcp_"))]
