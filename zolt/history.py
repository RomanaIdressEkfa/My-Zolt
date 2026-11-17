"""Action history (data/history.jsonl) and file backups for undo (data/backups/)."""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .memory import DATA_DIR

HISTORY = DATA_DIR / "history.jsonl"
BACKUPS = DATA_DIR / "backups"
BACKUP_INDEX = BACKUPS / "index.jsonl"


def log_action(name: str, tool_input: Any, result: str, is_error: bool) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    row = {"time": datetime.now().isoformat(timespec="seconds"), "tool": name,
           "input": str(tool_input)[:300], "ok": not is_error, "result": result[:300]}
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def recent(n: int = 20) -> str:
    try:
        lines = HISTORY.read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return "No history yet."
    out = []
    for line in lines:
        r = json.loads(line)
        out.append(f"{r['time']} {'✓' if r['ok'] else '✗'} {r['tool']} {r['input'][:120]}")
    return "\n".join(out)


def backup(path: Path) -> None:
    """Call before changing a file; undo_last() restores it (or deletes it if it was new)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    BACKUPS.mkdir(parents=True, exist_ok=True)
    entry = {"time": stamp, "original": str(path), "existed": path.exists(), "backup": ""}
    if path.exists():
        dest = BACKUPS / stamp / path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = 1
        while dest.exists():
            dest = dest.with_name(f"{path.stem}.{n}{path.suffix}")
            n += 1
        shutil.copy2(path, dest)
        entry["backup"] = str(dest)
    with BACKUP_INDEX.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def undo_last(steps: int = 1) -> str:
    try:
        entries = [json.loads(l) for l in BACKUP_INDEX.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return "Nothing to undo."
    if not entries:
        return "Nothing to undo."
    done = []
    for entry in reversed(entries[-steps:]):
        original = Path(entry["original"])
        if entry["existed"]:
            shutil.copy2(entry["backup"], original)
            done.append(f"restored {original}")
        elif original.exists():
            original.unlink()
            done.append(f"deleted new file {original}")
    remaining = entries[:-steps]
    BACKUP_INDEX.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in remaining), encoding="utf-8")
    return "; ".join(done) or "Nothing changed."
