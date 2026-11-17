"""Loads settings from the .env file next to the project root."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _add_laragon_to_path() -> None:
    """Laragon's php/composer/mysql/node aren't on PATH by default; add the newest of each."""
    base = Path(os.getenv("LARAGON_DIR", r"C:\laragon")) / "bin"
    if not base.is_dir():
        return
    found = []
    # Prefer the versions selected in Laragon's own settings (usr/laragon.ini).
    try:
        import configparser
        ini = configparser.ConfigParser(strict=False)
        ini.read(base.parent / "usr" / "laragon.ini", encoding="utf-8-sig")
        for section, sub in (("php", "php/{}"), ("mysql", "mysql/{}/bin")):
            version = ini.get(section, "Version", fallback="")
            if version and (base / sub.format(version)).is_dir():
                found.append(str(base / sub.format(version)))
    except Exception:
        pass
    for pattern in ("php/php-*", "mysql/mysql-*/bin", "mysql/mariadb-*/bin", "nodejs/node-*"):
        hits = sorted(base.glob(pattern))
        if hits:
            found.append(str(hits[-1]))
    for fixed in ("composer", "git/bin", "git/cmd"):
        if (base / fixed).is_dir():
            found.append(str(base / fixed))
    os.environ["PATH"] = os.pathsep.join(found + [os.environ.get("PATH", "")])


_add_laragon_to_path()


def _int_or_none(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


@dataclass(frozen=True)
class Config:
    agent_name: str = os.getenv("AGENT_NAME", "Zolt")
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_user_id: int | None = _int_or_none(os.getenv("ALLOWED_TELEGRAM_USER_ID"))
    model: str = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
    effort: str = os.getenv("CLAUDE_EFFORT", "medium")
    projects_dir: Path = Path(os.getenv("PROJECTS_DIR", r"C:\laragon\www"))
    profile_file: Path = ROOT / "profile.md"
    # Friendly app names -> what to launch. Extend in .env with APP_<NAME>=path
    apps: dict[str, str] = field(default_factory=lambda: {
        "notepad": "notepad.exe",
        "calculator": "calc.exe",
        "explorer": "explorer.exe",
        "cmd": "cmd.exe",
        "terminal": "wt.exe",
        "chrome": "chrome",
        "edge": "msedge",
        "vscode": "code",
        "laragon": r"C:\laragon\laragon.exe",
        **{k[4:].lower(): v for k, v in os.environ.items() if k.startswith("APP_")},
    })

    def missing(self) -> list[str]:
        out = []
        if not self.telegram_token:
            out.append("TELEGRAM_BOT_TOKEN")
        return out

    def profile_text(self) -> str:
        try:
            return self.profile_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""


CONFIG = Config()
