"""Benchmark free models on Zolt's real everyday tasks and print a ranking.

    python -m zolt.bench                # all candidate models
    python -m zolt.bench groq/qwen/...  # only the given models
    python -m zolt.bench zolt           # Zolt as she uses it: MODEL_ORDER with automatic fallback

Side-effect tools (opening apps, pushing, reminders, ...) are simulated, so nothing
on the PC changes. Read-only tools (git status, read file, system status, web search)
run for real. Scores: right tool used, no wrong tool, clean Bangla, key fact present.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

from .brain import has_foreign_script
from .providers import LiteLLMProvider, ProviderUnavailable
from .tools import ToolContext, _HANDLERS

CANDIDATES = [
    ("GROQ_API_KEY", "groq/qwen/qwen3.8-27b"),
    ("GROQ_API_KEY", "groq/openai/gpt-oss-120b"),
    ("GROQ_API_KEY", "groq/openai/gpt-oss-20b"),
    ("OPENROUTER_API_KEY", "openrouter/nvidia/nemotron-3.5-super-vl:free"),
    ("OPENROUTER_API_KEY", "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"),
    ("OPENROUTER_API_KEY", "openrouter/google/gemma-4-31b-it:free"),
    ("OPENROUTER_API_KEY", "openrouter/inclusionai/ling-3.2-flash:free"),
    ("OPENROUTER_API_KEY", "openrouter/nvidia/nemotron-3.5-lightning:free"),
    ("GEMINI_API_KEY", "gemini/gemini-flash-latest"),
    ("GEMINI_API_KEY", "gemini/gemini-flash-lite-latest"),
]

# (her message, tools that count as right, tools that must NOT be used, words the reply should contain)
TASKS = [
    ("amar project gula dekhao", {"list_projects", "list_dir"}, set(), ["Apollo"]),
    ("Apollo project kon branch e ache?", {"git_status", "git_branch"}, {"run_command"}, ["main"]),
    ("Apollo er composer.json e laravel er version koto?", {"read_file", "read_document", "search_in_project"}, set(), ["12"]),
    ("pc te disk e koto jayga khali ache?", {"system_status"}, set(), ["GB"]),
    ("kal sokal 10 tay amake mone koriye dio client ke call korte", {"set_reminder"}, set(), []),
    ("Apollo project ta open koro", {"open_project_folder", "open_website", "browser_open"}, {"run_command"}, []),
    ("web e khuje bolo PHP er latest stable version koto", {"web_search", "fetch_url", "browser_open"}, set(), ["PHP"]),
    ("github khulo", {"open_website", "browser_open"}, {"run_command"}, []),
    ("mone rakho amar office time sokal 10 ta theke sondha 6 ta", {"remember"}, set(), []),
    ("assalamu alaikum, kemon acho?", set(), {"run_command", "power_action"}, []),
    ("inbox e notun kono mail ache?", {"gmail", "browser_open"}, {"run_command"}, []),
    ("Apollo te ki ki change kora hoyeche ekhono commit hoyni?", {"git_status", "git_diff"}, {"git_push"}, []),
    ("1 ghonta por pc ta bondho kore dio", {"power_action", "set_reminder"}, {"run_command"}, []),
    ("ajke tumi ki ki korecho?", {"show_history"}, set(), []),
    ("amar screen er ekta chobi pathao", {"take_screenshot"}, {"browser_screenshot"}, []),
]

SIMULATED = {"open_app", "open_website", "open_project_folder", "power_action", "run_command", "git_push",
             "set_reminder", "remember", "forget", "cancel_reminder", "browser_open", "browser_click", "browser_type",
             "browser_key", "browser_scroll", "browser_back", "browser_screenshot", "browser_read", "desktop_click",
             "desktop_type", "desktop_hotkey", "send_file", "make_document", "gmail", "calendar", "deploy_project",
             "call_webhook", "start_parallel_task", "edit_file", "write_file", "undo_changes", "clipboard",
             "watch_website", "take_screenshot", "create_pull_request", "git_branch", "db_query", "set_deploy_target"}


class _AutoYes:
    async def ask(self, *a, **k):
        return True


async def _simulated(inp, ctx):
    return "Done (simulated in benchmark)."


async def run_model(key_env: str, model: str) -> dict:
    if model == "zolt":
        from .brain import Brain
        provider = Brain()
        ok = bool(provider.configured())
    else:
        env_name = "BENCH_MODEL_" + str(abs(hash(model)))
        os.environ[env_name] = model
        provider = LiteLLMProvider("bench", model, key_env, env_name)
        ok = provider.available()
    score, total_time, notes = 0, 0.0, []
    if not ok:
        return {"model": model, "score": -1, "time": 0, "notes": ["no key"]}
    for i, (text, good, bad, words) in enumerate(TASKS):
        ctx = ToolContext(bot=None, chat_id=900 + i, approvals=_AutoYes())
        start = time.monotonic()
        try:
            reply = await provider.handle(900 + i, [{"type": "text", "text": text}], ctx)
        except ProviderUnavailable as e:
            notes.append(f"#{i + 1} unavailable")
            reply = ""
        except Exception as e:
            notes.append(f"#{i + 1} {type(e).__name__}")
            reply = ""
        total_time += time.monotonic() - start
        provider.reset(900 + i)
        used = set(ctx.tools_run)
        points = 0
        if (good & used) or (not good and not used):
            points += 2
        if not (bad & used):
            points += 1
        if reply.strip() and not has_foreign_script(reply):
            points += 1
        if all(w.lower() in reply.lower() for w in words):
            points += 1
        if points < 5:
            notes.append(f"#{i + 1}:{points}/5 used={sorted(used) or '-'}")
        score += points
        await asyncio.sleep(4)  # be gentle with free rate limits
    return {"model": model, "score": score, "time": total_time, "notes": notes}


async def main(models: list[str]) -> None:
    for name in SIMULATED:
        if name in _HANDLERS:
            _HANDLERS[name] = _simulated
    from . import providers  # noqa: F401  (registers all tools)
    cands = [c for c in [*CANDIDATES, ("", "zolt")] if c[1] in models or (not models and c[1] != "zolt")]
    by_key: dict[str, list[str]] = {}
    for key, model in cands:
        by_key.setdefault(key, []).append(model)

    async def one_key(key: str, ms: list[str]) -> list[dict]:
        out = []
        for m in ms:  # models sharing a key share its rate limit: run them one by one
            out.append(await run_model(key, m))
            await asyncio.sleep(20)
        return out

    results = [r for group in await asyncio.gather(*(one_key(k, ms) for k, ms in by_key.items())) for r in group]
    best = len(TASKS) * 5
    print(f"\n{'score':>7}  {'time':>6}  model")
    for r in sorted(results, key=lambda r: (-r["score"], r["time"])):
        shown = "n/a" if r["score"] < 0 else f"{r['score']}/{best}"
        print(f"{shown:>7}  {r['time']:5.0f}s  {r['model']}")
        for n in r["notes"][:6]:
            print(f"{'':16}{n}")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(main(sys.argv[1:]))
