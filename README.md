# 🤖 Zolt: আপনার Personal AI Agent

ফোনের Telegram থেকে লিখে বা ছবি পাঠিয়ে command দিন, Zolt আপনার PC-তে কাজটা করে দেবে।

## ধাপ ১-এ Zolt যা পারে

| বলুন | Zolt যা করবে |
|---|---|
| "PC off করো" / "restart দাও" / "lock করো" | Power control (off/restart/sleep-এর আগে আপনার অনুমতি নেবে) |
| "Fiverr খোলো" / "GitHub open করো" | Browser-এ website খুলবে |
| "VS Code খোলো" / "Laragon চালু করো" | App খুলবে |
| "স্ক্রিনশট দাও" | PC-র স্ক্রিনের ছবি পাঠাবে |
| "আমার project গুলো দেখাও" | `C:\laragon\www`-এর project-এর তালিকা দেবে |
| "urban-mart-এর status কী?" | git status আর শেষ commit গুলো দেখাবে |
| "urban-mart push করো" | পরিবর্তনগুলো দেখিয়ে অনুমতি নেবে, তারপর commit + push করবে |
| ছবি + লেখা পাঠানো | ছবি দেখে বুঝে উত্তর দেবে |

পরের ধাপে আসবে: Voice, Gmail outreach, Facebook/YouTube, website-এ কোড পরিবর্তন, নিজে থেকে মনে করানো, আটকে গেলে call।

## চালু করার নিয়ম (একবারই করতে হবে)

### ১. Telegram bot বানান
1. Telegram-এ **@BotFather** খুঁজে `/newbot` লিখুন
2. নাম দিন `Zolt`, username দিন যেমন `zolt_ekfa_bot` (শেষে `bot` থাকতে হবে)
3. BotFather একটা **token** দেবে। এটা গোপন রাখবেন, কাউকে দেবেন না।

### ২. Claude API key নিন
[console.anthropic.com](https://console.anthropic.com) এ গিয়ে API key বানান। কিছু credit কিনে রাখতে হবে।

### ৩. `.env` ফাইল বানান
`.env.example` কপি করে `.env` নাম দিন। তারপর token আর API key **নিজে** বসান।

### ৪. চালু করুন
`start_zolt.bat`-এ double-click করুন। অথবা:
```
python -m zolt
```

### ৫. নিজেকে owner বানান
ফোন থেকে bot-কে যেকোনো message দিন। Bot আপনার **Telegram ID** বলে দেবে।
সেটা `.env`-এ `ALLOWED_TELEGRAM_USER_ID=`-এর পরে বসিয়ে Zolt আবার চালু করুন।
এরপর শুধু আপনার কথাই Zolt শুনবে, অন্য কারো না।

### ৬. (ঐচ্ছিক) PC চালু হলেই Zolt চালু হবে
`Win + R` চেপে `shell:startup` লিখুন। যে ফোল্ডার খুলবে, সেখানে `start_zolt.bat`-এর একটা shortcut রাখুন।

## নিরাপত্তা
- শুধু `ALLOWED_TELEGRAM_USER_ID`-এর মালিকের command মানবে
- Shutdown, restart, sleep আর git push-এর আগে সবসময় ✅/❌ বাটনে অনুমতি নেবে
- Project-এর কাজ শুধু `PROJECTS_DIR`-এর ভেতরে করবে
- `.env` ফাইল কখনো GitHub-এ push হবে না (`.gitignore`-এ আছে)

## নিজের তথ্য যোগ করা
`profile.md`-এ আপনার project, পছন্দ ইত্যাদি লিখুন। Zolt সেটা পড়ে আপনার কাজ ভালো বুঝবে।

## এখন Zolt যা যা পারে (মোট ৫৪টা tool)
- PC: power, app/website খোলা, screenshot, system status, clipboard, desktop app control
- Files: যেকোনো ফাইল পড়া/edit/তৈরি, PDF/Word/Excel বানানো, Telegram-এ ফাইল নেওয়া/পাঠানো, documents search
- Coding: git status/push/branch/diff, tests, Pull Request, SSH deploy, MySQL query, command চালানো
- Web: search, page পড়া, নিজের Edge browser চালানো (login একবার নিজে করতে হয়)
- Google: Gmail + Calendar (একবার setup লাগে, Zolt-কে জিজ্ঞেস করলে নিয়ম বলবে)
- Memory, reminder, প্রতিদিন সকাল ৯টায় brief (`MORNING_BRIEF_TIME`), website down হলে alert, নতুন email alert
- Voice বোঝা + voice-এ উত্তর, ব্যাকগ্রাউন্ডে একসাথে একাধিক কাজ, n8n webhook, MCP servers (`data/mcp.json`)
- History আর undo, ১০ মিনিটের অটো-অনুমতি, "থামো" লিখলে থামে

## Auto-start
PC চালু হলেই Zolt নিজে চালু হয় (Startup folder-এ `Zolt.lnk`), crash হলে নিজেই আবার চালু হয়।
Log: `data/logs/zolt.log`। বন্ধ করতে Task Manager থেকে `pythonw.exe` (run_forever) বন্ধ করুন।
