"""Watchdog: keeps Zolt running. Restarts it if it crashes, logs to data/logs/zolt.log.
Started at Windows login by start_zolt_hidden.vbs (no console window)."""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "data" / "logs"
LOGS.mkdir(parents=True, exist_ok=True)
LOCK = ROOT / "data" / "watchdog.pid"


def already_running() -> bool:
    try:
        import psutil
        pid = int(LOCK.read_text())
        return psutil.pid_exists(pid) and pid != __import__("os").getpid() and \
            "run_forever" in " ".join(psutil.Process(pid).cmdline())
    except Exception:
        return False


def main() -> None:
    if already_running():
        return
    LOCK.write_text(str(__import__("os").getpid()))
    python = Path(sys.executable)
    console_python = python.with_name("python.exe") if python.name.lower() == "pythonw.exe" else python
    fails = 0
    while True:
        started = time.time()
        with open(LOGS / "zolt.log", "a", encoding="utf-8") as log:
            log.write(f"\n=== starting Zolt {time.ctime()} ===\n")
            log.flush()
            proc = subprocess.Popen([str(console_python), "-m", "zolt"], cwd=ROOT, stdout=log, stderr=log,
                                    creationflags=subprocess.CREATE_NO_WINDOW,
                                    env={**__import__("os").environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
            proc.wait()
            log.write(f"=== Zolt exited with code {proc.returncode} at {time.ctime()} ===\n")
        log_file = LOGS / "zolt.log"
        if log_file.stat().st_size > 5_000_000:  # keep one old log
            log_file.replace(LOGS / "zolt.old.log")
        fails = fails + 1 if time.time() - started < 60 else 0
        time.sleep(min(300, 10 * (2 ** min(fails, 5))))  # back off if it keeps crashing right away


if __name__ == "__main__":
    main()
