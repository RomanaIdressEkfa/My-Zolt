"""Zolt's own browser: Microsoft Edge driven by Playwright, with its own profile in
data/browser-profile so sign-ins (Gmail, Fiverr, GitHub...) persist after the owner
logs in once herself. Zolt never types passwords.

The model sees a page as a "snapshot": title, URL, visible text, and a numbered
list of clickable/typeable elements. It acts by element number (ref).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .memory import DATA_DIR

log = logging.getLogger("zolt.browser")
PROFILE_DIR = DATA_DIR / "browser-profile"

_SNAPSHOT_JS = r"""
() => {
  const sel = 'a[href], button, input, textarea, select, summary, [role=button], [role=link], [role=tab], ' +
              '[role=menuitem], [role=checkbox], [role=option], [role=textbox], [contenteditable=""], ' +
              '[contenteditable=true], [onclick], [tabindex]:not([tabindex="-1"])';
  document.querySelectorAll('[data-zolt-ref]').forEach(e => e.removeAttribute('data-zolt-ref'));
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(sel)) {
    if (out.length >= 150) break;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (r.bottom < 0 || r.top > innerHeight * 2.5) continue;   // near the visible area only
    if (el.disabled) continue;
    const label = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder ||
                   el.title || el.alt || el.name || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('type') || el.getAttribute('role') || '';
    const key = tag + type + label + Math.round(r.top) + Math.round(r.left);
    if (seen.has(key)) continue;
    seen.add(key);
    const ref = out.length + 1;
    el.setAttribute('data-zolt-ref', ref);
    out.push({ref, tag, type, label, href: tag === 'a' ? (el.getAttribute('href') || '').slice(0, 80) : ''});
  }
  const text = (document.body ? document.body.innerText : '').replace(/\n\s*\n+/g, '\n').slice(0, 5000);
  return {title: document.title, url: location.href, text, elements: out};
}
"""

RISKY_WORDS = ("send", "submit", "post", "publish", "delete", "remove", "pay", "buy", "order", "checkout",
               "confirm", "purchase", "transfer", "withdraw", "accept", "reply", "share", "deliver",
               "পাঠান", "পোস্ট", "মুছুন", "নিশ্চিত")


class BrowserSession:
    def __init__(self) -> None:
        self._pw = None
        self._context = None
        self.page = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        if self._context is not None:
            try:
                if self.page is None or self.page.is_closed():
                    self.page = self._context.pages[-1] if self._context.pages else await self._context.new_page()
                return self.page
            except Exception:
                await self.close()
        from playwright.async_api import async_playwright

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            str(PROFILE_DIR), channel="msedge", headless=False, no_viewport=True,
            args=["--start-maximized"],
        )
        self._context.on("page", self._on_new_page)
        self._context.on("close", lambda *_: self._forget())
        self.page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        return self.page

    def _on_new_page(self, page) -> None:
        self.page = page  # follow links that open a new tab

    def _forget(self) -> None:
        self._context, self.page = None, None

    async def close(self) -> None:
        try:
            if self._context:
                await self._context.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None
        self._forget()

    async def _settle(self, page) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(1.2)  # let client-side apps render

    async def snapshot(self) -> str:
        page = await self._ensure()
        await self._settle(page)
        data: dict[str, Any] = await page.evaluate(_SNAPSHOT_JS)
        rows = []
        for e in data["elements"]:
            extra = f" type={e['type']}" if e["type"] else ""
            href = f" -> {e['href']}" if e["href"] else ""
            rows.append(f"[{e['ref']}] <{e['tag']}{extra}> {e['label']}{href}")
        return (f"TITLE: {data['title']}\nURL: {data['url']}\n\nPAGE TEXT:\n{data['text']}\n\n"
                f"ELEMENTS (use the number as ref):\n" + "\n".join(rows))

    async def element_info(self, ref: int) -> dict[str, Any] | None:
        page = await self._ensure()
        return await page.evaluate(
            """ref => { const el = document.querySelector(`[data-zolt-ref="${ref}"]`);
                 if (!el) return null;
                 return {tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
                         autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
                         name: (el.getAttribute('name') || '').toLowerCase(),
                         label: (el.getAttribute('aria-label') || el.innerText || el.value || '').trim().slice(0, 80)}; }""",
            ref)

    async def goto(self, url: str) -> str:
        page = await self._ensure()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        return await self.snapshot()

    async def click(self, ref: int) -> str:
        page = await self._ensure()
        await page.locator(f'[data-zolt-ref="{ref}"]').first.click(timeout=10000)
        return await self.snapshot()

    async def type(self, ref: int, text: str, press_enter: bool) -> str:
        page = await self._ensure()
        loc = page.locator(f'[data-zolt-ref="{ref}"]').first
        try:
            await loc.fill(text, timeout=10000)
        except Exception:  # contenteditable editors (Gmail body etc.)
            await loc.click(timeout=10000)
            await page.keyboard.type(text, delay=10)
        if press_enter:
            await page.keyboard.press("Enter")
        return await self.snapshot()

    async def press(self, key: str) -> str:
        page = await self._ensure()
        await page.keyboard.press(key)
        return await self.snapshot()

    async def scroll(self, down: bool) -> str:
        page = await self._ensure()
        await page.mouse.wheel(0, 900 if down else -900)
        return await self.snapshot()

    async def back(self) -> str:
        page = await self._ensure()
        await page.go_back(timeout=15000)
        return await self.snapshot()

    async def screenshot(self) -> bytes:
        page = await self._ensure()
        return await page.screenshot(type="png")


BROWSER = BrowserSession()
