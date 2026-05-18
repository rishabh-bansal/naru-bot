// Set Telegram webhook to point at your deployed Worker.
// Usage: TELEGRAM_BOT_TOKEN=xxx WEBHOOK_SECRET=yyy WORKER_URL=https://naru-bot.you.workers.dev node scripts/set-webhook.mjs

const TOKEN  = process.env.TELEGRAM_BOT_TOKEN;
const SECRET = process.env.WEBHOOK_SECRET;
const URL_   = process.env.WORKER_URL;

if (!TOKEN || !SECRET || !URL_) {
  console.error("Set TELEGRAM_BOT_TOKEN, WEBHOOK_SECRET, WORKER_URL");
  process.exit(1);
}

const webhookUrl = `${URL_.replace(/\/$/, "")}/tg/${SECRET}`;
const r = await fetch(`https://api.telegram.org/bot${TOKEN}/setWebhook`, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({
    url: webhookUrl,
    allowed_updates: ["message", "callback_query"],
  }),
});
console.log(await r.json());
