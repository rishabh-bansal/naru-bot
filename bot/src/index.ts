/**
 * Naru Bot — Telegram Bot on Cloudflare Workers (v2 rewrite)
 *
 * /arm and /addpref are STATELESS — party size and preset index travel
 * in callback_data, not in KV. Eliminates read-after-write race conditions.
 * KV is only used for /register (slow text-input flow, one-time).
 */

import { Bot, Context, InlineKeyboard, webhookCallback } from "grammy";

interface Env {
  TELEGRAM_BOT_TOKEN: string;
  SUPABASE_URL: string;
  SUPABASE_KEY: string;
  CONV: KVNamespace;
  WEBHOOK_SECRET: string;
}

const SLOT_PRESETS: { label: string; pref: any }[] = [
  { label: "🌃 Sat dinner — table",       pref: { day_filter: "saturday",    time_filter: "dinner", group_filter: "any_table" } },
  { label: "🌃 Fri dinner — table",       pref: { day_filter: "friday",      time_filter: "dinner", group_filter: "any_table" } },
  { label: "🌃 Sun dinner — table",       pref: { day_filter: "sunday",      time_filter: "dinner", group_filter: "any_table" } },
  { label: "☀️ Weekend lunch — table",    pref: { day_filter: "any_weekend", time_filter: "lunch",  group_filter: "any_table" } },
  { label: "🌃 Weeknight dinner — table", pref: { day_filter: "any_weekday", time_filter: "dinner", group_filter: "any_table" } },
  { label: "🍜 Ramen — Sat night",        pref: { day_filter: "saturday",    time_filter: "dinner", group_filter: "ramen_bar" } },
  { label: "🍜 Ramen — any weekend",      pref: { day_filter: "any_weekend", time_filter: "any",    group_filter: "ramen_bar" } },
  { label: "🎯 Any table, any time",      pref: { day_filter: "any_day",     time_filter: "any",    group_filter: "any_table" } },
  { label: "🎯 Any ramen, any time",      pref: { day_filter: "any_day",     time_filter: "any",    group_filter: "ramen_bar" } },
];

// ═══════════════════════════════════════════════════════════════
// SUPABASE
// ═══════════════════════════════════════════════════════════════
async function supa(env: Env, path: string, init: RequestInit = {}) {
  const r = await fetch(`${env.SUPABASE_URL}/rest/v1/${path}`, {
    ...init,
    headers: {
      apikey: env.SUPABASE_KEY,
      Authorization: `Bearer ${env.SUPABASE_KEY}`,
      "Content-Type": "application/json",
      Prefer: "return=representation",
      ...(init.headers || {}),
    },
  });
  if (!r.ok) console.error("SUPA_ERR", r.status, await r.clone().text());
  return r;
}

async function getUser(env: Env, chatId: number) {
  const r = await supa(env, `naru_users?chat_id=eq.${chatId}&select=*`);
  const arr = (await r.json()) as any[];
  return arr?.[0] || null;
}

async function upsertUser(env: Env, chatId: number, patch: Record<string, any>) {
  const existing = await getUser(env, chatId);
  if (existing) {
    await supa(env, `naru_users?chat_id=eq.${chatId}`, {
      method: "PATCH", body: JSON.stringify(patch),
    });
  } else {
    await supa(env, "naru_users", {
      method: "POST",
      body: JSON.stringify({ chat_id: chatId, is_armed: false, ...patch }),
    });
  }
}

async function getAttempts(env: Env, userId: number) {
  const r = await supa(env, `naru_attempts?user_id=eq.${userId}&select=*&order=created_at.desc&limit=5`);
  return (await r.json()) as any[];
}

// ═══════════════════════════════════════════════════════════════
// KV — /register text flow only
// ═══════════════════════════════════════════════════════════════
async function getConv(env: Env, chatId: number) {
  const raw = await env.CONV.get(`conv:${chatId}`);
  return raw ? JSON.parse(raw) : null;
}
async function setConv(env: Env, chatId: number, state: any) {
  await env.CONV.put(`conv:${chatId}`, JSON.stringify(state), { expirationTtl: 600 });
}
async function clearConv(env: Env, chatId: number) {
  await env.CONV.delete(`conv:${chatId}`);
}

// ═══════════════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════════════
function describePref(p: any): string {
  const D: any = { any_day:"any day", any_weekend:"weekend", any_weekday:"weekday", monday:"Mon", tuesday:"Tue", wednesday:"Wed", thursday:"Thu", friday:"Fri", saturday:"Sat", sunday:"Sun" };
  const T: any = { any:"any time", lunch:"lunch", dinner:"dinner" };
  const G: any = { any:"any", any_table:"table", ramen_bar:"ramen bar" };
  return `${D[p.day_filter]||p.day_filter} · ${T[p.time_filter]||p.time_filter} · ${G[p.group_filter]||p.group_filter}`;
}

function parsePrefs(raw: any): any[] {
  if (!raw) return [];
  if (typeof raw === "string") try { return JSON.parse(raw); } catch { return []; }
  return Array.isArray(raw) ? raw : [];
}

function armedMsg(ps: number, prefs: any[]): string {
  const lines = prefs.map((p: any, i: number) => `  ${i+1}. ${describePref(p)}`).join("\n");
  return `✅ <b>Armed for next Monday 8 PM</b>\n\nParty: ${ps}\nPreferences:\n${lines}\n\n/addpref — add fallback (max 3)\n/disarm — cancel · /status — check`;
}

// ═══════════════════════════════════════════════════════════════
// BOT
// ═══════════════════════════════════════════════════════════════
function buildBot(env: Env): Bot<Context> {
  const bot = new Bot(env.TELEGRAM_BOT_TOKEN);

  bot.catch((err) => console.error("BOT_CATCH", err.message, err.stack));

  bot.command("start", (ctx) => ctx.reply(
    "🍜 <b>Naru Booking Bot</b>\n\n" +
    "Reservations open Mon 8 PM and sell out in minutes.\n" +
    "This bot fires the API at 20:00:00.000 IST and sends you a payment link in ~1s.\n\n" +
    "1. /register — name, email, phone\n2. /arm — party size + preference\n3. Mon 8 PM — tap the link, pay, eat 🍜\n\n/help for all commands",
    { parse_mode: "HTML" }));

  bot.command("help", (ctx) => ctx.reply(
    "/register — profile setup\n/arm — arm for Monday\n/addpref — add fallback (max 3)\n/disarm — opt out\n/status — check\n/history — past attempts\n/cancel — abort flow"));

  bot.command("cancel", async (ctx) => { if (ctx.chat) { await clearConv(env, ctx.chat.id); await ctx.reply("Cancelled."); }});

  // ── /register ───────────────────────────────────────────────
  bot.command("register", async (ctx) => {
    if (!ctx.chat) return;
    await setConv(env, ctx.chat.id, { step: "reg_name", data: {} });
    await ctx.reply("What's your full name?");
  });

  // ── /arm → show party size buttons ──────────────────────────
  bot.command("arm", async (ctx) => {
    if (!ctx.chat) return;
    const user = await getUser(env, ctx.chat.id);
    if (!user || !user.name) { await ctx.reply("Please /register first."); return; }
    const kb = new InlineKeyboard();
    for (let n = 1; n <= 6; n++) { kb.text(`${n}`, `ap:${n}`); if (n === 3) kb.row(); }
    await ctx.reply("How many people?", { reply_markup: kb });
  });

  // party tapped → show presets. callback: ap:N
  bot.callbackQuery(/^ap:(\d)$/, async (ctx) => {
    if (!ctx.chat) return;
    const ps = ctx.match![1];
    const kb = new InlineKeyboard();
    SLOT_PRESETS.forEach((p, i) => kb.text(p.label, `as:${ps}:${i}`).row());
    await ctx.answerCallbackQuery(`Party of ${ps}`);
    await ctx.reply("Pick your top preference:", { reply_markup: kb });
  });

  // preset tapped → ARMED. callback: as:N:P (N=party, P=preset). NO KV.
  bot.callbackQuery(/^as:(\d):(\d)$/, async (ctx) => {
    if (!ctx.chat) return;
    const ps = parseInt(ctx.match![1]);
    const pi = parseInt(ctx.match![2]);
    const preset = SLOT_PRESETS[pi];
    if (!preset) { await ctx.answerCallbackQuery("Invalid."); return; }
    const prefs = [preset.pref];
    await upsertUser(env, ctx.chat.id, { party_size: ps, preferences: JSON.stringify(prefs), is_armed: true });
    await ctx.answerCallbackQuery("Armed!");
    await ctx.reply(armedMsg(ps, prefs), { parse_mode: "HTML" });
  });

  // ── /addpref ────────────────────────────────────────────────
  bot.command("addpref", async (ctx) => {
    if (!ctx.chat) return;
    const user = await getUser(env, ctx.chat.id);
    if (!user?.is_armed) { await ctx.reply("Use /arm first."); return; }
    const existing = parsePrefs(user.preferences);
    if (existing.length >= 3) { await ctx.reply("Already at 3 (max). Use /arm to reset."); return; }
    const kb = new InlineKeyboard();
    SLOT_PRESETS.forEach((p, i) => kb.text(p.label, `xp:${i}`).row());
    await ctx.reply(`You have ${existing.length}. Pick fallback #${existing.length + 1}:`, { reply_markup: kb });
  });

  bot.callbackQuery(/^xp:(\d)$/, async (ctx) => {
    if (!ctx.chat) return;
    const pi = parseInt(ctx.match![1]);
    const preset = SLOT_PRESETS[pi];
    if (!preset) { await ctx.answerCallbackQuery("Invalid."); return; }
    const user = await getUser(env, ctx.chat.id);
    if (!user) { await ctx.answerCallbackQuery("Not registered."); return; }
    const existing = parsePrefs(user.preferences);
    if (existing.length >= 3) { await ctx.answerCallbackQuery("Max 3."); return; }
    existing.push(preset.pref);
    await upsertUser(env, ctx.chat.id, { preferences: JSON.stringify(existing) });
    await ctx.answerCallbackQuery("Added!");
    await ctx.reply(armedMsg(user.party_size, existing), { parse_mode: "HTML" });
  });

  // ── /disarm ─────────────────────────────────────────────────
  bot.command("disarm", async (ctx) => {
    if (!ctx.chat) return;
    await upsertUser(env, ctx.chat.id, { is_armed: false });
    await ctx.reply("🔕 Disarmed.");
  });

  // ── /status ─────────────────────────────────────────────────
  bot.command("status", async (ctx) => {
    if (!ctx.chat) return;
    const user = await getUser(env, ctx.chat.id);
    if (!user) { await ctx.reply("Not registered. /register"); return; }
    if (!user.is_armed) { await ctx.reply(`${user.name} · ${user.email}\n🔕 Not armed. /arm to set up.`); return; }
    const prefs = parsePrefs(user.preferences);
    await ctx.reply(`${user.name} · ${user.email}\n\n` + armedMsg(user.party_size, prefs), { parse_mode: "HTML" });
  });

  // ── /history ────────────────────────────────────────────────
  bot.command("history", async (ctx) => {
    if (!ctx.chat) return;
    const user = await getUser(env, ctx.chat.id);
    if (!user) { await ctx.reply("Not registered."); return; }
    const att = await getAttempts(env, user.id);
    if (!att.length) { await ctx.reply("No attempts yet."); return; }
    const lines = att.map((a:any) => `${a.success?"✅":"❌"} ${(a.created_at||"?").slice(0,16).replace("T"," ")} ${a.slot?a.slot.group_title:a.error||"?"}`).join("\n");
    await ctx.reply(`<b>Recent:</b>\n<code>${lines}</code>`, { parse_mode: "HTML" });
  });

  // ── text handler (register flow) ────────────────────────────
  bot.on("message:text", async (ctx) => {
    if (!ctx.chat || !ctx.message) return;
    const text = ctx.message.text.trim();
    if (text.startsWith("/")) return;
    const conv = await getConv(env, ctx.chat.id);
    if (!conv) return;

    if (conv.step === "reg_name") {
      if (text.length < 2 || text.length > 60) { await ctx.reply("Send a valid name."); return; }
      conv.data.name = text; conv.step = "reg_email";
      await setConv(env, ctx.chat.id, conv);
      await ctx.reply("Email?");
    } else if (conv.step === "reg_email") {
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(text)) { await ctx.reply("Invalid email."); return; }
      conv.data.email = text; conv.step = "reg_phone";
      await setConv(env, ctx.chat.id, conv);
      await ctx.reply("Mobile number? (10 digits)");
    } else if (conv.step === "reg_phone") {
      const d = text.replace(/\D/g, "");
      if (d.length !== 10) { await ctx.reply("Need 10 digits."); return; }
      conv.data.phone = "+91" + d;
      await upsertUser(env, ctx.chat.id, { name: conv.data.name, email: conv.data.email, phone: conv.data.phone });
      await clearConv(env, ctx.chat.id);
      await ctx.reply(`✅ Registered.\n\n${conv.data.name}\n${conv.data.email}\n${conv.data.phone}\n\nNow /arm.`);
    }
  });

  return bot;
}

// ═══════════════════════════════════════════════════════════════
// PAYMENT PAGE
// ═══════════════════════════════════════════════════════════════
function payHtml(oid: string, amt: string, name: string): string {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pay — Naru</title>
<style>body{font-family:-apple-system,sans-serif;background:#fef2e8;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}.c{background:#fff;padding:32px;border-radius:16px;max-width:380px;box-shadow:0 4px 24px rgba(0,0,0,.08);text-align:center}h1{margin:0 0 8px;color:#d97757;font-size:24px}.a{font-size:32px;font-weight:700;margin:16px 0}button{background:#d97757;color:#fff;border:none;padding:14px 32px;border-radius:8px;font-size:16px;font-weight:600;cursor:pointer;width:100%}</style></head>
<body><div class="c"><h1>🍜 Naru Noodle Bar</h1><p>Almost there, ${name}.</p><div class="a">₹${amt}</div><button onclick="pay()">Pay now</button></div>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>function pay(){new Razorpay({key:'rzp_live_WDAVg5vJre8fdX',order_id:'${oid}',name:'Naru Noodle Bar',description:'Reservation',prefill:{name:'${name}'},theme:{color:'#d97757'},handler:function(){document.querySelector('.c').innerHTML='<h1>✅ Done</h1><p>Booking confirmed!</p>'}}).open()}setTimeout(pay,800)</script></body></html>`;
}

// ═══════════════════════════════════════════════════════════════
// WORKER
// ═══════════════════════════════════════════════════════════════
export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    if (url.pathname === "/pay") {
      const oid = url.searchParams.get("order_id") || "";
      if (!oid.startsWith("order_")) return new Response("Invalid", { status: 400 });
      return new Response(payHtml(oid, url.searchParams.get("amount")||"0", (url.searchParams.get("user")||"Guest").replace(/\+/g," ")), { headers: { "content-type": "text/html;charset=utf-8" } });
    }
    if (url.pathname === `/tg/${env.WEBHOOK_SECRET}`) {
      try { return await webhookCallback(buildBot(env), "cloudflare-mod")(req); }
      catch (e: any) { console.error("WH_ERR", e?.message); return new Response("ok"); }
    }
    return new Response("Naru Bot running.");
  },
};
