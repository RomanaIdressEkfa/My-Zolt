"""Copy the keys the cloud video factory needs from .env into GitHub Actions secrets of My-Zolt.

Run it yourself from the project folder:   python cloud/set_github_secrets.py
Nothing is printed except the secret names. GitHub keeps secrets encrypted; nobody can read them back.
"""
import subprocess
from pathlib import Path

from dotenv import dotenv_values

REPO = "RomanaIdressEkfa/My-Zolt"
env = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
names = ["TELEGRAM_BOT_TOKEN", "ALLOWED_TELEGRAM_USER_ID", "GEMINI_API_KEY", "GROQ_API_KEY",
         "OPENROUTER_API_KEY", "ELEVENLABS_API_KEY", "PC_TOKEN"]
values = {k: (env.get(k) or "").strip() for k in names}
values["CLOUD_URL"] = "https://zolt.devekfa.workers.dev"

print(f"Saving secrets to GitHub repo {REPO} ...")
for name, value in values.items():
    if not value:
        print(f"  {name}: (empty in .env, skipped)")
        continue
    r = subprocess.run(["gh", "secret", "set", name, "--repo", REPO], input=value.encode(), capture_output=True)
    print(f"  {name}: {'saved' if r.returncode == 0 else 'FAILED - ' + r.stderr.decode('utf-8', 'replace').strip()[-120:]}")
print("Done.")
