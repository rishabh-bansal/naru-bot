# 🍜 Naru Booking Bot

> Naru Noodle Bar reservations open every Monday at 8 PM IST and sell out in minutes. This bot fires the booking API at exactly `20:00:00.000` and DMs you a Razorpay payment link in <1 second.

## Architecture

```
Telegram                            Supabase                       GitHub Actions
   ↓                                   ↓                                ↓
Cloudflare Worker (grammY bot)  →  users table             ←  Python booker (Playwright)
   ↓                                                              ↓
/start /register /arm /status                              Mondays 19:50 IST → fires at 20:00:00.000
                                                                  ↓
                                                          POST to airmenus API
                                                                  ↓
                                                          razorpay_order_id
                                                                  ↓
                                                          Telegram DM with /pay link
```

**All three services on free tiers. Zero ongoing cost.**

The "moat" we cracked: airmenus.in uses **reCAPTCHA Enterprise + IP-bound session state** for booking auth. Our bot mints reCAPTCHA tokens via a real headless Chromium (Playwright), then fires the booking POST from inside the same browser context — same IP, same TLS fingerprint — bypassing the form-fill latency that costs human users 20-40 seconds.

End-to-end booking time: **~500ms from T+0** (20:00:00.000 IST).

## How fast is fast enough?

The script runs from a GitHub Actions runner (Microsoft Azure, typically US-East). RTT to AWS Mumbai (where airmenus is hosted) is ~120-180ms. So a 3-call sequence finishes in roughly 500-700ms.

A human clicking through the same flow takes 20-40 seconds (load page → pick date → pick group → pick time → fill form → click PROCEED). The bot beats this by 30-80×.

## Setup

You'll need:
1. A Telegram bot (chat with [@BotFather](https://t.me/BotFather), get a token)
2. A free Supabase project ([supabase.com](https://supabase.com))
3. A free Cloudflare account
4. A GitHub account
5. ~30 minutes

### 1. Database (Supabase)

- Create a new Supabase project (any region, but Mumbai/Singapore is closest to airmenus).
- In **SQL Editor**, paste the contents of `supabase/schema.sql` and run.
- From **Settings → API**, grab:
  - `Project URL` (e.g. `https://abc123.supabase.co`)
  - `service_role` key (NOT the anon key — we need writes)

### 2. Telegram bot (Cloudflare Worker)

```bash
cd bot
npm install
npx wrangler login
```

Create the KV namespace for conversation state:
```bash
npx wrangler kv:namespace create CONV
```
Paste the returned `id` into `wrangler.toml` under `[[kv_namespaces]]`.

Generate a webhook secret (any random string):
```bash
openssl rand -hex 16
# example output: a1b2c3d4e5f6...
```

Set Worker secrets:
```bash
npx wrangler secret put TELEGRAM_BOT_TOKEN     # paste BotFather token
npx wrangler secret put SUPABASE_URL           # paste Supabase URL
npx wrangler secret put SUPABASE_KEY           # paste service_role key
npx wrangler secret put WEBHOOK_SECRET         # paste the random string from above
```

Deploy:
```bash
npx wrangler deploy
# Note the Worker URL, e.g. https://naru-bot.yourname.workers.dev
```

Register the Telegram webhook:
```bash
TELEGRAM_BOT_TOKEN=<token> \
WEBHOOK_SECRET=<secret> \
WORKER_URL=https://naru-bot.yourname.workers.dev \
node scripts/set-webhook.mjs
```

Open your bot in Telegram, send `/start`. If you see the welcome message, you're done.

### 3. Booker (GitHub Actions)

- Push this repo to GitHub. **Public is fine — and gives you unlimited free Actions minutes.** The Worker has `WEBHOOK_SECRET` and Supabase has RLS-disabled-but-keyed access, so there are no real secrets in the repo itself.
- In **Settings → Secrets and variables → Actions**, add:
  - `TELEGRAM_BOT_TOKEN` (same token as above)
  - `SUPABASE_URL`
  - `SUPABASE_SERVICE_KEY` (the service_role key)
  - `PAY_BASE_URL` = `https://naru-bot.yourname.workers.dev/pay`

The workflow at `.github/workflows/book.yml` runs every Monday at 14:20 UTC (19:50 IST). The script then sleeps until 20:00:00.000 IST exactly before firing.

### 4. Test it

Trigger a manual dry-run from GitHub **Actions → Naru Booking Run → Run workflow**:
- `target_time`: `20:00:00.000`
- `target_date`: pick any future date for a quick test
- `observe_only`: ✅ checked
- `dry_run`: ✅ checked

This runs the entire flow against the real Naru API **except** the final booking POST — no money spent, no slot held. You'll see the log artifact attached to the run with everything that happened.

## Usage (as a user)

Open the bot in Telegram, then:

```
/start         → onboarding
/register      → name, email, phone
/arm           → pick up to 3 preference slots
/status        → see what's armed
/disarm        → opt out
/history       → past attempts
```

When Monday 8 PM hits, you'll either get a payment link (tap → UPI → done) or a failure message with a link to manually pick what's left.

## Observability

Every run dumps to `booker/logs/`:
- `run-{timestamp}.jsonl` — every API call, every reCAPTCHA mint, every slot poll, timestamped to the millisecond
- `config-{timestamp}.json` — snapshot of `/reservations/config/44/` for diffing weekly
- `availability-{timestamp}.json` — slot availability curve from T-30s to T+60s for the top 5 slots

GitHub Actions uploads all of this as an artifact on every run (retention: 30 days).

The first live run answers everything we don't yet know:
- **T+0 to T+60s decay curve** — `availability-*.json` plotted gives the drain rate
- **Slot-flip timestamp** — exact server-side flip captured in `run-*.jsonl`
- **Config stability** — diff this week's `config-*.json` against last week's
- **Rate limiting** — every `api_call` event has a status code
- **UA / fingerprint check** — runner geo logged at start
- **Razorpay TTL** — order creation timestamp logged; auto-cancellation observable from booking status polling

## Tweet-worthy talking points

When you eventually post about this:

- **Sub-second booking pipeline** from a free GitHub Actions runner — no infra
- **No browser automation clicks** — pure API calls inside a warm Playwright context
- **No auth tokens to manage** — airmenus is purely IP+reCAPTCHA gated
- **Multi-user Telegram bot** with conversational preference setting
- **Entire stack costs ₹0/month** — Cloudflare Worker + Supabase + GitHub Actions all free tier
- **Hosts Razorpay checkout** on the same Worker — no payment integration needed

## What this bot is NOT

- Not a scalper. Each booking is paid for by the actual diner via Razorpay. No re-selling. No held inventory.
- Not bypassing payment. Razorpay still charges the user; we just compress the steps before payment.
- Not unfair. The same code runs identically for whoever uses it — first-come-first-served at registration, first-byte-wins at the airmenus server.

## License

MIT. Fork freely.

## Stability disclaimer

This is one person's side project. The airmenus API could change overnight. reCAPTCHA could get stricter. Naru could add OTP. If any of those happen, the bot breaks and I'll fix it when I can. Don't bet your anniversary on it.
