"""Control desktop apps through Windows UI Automation (pywinauto): list windows,
read a window's controls as a numbered list, click / type / press hotkeys.
All UIA calls run on one dedicated COM-initialised thread."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def _init_com() -> None:
    import comtypes
    comtypes.CoInitialize()


_pool = ThreadPoolExecutor(max_workers=1, initializer=_init_com, thread_name_prefix="zolt-uia")
_refs: dict[int, Any] = {}
_window: Any = None


async def _run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_pool, fn, *args)


def _desktop():
    from pywinauto import Desktop
    return Desktop(backend="uia")


def _windows() -> str:
    rows = []
    for w in _desktop().windows():
        try:
            title = w.window_text().strip()
            if title and w.is_visible():
                rows.append(title[:100])
        except Exception:
            continue
    return "Open windows:\n" + "\n".join(f"- {t}" for t in rows)


def _read(title: str) -> str:
    global _window
    matches = [w for w in _desktop().windows()
               if title.lower() in (w.window_text() or "").lower() and w.is_visible()]
    if not matches:
        return f"No open window matching '{title}'. Use desktop_windows to see titles."
    win = matches[0]
    try:
        if win.get_show_state() == 2:  # minimized
            win.restore()
        win.set_focus()
    except Exception:
        pass
    _window = win
    _refs.clear()
    rows = []
    interesting = {"Button", "Edit", "MenuItem", "ListItem", "TabItem", "CheckBox", "RadioButton", "ComboBox",
                   "Hyperlink", "TreeItem", "Document", "SplitButton", "Text"}
    for el in win.descendants():
        if len(_refs) >= 120:
            break
        try:
            ctype = el.element_info.control_type
            name = (el.window_text() or "").strip()
            if ctype not in interesting or (ctype == "Text" and not name) or not el.is_visible():
                continue
        except Exception:
            continue
        ref = len(_refs) + 1
        _refs[ref] = el
        rows.append(f"[{ref}] {ctype}: {name[:80]}")
    return f"WINDOW: {win.window_text()}\n" + "\n".join(rows)


def _click(ref: int) -> str:
    el = _refs.get(ref)
    if el is None:
        return f"No control [{ref}]. Call desktop_read first."
    el.click_input()
    return f"Clicked [{ref}] {el.window_text()[:60]}"


def _label(ref: int) -> str:
    el = _refs.get(ref)
    return (el.window_text() or "") if el is not None else ""


def _type(text: str, ref: int) -> str:
    from pywinauto.keyboard import send_keys
    if ref:
        el = _refs.get(ref)
        if el is None:
            return f"No control [{ref}]. Call desktop_read first."
        try:
            el.set_edit_text(text)
            return f"Typed into [{ref}]"
        except Exception:
            el.click_input()
    escaped = "".join("{" + c + "}" if c in "{}()+^%~[]" else c for c in text).replace("\n", "{ENTER}")
    send_keys(escaped, with_spaces=True, pause=0.01)
    return "Typed text."


def _hotkey(keys: str) -> str:
    from pywinauto.keyboard import send_keys
    mods = {"ctrl": "^", "control": "^", "shift": "+", "alt": "%", "win": "{VK_LWIN}"}
    named = {"enter": "{ENTER}", "tab": "{TAB}", "esc": "{ESC}", "escape": "{ESC}", "delete": "{DELETE}",
             "backspace": "{BACKSPACE}", "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
             "home": "{HOME}", "end": "{END}", "space": "{SPACE}", **{f"f{i}": f"{{F{i}}}" for i in range(1, 13)}}
    parts = [p.strip().lower() for p in keys.split("+") if p.strip()]
    seq = "".join(mods.get(p, "") for p in parts[:-1]) + named.get(parts[-1], parts[-1])
    send_keys(seq)
    return f"Pressed {keys}"


async def windows() -> str:
    return await _run(_windows)


async def read(title: str) -> str:
    return await _run(_read, title)


async def click(ref: int) -> str:
    return await _run(_click, ref)


async def label(ref: int) -> str:
    return await _run(_label, ref)


async def type_text(text: str, ref: int) -> str:
    return await _run(_type, text, ref)


async def hotkey(keys: str) -> str:
    return await _run(_hotkey, keys)
