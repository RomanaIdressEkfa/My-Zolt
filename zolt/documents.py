"""Read documents (PDF, Word, Excel, CSV, text, zip), make PDF/Word/Excel files,
and keyword-search her documents folder."""
from __future__ import annotations

import csv
import io
import re
import zipfile
from pathlib import Path

from .memory import DATA_DIR

INBOX = DATA_DIR / "inbox"      # files she sends on Telegram
OUTBOX = DATA_DIR / "outbox"    # files Zolt makes
DOCS = DATA_DIR / "docs"        # her knowledge base (she can drop files here)
TEXT_EXT = {".txt", ".md", ".php", ".js", ".ts", ".vue", ".json", ".html", ".css", ".py", ".env.example",
            ".sql", ".xml", ".yml", ".yaml", ".log", ".blade.php", ".jsx", ".tsx", ".ini", ".csv"}


def extract_text(path: Path, limit: int = 30000) -> str:
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            text = "\n".join(f"--- page {i + 1} ---\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages[:60]))
        elif ext == ".docx":
            import docx
            d = docx.Document(str(path))
            parts = [p.text for p in d.paragraphs]
            for t in d.tables:
                parts += [" | ".join(c.text for c in row.cells) for row in t.rows]
            text = "\n".join(parts)
        elif ext in (".xlsx", ".xlsm"):
            import openpyxl
            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets[:10]:
                parts.append(f"--- sheet {ws.title} ---")
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= 300:
                        parts.append("...")
                        break
                    parts.append(" | ".join("" if v is None else str(v) for v in row))
            text = "\n".join(parts)
        elif ext == ".zip":
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
            text = f"ZIP with {len(names)} files:\n" + "\n".join(names[:300])
        elif ext in TEXT_EXT or path.name.endswith(".blade.php"):
            text = path.read_text(encoding="utf-8", errors="replace")
        else:
            return f"(Can't read {ext} files as text. Size {path.stat().st_size} bytes.)"
    except Exception as e:
        return f"(Could not read {path.name}: {type(e).__name__}: {e})"
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit] + (f"\n...[truncated, {len(text)} chars total]" if len(text) > limit else "")


async def make_pdf(html: str, name: str) -> Path:
    """HTML -> PDF with Edge (renders Bangla fonts correctly)."""
    from playwright.async_api import async_playwright

    OUTBOX.mkdir(parents=True, exist_ok=True)
    out = OUTBOX / (Path(name).stem + ".pdf")
    if "<html" not in html.lower():
        html = ("<html><head><meta charset='utf-8'><style>body{font-family:'Segoe UI','Nirmala UI',sans-serif;"
                "margin:40px;line-height:1.5} table{border-collapse:collapse;width:100%} td,th{border:1px solid #ccc;"
                f"padding:6px}}</style></head><body>{html}</body></html>")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page()
        await page.set_content(html, wait_until="load")
        await page.pdf(path=str(out), format="A4", print_background=True,
                       margin={"top": "15mm", "bottom": "15mm", "left": "12mm", "right": "12mm"})
        await browser.close()
    return out


def make_docx(title: str, text: str, name: str) -> Path:
    import docx
    OUTBOX.mkdir(parents=True, exist_ok=True)
    d = docx.Document()
    if title:
        d.add_heading(title, level=1)
    for block in text.split("\n"):
        if block.startswith("## "):
            d.add_heading(block[3:], level=2)
        elif block.startswith(("- ", "* ")):
            d.add_paragraph(block[2:], style="List Bullet")
        else:
            d.add_paragraph(block)
    out = OUTBOX / (Path(name).stem + ".docx")
    d.save(str(out))
    return out


def make_xlsx(csv_text: str, name: str) -> Path:
    import openpyxl
    OUTBOX.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in csv.reader(io.StringIO(csv_text.strip())):
        ws.append([_num(v) for v in row])
    for cell in ws[1]:
        cell.font = openpyxl.styles.Font(bold=True)
    out = OUTBOX / (Path(name).stem + ".xlsx")
    wb.save(str(out))
    return out


def _num(v: str):
    try:
        return int(v) if v.strip().lstrip("-").isdigit() else float(v) if re.fullmatch(r"-?\d+\.\d+", v.strip()) else v
    except ValueError:
        return v


_index: dict[str, tuple[float, list[str]]] = {}


def search_docs(query: str, top: int = 8) -> str:
    folders = [DOCS, INBOX]
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1]
    if not terms:
        return "Empty query."
    scored = []
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
            cached = _index.get(str(path))
            if not cached or cached[0] != mtime:
                chunks = [c.strip() for c in re.split(r"\n\s*\n", extract_text(path, 200000)) if c.strip()]
                _index[str(path)] = cached = (mtime, chunks)
            for chunk in cached[1]:
                low = chunk.lower()
                score = sum(low.count(t) for t in terms) + 3 * sum(t in low for t in terms)
                if score:
                    scored.append((score, path.name, chunk[:600]))
    if not scored:
        return f"Nothing found for '{query}' in her documents ({DOCS} and received files)."
    scored.sort(key=lambda x: -x[0])
    return "\n\n".join(f"[{name}]\n{chunk}" for _, name, chunk in scored[:top])
