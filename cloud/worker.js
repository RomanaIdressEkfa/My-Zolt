// Zolt cloud gateway (Cloudflare Worker) — always on.
//
// Telegram sends every update here (webhook). If her PC is online, the update is queued for the PC
// Zolt, which pulls it within a second or two (POST /pc/sync). If the PC is off, this Worker answers
// by itself with free Gemini: chat, reminders, and videos (it starts the GitHub Actions "video
// factory", which renders the cartoon and sends it to Telegram). Jobs that need the PC itself
// (open an app, edit a project...) wait in a queue until the PC comes back.

const TG = (env, method) => `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`;
const PC_FRESH_MS = 45_000;            // PC counts as online if it synced within this window
const HISTORY_TURNS = 12;

async function tg(env, method, body) {
  const r = await fetch(TG(env, method), {method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify(body)});
  return r.json();
}
const say = (env, text) => tg(env, "sendMessage", {chat_id: Number(env.OWNER_ID), text: text.slice(0, 4000)});

async function kvJSON(env, key, fallback) {
  const v = await env.ZOLT.get(key);
  if (!v) return fallback;
  try { return JSON.parse(v); } catch { return fallback; }
}
const putJSON = (env, key, value) => env.ZOLT.put(key, JSON.stringify(value));

// The PC's inbox and heartbeat live in one Durable Object: strongly consistent, so the PC never gets a
// message twice or late (KV is eventually consistent across Cloudflare's locations).
export class Hub {
  constructor(state) { this.storage = state.storage; }
  async fetch(request) {
    const path = new URL(request.url).pathname;
    if (path === "/push") {           // a Telegram update for the PC
      const inbox = (await this.storage.get("inbox")) || [];
      inbox.push(await request.json());
      await this.storage.put("inbox", inbox);
      return new Response("ok");
    }
    if (path === "/take") {           // PC heartbeat: hand over everything waiting
      await this.storage.put("pc_seen", Date.now());
      const inbox = (await this.storage.get("inbox")) || [];
      if (inbox.length) await this.storage.put("inbox", []);
      return Response.json(inbox);
    }
    if (path === "/seen") return new Response(String((await this.storage.get("pc_seen")) || 0));
    return new Response("?", {status: 404});
  }
}
const hub = env => env.HUB.get(env.HUB.idFromName("main"));

async function pcOnline(env) {
  const seen = Number(await (await hub(env).fetch("https://hub/seen")).text());
  return Date.now() - seen < PC_FRESH_MS;
}

// ---------------------------------------------------------------- the cloud brain (free Gemini)

const SYSTEM = `You are Zolt, the personal AI agent of a Bangladeshi developer and storyteller ("ম্যাম").
Her PC is OFF right now, so you are answering from the cloud. Reply in Bangla (Bengali script), short and warm,
address her as "ম্যাম". Keep technical words in English.
Decide what she wants and answer ONLY with JSON:
{"action": "chat" | "video" | "reminder" | "pc_task", "reply": "<what you say to her in Bangla>",
 "request": "<for video: her full request, including the story idea, length, style>",
 "when": "<for reminder: ISO 8601 date-time in Asia/Dhaka (+06:00)>", "text": "<for reminder: what to remind>"}
- "video": she wants a reel/cartoon/story video. The cloud video factory will make it (8-12 minutes) and send it.
  In "reply" say you started it and roughly when it will arrive.
- "reminder": she asks to be reminded at a time.
- "pc_task": it needs her PC itself (open an app or website on the PC, screenshot, power, files or projects on the PC,
  git/deploy from the PC, Gmail with the PC). Say it will be done as soon as the PC is on.
- "chat": anything else (questions, advice, story ideas, writing). Answer fully in "reply".
Her saved notes and preferences:
`;

async function brain(env, userText) {
  const memory = (await env.ZOLT.get("memory")) || "";
  const history = await kvJSON(env, "history", []);
  const now = new Date().toLocaleString("en-GB", {timeZone: "Asia/Dhaka"});
  const contents = [...history, {role: "user", parts: [{text: userText}]}];
  const body = {
    systemInstruction: {parts: [{text: SYSTEM + memory.slice(-6000) + `\nCurrent time in Dhaka: ${now}`}]},
    contents,
    generationConfig: {responseMimeType: "application/json", temperature: 0.7},
  };
  let out = null;
  for (const model of (env.GEMINI_MODELS || "gemini-flash-latest,gemini-flash-lite-latest").split(",")) {
    const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model.trim()}:generateContent?key=${env.GEMINI_API_KEY}`,
      {method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify(body)});
    if (!r.ok) continue;
    const j = await r.json();
    const text = j?.candidates?.[0]?.content?.parts?.map(p => p.text || "").join("") || "";
    try { out = JSON.parse(text); break; } catch { out = {action: "chat", reply: text}; break; }
  }
  if (!out) out = {action: "chat", reply: "😔 ম্যাম, এই মুহূর্তে AI সাড়া দিচ্ছে না। কয়েক মিনিট পরে আবার বলুন।"};
  history.push({role: "user", parts: [{text: userText}]}, {role: "model", parts: [{text: out.reply || ""}]});
  await putJSON(env, "history", history.slice(-2 * HISTORY_TURNS));
  return out;
}

async function startVideo(env, request) {
  const r = await fetch(`https://api.github.com/repos/${env.GH_REPO}/actions/workflows/zolt-video.yml/dispatches`, {
    method: "POST",
    headers: {"authorization": `Bearer ${env.GH_TOKEN}`, "accept": "application/vnd.github+json",
              "user-agent": "zolt-cloud", "x-github-api-version": "2022-11-28"},
    body: JSON.stringify({ref: env.GH_BRANCH || "main", inputs: {request: request.slice(0, 6000)}}),
  });
  return r.status === 204;
}

async function transcribe(env, fileId) {   // voice notes while the PC is off: Groq Whisper (free)
  const info = await tg(env, "getFile", {file_id: fileId});
  const path = info?.result?.file_path;
  if (!path || !env.GROQ_API_KEY) return "";
  const audio = await (await fetch(`https://api.telegram.org/file/bot${env.TELEGRAM_BOT_TOKEN}/${path}`)).arrayBuffer();
  const form = new FormData();
  form.append("file", new Blob([audio]), "voice.ogg");
  form.append("model", "whisper-large-v3");
  form.append("language", "bn");
  const r = await fetch("https://api.groq.com/openai/v1/audio/transcriptions",
    {method: "POST", headers: {authorization: `Bearer ${env.GROQ_API_KEY}`}, body: form});
  return r.ok ? ((await r.json()).text || "") : "";
}

async function handleOffline(env, update) {
  const msg = update.message;
  if (!msg) return;                                   // buttons etc. only make sense on the PC
  let text = msg.text || msg.caption || "";
  if (msg.voice || msg.audio) {
    const heard = await transcribe(env, (msg.voice || msg.audio).file_id);
    if (heard) { text = heard; await say(env, `🎙️ শুনলাম: ${heard}`); }
  }
  if (msg.photo || msg.document) {
    // pictures and files need the PC (vision / file reading): keep them for later
    const tasks = await kvJSON(env, "pc_tasks", []);
    tasks.push(update);
    await putJSON(env, "pc_tasks", tasks);
    await say(env, "📎 ম্যাম, ফাইল/ছবিটা রেখে দিলাম। PC চালু হলেই দেখে উত্তর দেব।");
    return;
  }
  if (!text.trim()) return;
  if (text.startsWith("/start")) { await say(env, "⚡ Zolt (cloud) চালু আছে, ম্যাম। PC বন্ধ থাকলেও কথা বলা, reminder আর video চলবে।"); return; }
  await tg(env, "sendChatAction", {chat_id: Number(env.OWNER_ID), action: "typing"});
  const out = await brain(env, text);
  if (out.action === "video") {
    const ok = await startVideo(env, out.request || text);
    await say(env, ok ? (out.reply || "🎬 ভিডিও বানানো শুরু করলাম, ম্যাম। ৮-১২ মিনিটের মধ্যে এখানে পাঠাব।")
                      : "⚠️ ম্যাম, video কারখানা চালু করতে পারলাম না। PC চালু হলে বানিয়ে দেব।");
    if (!ok) { const t = await kvJSON(env, "pc_tasks", []); t.push(update); await putJSON(env, "pc_tasks", t); }
    return;
  }
  if (out.action === "reminder" && out.when) {
    const at = Date.parse(out.when);
    if (!isNaN(at)) {
      const list = await kvJSON(env, "reminders", []);
      list.push({at, text: out.text || text});
      await putJSON(env, "reminders", list);
    }
  }
  if (out.action === "pc_task") {
    const tasks = await kvJSON(env, "pc_tasks", []);
    tasks.push(update);
    await putJSON(env, "pc_tasks", tasks);
  }
  await say(env, out.reply || "✅");
}

// ---------------------------------------------------------------- HTTP + cron

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === `/tg/${env.WEBHOOK_SECRET}` && request.method === "POST") {
      const update = await request.json();
      const from = update.message?.from?.id ?? update.callback_query?.from?.id;
      if (String(from) !== String(env.OWNER_ID)) return new Response("ok");   // only her
      if (await pcOnline(env)) {
        await hub(env).fetch("https://hub/push", {method: "POST", body: JSON.stringify(update)});
      } else {
        ctx.waitUntil(handleOffline(env, update));
      }
      return new Response("ok");
    }

    // PC Zolt: heartbeat + pick up everything waiting for it
    if (url.pathname === "/pc/sync" && request.method === "POST") {
      if (request.headers.get("x-zolt-token") !== env.PC_TOKEN) return new Response("no", {status: 403});
      const body = await request.json().catch(() => ({}));
      if (body.memory) await env.ZOLT.put("memory", String(body.memory).slice(-20000));
      const inbox = await (await hub(env).fetch("https://hub/take", {method: "POST"})).json();
      const tasks = await kvJSON(env, "pc_tasks", []);
      if (tasks.length) await putJSON(env, "pc_tasks", []);
      return Response.json({updates: [...tasks, ...inbox]});
    }

    // the video factory reads her preferences
    if (url.pathname === "/memory" && request.headers.get("x-zolt-token") === env.PC_TOKEN)
      return new Response((await env.ZOLT.get("memory")) || "");

    return new Response("Zolt cloud is running.");
  },

  async scheduled(event, env, ctx) {   // every minute: due reminders
    const list = await kvJSON(env, "reminders", []);
    const now = Date.now();
    const due = list.filter(r => r.at <= now), rest = list.filter(r => r.at > now);
    for (const r of due) await say(env, `⏰ মনে করিয়ে দিচ্ছি, ম্যাম: ${r.text}`);
    if (due.length) await putJSON(env, "reminders", rest);
  },
};
