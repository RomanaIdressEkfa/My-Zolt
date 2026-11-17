"""Long-term memory: short facts Zolt learns about the owner, kept in data/memory.md."""
from __future__ import annotations

from datetime import datetime

from .config import ROOT

DATA_DIR = ROOT / "data"
MEMORY_FILE = DATA_DIR / "memory.md"
MAX_MEMORY_CHARS = 6000  # keep the prompt small; oldest lines drop out of the prompt first


def read_memory() -> str:
    try:
        text = MEMORY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text[-MAX_MEMORY_CHARS:]


def remember(fact: str) -> str:
    DATA_DIR.mkdir(exist_ok=True)
    line = f"- ({datetime.now():%Y-%m-%d}) {fact.strip()}\n"
    with MEMORY_FILE.open("a", encoding="utf-8") as f:
        f.write(line)
    return "Saved to long-term memory."


def forget(text: str) -> str:
    try:
        lines = MEMORY_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return "Memory is empty."
    kept = [l for l in lines if text.lower() not in l.lower()]
    MEMORY_FILE.write_text("".join(kept), encoding="utf-8")
    return f"Removed {len(lines) - len(kept)} memory line(s)."
