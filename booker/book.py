"""
Naru booking bot — fires at Monday 8:00 PM IST sharp.

Strategy:
  1. Load armed users from Supabase
  2. T-90s: Spawn one Playwright context per user, navigate to booking page
  3. T-3s : Mint reCAPTCHA tokens (view_slot + checkout) per user
  4. T-30s onward: Poll slot availability every 2s, log all changes
  5. T+0  : Fire booking API in parallel per user (verify → register → book)
  6. T+1s : Send Razorpay payment links via Telegram
  7. T+5m : Poll booking status, send final confirmations

Every API call is timestamped and logged to logs/run-{timestamp}.jsonl for
post-run analysis. GitHub Actions uploads logs/ as an artifact every run.
"""

from __future__ import annotations
import asyncio, json, os, sys, time, traceback, uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# ════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════════════════════════
IST              = timezone(timedelta(hours=5, minutes=30))
NARU_OUTLET_ID   = 44
NARU_SHORT       = "eatnaru"
RECAPTCHA_KEY    = "6LcxXgYqAAAAAGgGKKXR0LmqZXBx-P8CjSQju1z0"
RAZORPAY_KEY_ID  = "rzp_live_WDAVg5vJre8fdX"
API_BASE         = "https://apis.airmenus.in"
BOOKING_PAGE     = "https://bookings.airmenus.in/eatnaru/order"
MAC_UA           = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/147.0.0.0 Safari/537.36")
HEADERS          = {
    "origin": "https://bookings.airmenus.in",
    "referer": "https://bookings.airmenus.in/",
    "accept": "application/json, text/plain, */*",
    "user-agent": MAC_UA,
}

# ════════════════════════════════════════════════════════════════════════════
# CONFIG (env-driven)
# ════════════════════════════════════════════════════════════════════════════
TG_TOKEN        = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SUPABASE_URL    = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY", "")
PAY_BASE_URL    = os.environ.get("PAY_BASE_URL", "")  # e.g. https://naru-bot.acme.workers.dev/pay
DRY_RUN         = os.environ.get("DRY_RUN", "false").lower() == "true"
# Format: HH:MM:SS.mmm in IST. Default = next Monday 8 PM IST.
TARGET_TIME     = os.environ.get("TARGET_TIME", "20:00:00.000")
# If empty, computes "next Monday from now"; else parses as YYYY-MM-DD
TARGET_DATE     = os.environ.get("TARGET_DATE", "")
# Observation mode: still mints tokens + polls slots, but doesn't fire booking POST.
# Use this for May 11 first run to collect data without spending money.
OBSERVE_ONLY    = os.environ.get("OBSERVE_ONLY", "false").lower() == "true"

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
RUN_ID  = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
LOG_FILE = LOG_DIR / f"run-{RUN_ID}.jsonl"

# ════════════════════════════════════════════════════════════════════════════
# OBSERVABILITY — structured JSONL logger
# Everything goes through this so we can analyze runs post-hoc.
# ════════════════════════════════════════════════════════════════════════════
_log_fp = open(LOG_FILE, "a", buffering=1)  # line-buffered

def log(event: str, **fields):
    """Emit one JSONL line and also print to stdout for GH Actions logs."""
    rec = {
        "ts": datetime.now(IST).isoformat(timespec="milliseconds"),
        "ts_ms": int(time.time() * 1000),
        "event": event,
        **fields,
    }
    line = json.dumps(rec, default=str)
    _log_fp.write(line + "\n")
    print(line, flush=True)

# ════════════════════════════════════════════════════════════════════════════
# DATA TYPES
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class User:
    id: int
    chat_id: int
    name: str
    email: str
    phone: str           # +91XXXXXXXXXX format
    party_size: int      # number of guests
    preferences: list[dict]  # ordered list of preference filters

@dataclass
class Slot:
    """A concrete (group, date, time) tuple we can attempt to book."""
    group_title: str         # e.g. "TABLE - 2 (Seats 6)"
    booking_dt_utc: str      # e.g. "2026-05-16T13:00:00.000Z" (8:30 PM IST -> 15:00 UTC)
    time_local: str          # e.g. "20:30"
    date_local: str          # e.g. "2026-05-16"
    weekday: int             # 0=Mon ... 6=Sun
    price: str               # e.g. "6000"
    max_pax: int             # 1 for tables, 3 for ramen bar
    group_config: dict       # the full group block from config — copied into booking POST

@dataclass
class BookingResult:
    user_id: int
    success: bool
    placed_booking_id: Optional[int] = None
    razorpay_order_id: Optional[str] = None
    invoice_no: Optional[int] = None
    slot: Optional[Slot] = None
    error: Optional[str] = None
    error_code: Optional[int] = None
    pay_url: Optional[str] = None

# ════════════════════════════════════════════════════════════════════════════
# SUPABASE CLIENT (REST API, no SDK needed)
# ════════════════════════════════════════════════════════════════════════════
def supa_headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

async def load_armed_users() -> list[User]:
    """Pull all users with is_armed=true from Supabase."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        log("supabase_skip", reason="no_credentials")
        return []
    async with httpx.AsyncClient(timeout=10) as cl:
        r = await cl.get(
            f"{SUPABASE_URL}/rest/v1/naru_users",
            params={"is_armed": "eq.true", "select": "*"},
            headers=supa_headers(),
        )
        r.raise_for_status()
        rows = r.json()
    users = []
    for row in rows:
        prefs = row.get("preferences") or []
        if isinstance(prefs, str): prefs = json.loads(prefs)
        users.append(User(
            id=row["id"], chat_id=row["chat_id"], name=row["name"],
            email=row["email"], phone=row["phone"],
            party_size=row.get("party_size") or 2,
            preferences=prefs,
        ))
    log("users_loaded", count=len(users),
        users=[{"id": u.id, "name": u.name, "party_size": u.party_size,
                "n_prefs": len(u.preferences)} for u in users])
    return users

async def save_attempt(user_id: int, result: BookingResult):
    """Persist booking attempt to Supabase for /history command."""
    if not SUPABASE_URL: return
    payload = {
        "user_id": user_id,
        "run_id": RUN_ID,
        "success": result.success,
        "placed_booking_id": result.placed_booking_id,
        "razorpay_order_id": result.razorpay_order_id,
        "slot": asdict(result.slot) if result.slot else None,
        "error": result.error,
        "error_code": result.error_code,
    }
    async with httpx.AsyncClient(timeout=10) as cl:
        try:
            await cl.post(f"{SUPABASE_URL}/rest/v1/naru_attempts",
                          json=payload, headers=supa_headers())
        except Exception as e:
            log("save_attempt_failed", error=str(e))

# ════════════════════════════════════════════════════════════════════════════
# TELEGRAM CLIENT
# ════════════════════════════════════════════════════════════════════════════
async def tg_send(chat_id: int, text: str, parse_mode: str = "HTML") -> bool:
    if not TG_TOKEN:
        log("tg_skip", reason="no_token", chat_id=chat_id, text=text[:80])
        return False
    async with httpx.AsyncClient(timeout=10) as cl:
        try:
            r = await cl.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
                      "disable_web_page_preview": True},
            )
            log("tg_send", chat_id=chat_id, status=r.status_code,
                preview=text[:80])
            return r.status_code == 200
        except Exception as e:
            log("tg_send_error", chat_id=chat_id, error=str(e))
            return False

# ════════════════════════════════════════════════════════════════════════════
# TIMING — sleep precisely until target IST instant
# ════════════════════════════════════════════════════════════════════════════
def compute_target_ts() -> float:
    """Return Unix timestamp of next target instant."""
    now = datetime.now(IST)
    if TARGET_DATE:
        d = datetime.strptime(TARGET_DATE, "%Y-%m-%d").date()
    else:
        # Next Monday (weekday 0). If today is Monday, use today regardless of time.
        days_ahead = (0 - now.weekday()) % 7
        if days_ahead == 0:
            d = now.date()  # Today is Monday — always use today
        else:
            d = now.date() + timedelta(days=days_ahead)
    h, m, rest = TARGET_TIME.split(":")
    s, ms = rest.split(".") if "." in rest else (rest, "0")
    target = datetime(d.year, d.month, d.day, int(h), int(m), int(s),
                      int(ms) * 1000, tzinfo=IST)
    ts = target.timestamp()
    # Safety: if target is more than 30 min in the past, log a warning but proceed
    if ts < time.time() - 1800:
        log("target_in_past_warning", target=target.isoformat(),
            now=now.isoformat(), diff_s=round(time.time() - ts))
    return ts

async def sleep_until(ts: float):
    """High-precision sleep with periodic logging."""
    while True:
        now = time.time()
        remaining = ts - now
        if remaining <= 0: return
        if remaining > 60:
            log("countdown", remaining_s=round(remaining))
            await asyncio.sleep(min(remaining - 60, 30))
        elif remaining > 5:
            await asyncio.sleep(remaining - 5)
        elif remaining > 0.1:
            await asyncio.sleep(0.05)
        else:
            # Tight loop for last 100ms — accurate to ~1ms
            while time.time() < ts:
                pass
            return

# ════════════════════════════════════════════════════════════════════════════
# AIRMENUS API — all calls go through Playwright page.evaluate() to preserve
# the IP-session-binding tied to the reCAPTCHA verify.
# ════════════════════════════════════════════════════════════════════════════
async def page_fetch(page: Page, path: str, method: str = "POST",
                     body: Optional[dict] = None) -> dict:
    """Fire an airmenus API call via httpx (not browser fetch — avoids CORS)."""
    t0 = time.time()
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=15) as cl:
            if method == "GET":
                r = await cl.get(API_BASE + path)
            else:
                r = await cl.post(API_BASE + path, json=body)
            dur = round((time.time() - t0) * 1000, 1)
            raw_text = r.text
            try:
                parsed = r.json()
            except Exception as je:
                log("json_parse_fail", error=str(je), text_len=len(raw_text), preview=raw_text[:200])
                parsed = None
            res = {"status": r.status_code, "body": parsed, "raw": raw_text[:500], "dur_ms": dur}
    except Exception as e:
        dur = round((time.time() - t0) * 1000, 1)
        res = {"status": 0, "body": None, "raw": str(e), "dur_ms": dur}
    log("api_call", path=path, method=method,
        status=res["status"], dur_ms=res["dur_ms"],
        body_preview=str(res.get("body"))[:200] if res.get("body") else None)
    return res

async def mint_token(page: Page, action: str) -> Optional[str]:
    """Mint a reCAPTCHA Enterprise token for the given action."""
    try:
        token = await page.evaluate(f"""
        () => new Promise((resolve, reject) => {{
          grecaptcha.enterprise.ready(() => {{
            grecaptcha.enterprise.execute('{RECAPTCHA_KEY}', {{action: '{action}'}})
              .then(resolve).catch(reject);
          }});
        }})
        """)
        log("recaptcha_minted", action=action, token_len=len(token))
        return token
    except Exception as e:
        log("recaptcha_mint_failed", action=action, error=str(e))
        return None

# ════════════════════════════════════════════════════════════════════════════
# SLOT DISCOVERY — fetch config, expand into bookable Slot objects
# ════════════════════════════════════════════════════════════════════════════
DAY_NAMES = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]

async def fetch_config(page: Page) -> dict:
    """Get the full /reservations/config/44/ — slot groups + per-day available_times."""
    res = await page_fetch(page, f"/api/reservations/config/{NARU_OUTLET_ID}/", "GET")
    if res["status"] != 200:
        log("config_fetch_failed", status=res["status"])
        return {}
    cfg = res["body"]
    # Snapshot the whole config to disk for unknown #3 (config stability)
    (LOG_DIR / f"config-{RUN_ID}.json").write_text(json.dumps(cfg, indent=2))
    log("config_snapshot", path=str(LOG_DIR / f"config-{RUN_ID}.json"))
    return cfg

def expand_slots(config: dict, days_ahead: int = 14) -> list[Slot]:
    """Walk the config and return every bookable (group, date, time) tuple."""
    if not config or "setting" not in config: return []
    out: list[Slot] = []
    today = datetime.now(IST).date()
    for offset in range(0, days_ahead + 1):
        d = today + timedelta(days=offset)
        day_name = DAY_NAMES[d.weekday()]
        day_cfg = config["setting"].get(day_name)
        if not day_cfg or not day_cfg.get("is_open"): continue
        for group in day_cfg.get("slot_groups", []):
            for at in group.get("available_times", []):
                h, m = at["time"].split(":")
                # Convert IST to UTC ISO string
                ist_dt = datetime(d.year, d.month, d.day, int(h), int(m), tzinfo=IST)
                utc_dt = ist_dt.astimezone(timezone.utc)
                # airmenus uses .000Z format
                booking_dt = utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                out.append(Slot(
                    group_title=group["title"],
                    booking_dt_utc=booking_dt,
                    time_local=at["time"],
                    date_local=d.isoformat(),
                    weekday=d.weekday(),
                    price=str(at.get("price", "0")),
                    max_pax=int(at.get("max_pax", 1)),
                    group_config=group,
                ))
    log("slots_expanded", total=len(out))
    return out

async def check_availability(page: Page, slot: Slot) -> int:
    """Returns pax_left for the slot's time, or -1 on error."""
    res = await page_fetch(
        page,
        f"/api/reservations/slot_group/remaining/paxs/"
        f"?group_title={slot.group_title.replace(' ', '+')}"
        f"&booking_dt={slot.booking_dt_utc}"
        f"&outlet_id={NARU_OUTLET_ID}",
        method="GET",
    )
    if res["status"] != 200 or not res["body"]: return -1
    return res["body"].get(slot.time_local, -1)

# ════════════════════════════════════════════════════════════════════════════
# PREFERENCE MATCHING — pick slots in user's priority order
# ════════════════════════════════════════════════════════════════════════════
def slot_matches_pref(slot: Slot, pref: dict) -> bool:
    """Does this slot satisfy the preference filters?"""
    # day_filter: "saturday", "any_weekend", "any_weekday", "any_day", or "YYYY-MM-DD"
    df = pref.get("day_filter", "any_day")
    if df == "any_weekend" and slot.weekday not in (5, 6): return False
    if df == "any_weekday" and slot.weekday not in (0,1,2,3,4): return False
    if df in DAY_NAMES and slot.weekday != DAY_NAMES.index(df): return False
    if "-" in df and df != slot.date_local: return False  # YYYY-MM-DD

    # time_filter: "lunch", "dinner", "any", or "HH:MM"
    tf = pref.get("time_filter", "any")
    h = int(slot.time_local.split(":")[0])
    if tf == "lunch" and h >= 17: return False
    if tf == "dinner" and h < 17: return False
    if ":" in tf and tf != slot.time_local: return False

    # group_filter: "any_table", "ramen_bar", "any", or exact title
    gf = pref.get("group_filter", "any")
    is_table = "TABLE" in slot.group_title
    if gf == "any_table" and not is_table: return False
    if gf == "ramen_bar" and is_table: return False
    if gf not in ("any","any_table","ramen_bar") and gf != slot.group_title: return False

    return True

def rank_slots_for_user(user: User, slots: list[Slot]) -> list[tuple[int, Slot]]:
    """Return [(pref_index, slot), ...] in priority order."""
    out: list[tuple[int, Slot]] = []
    for i, pref in enumerate(user.preferences or []):
        for s in slots:
            if slot_matches_pref(s, pref):
                out.append((i, s))
    # de-dup preserving order
    seen = set(); uniq = []
    for i, s in out:
        key = (s.group_title, s.booking_dt_utc)
        if key in seen: continue
        seen.add(key); uniq.append((i, s))
    return uniq

# ════════════════════════════════════════════════════════════════════════════
# BOOKING FLOW — per user, in their own browser context
# ════════════════════════════════════════════════════════════════════════════
async def book_for_user(browser: Browser, user: User,
                        target_ts: float,
                        availability_log: dict) -> BookingResult:
    """One booking attempt for one user. Owns its own browser context."""
    ctx = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        user_agent=MAC_UA,
        locale="en-GB",
    )
    page = await ctx.new_page()
    result = BookingResult(user_id=user.id, success=False)

    try:
        # ── T-90s region: load page, fetch config, pre-mint tokens ──
        log("user_session_start", user_id=user.id, name=user.name)
        await page.goto(BOOKING_PAGE, wait_until="domcontentloaded", timeout=30000)
        # reCAPTCHA only loads when user navigates through the SPA flow.
        # Inject it directly since we skip the UI entirely.
        await page.add_script_tag(url=f"https://www.google.com/recaptcha/enterprise.js?render={RECAPTCHA_KEY}")
        await page.wait_for_function(
            "() => typeof grecaptcha !== 'undefined' && grecaptcha.enterprise",
            timeout=20000,
        )

        config = await fetch_config(page)
        slots = expand_slots(config)
        ranked = rank_slots_for_user(user, slots)
        log("user_ranked_slots", user_id=user.id,
            count=len(ranked),
            top5=[{"pref": i, "slot": f"{s.group_title} {s.date_local} {s.time_local}"}
                  for i, s in ranked[:5]])

        if not ranked:
            result.error = "no_matching_slots"
            return result

        # ── Sleep until T-30s, start polling availability every 2s ──
        await sleep_until(target_ts - 30)
        polling_task = asyncio.create_task(
            poll_availability_loop(page, ranked[:5], target_ts, availability_log, user.id)
        )

        # ── At T-5s: pre-mint tokens (valid 120s, plenty of room) ──
        await sleep_until(target_ts - 5)
        view_token = await mint_token(page, f"{NARU_SHORT}_view_slot")
        checkout_token = await mint_token(page, f"{NARU_SHORT}_checkout")
        if not view_token or not checkout_token:
            result.error = "recaptcha_mint_failed"
            polling_task.cancel()
            return result

        # ── At T+0: fire booking sequence ──
        await sleep_until(target_ts)
        polling_task.cancel()  # cancel BEFORE booking to avoid page.evaluate contention
        try: await polling_task
        except asyncio.CancelledError: pass
        log("fire_start", user_id=user.id)
        t_fire = time.time()

        # verify view_slot
        r1 = await page_fetch(page, "/api/auth/verify_recaptcha/", body={
            "action": f"{NARU_SHORT}_view_slot",
            "token": view_token,
            "action_id": NARU_OUTLET_ID,
        })
        # verify checkout
        r2 = await page_fetch(page, "/api/auth/verify_recaptcha/", body={
            "action": f"{NARU_SHORT}_checkout",
            "token": checkout_token,
            "action_id": NARU_OUTLET_ID,
        })
        # register customer
        first, _, last = user.name.partition(" ")
        if not last: last = "User"
        r3 = await page_fetch(page, "/api/auth/customer/details", body={
            "first_name": first, "last_name": last,
            "email": user.email, "phone_number": user.phone,
        })

        # Try preferences in order
        for pref_idx, slot in ranked:
            if OBSERVE_ONLY or DRY_RUN:
                log("observe_skip_booking", user_id=user.id,
                    slot=f"{slot.group_title} {slot.date_local} {slot.time_local}")
                result.success = True
                result.slot = slot
                result.error = "observe_only"
                break

            booking_body = build_booking_body(user, slot)
            log("booking_attempt", user_id=user.id, pref_idx=pref_idx,
                slot=f"{slot.group_title} {slot.date_local} {slot.time_local}",
                price=slot.price,
                body_total=booking_body["total"],
                body_fee=booking_body["online_convenience_fee"],
                body_dt=booking_body["booking_dt"])

            r4 = await page_fetch(page, "/api/reservations/booking/", body=booking_body)
            if r4["status"] == 201 and r4["body"]:
                result.success = True
                result.placed_booking_id = r4["body"].get("placed_booking_id")
                result.razorpay_order_id = r4["body"].get("razorpay_order_id")
                result.invoice_no = r4["body"].get("invoice_no")
                result.slot = slot
                if PAY_BASE_URL and result.razorpay_order_id:
                    base = float(slot.price) if "TABLE" in slot.group_title else float(slot.price) * user.party_size
                    pay_total = int(base + base * 0.025)  # 2.5% convenience fee
                    result.pay_url = (
                        f"{PAY_BASE_URL}?order_id={result.razorpay_order_id}"
                        f"&amount={pay_total}"
                        f"&user={user.name.replace(' ', '+')}"
                    )
                log("booking_success", user_id=user.id,
                    total_ms=round((time.time() - t_fire) * 1000),
                    booking_id=result.placed_booking_id,
                    razorpay_order_id=result.razorpay_order_id,
                    pay_url=result.pay_url)
                break
            else:
                log("booking_failed_trying_next", user_id=user.id,
                    pref_idx=pref_idx, status=r4["status"],
                    body=str(r4.get("body"))[:200])
                result.error = f"status_{r4['status']}"
                result.error_code = r4["status"]

        log("fire_end", user_id=user.id, success=result.success,
            total_pipeline_ms=round((time.time() - t_fire) * 1000))
        return result

    except Exception as e:
        log("user_session_error", user_id=user.id,
            error=str(e), tb=traceback.format_exc())
        result.error = f"exception: {e}"
        return result
    finally:
        await ctx.close()

def build_booking_body(user: User, slot: Slot) -> dict:
    """Construct the POST /reservations/booking/ payload."""
    if "RAMEN" in slot.group_title:
        party_size = min(slot.max_pax, max(1, user.party_size))
    else:
        party_size = 1  # 1 table per booking, always
    base_total = float(slot.price) * (party_size if "RAMEN" in slot.group_title else 1)
    # Convenience fee: observed 2.5% for Naru (₹150 on ₹6000)
    convenience_fee = round(base_total * 0.025, 2)
    log("booking_body_built", user_id=user.id,
        group=slot.group_title, party_size=party_size,
        base_total=base_total, convenience_fee=convenience_fee)
    return {
        "outlet_id": NARU_OUTLET_ID,
        "total": f"{base_total:.2f}",
        "payment_mode": "paynow",
        "tax": "0.00",
        "online_convenience_fee": f"{convenience_fee:.2f}",
        "booking_dt": slot.booking_dt_utc,
        "booking_details": {
            "guests_count": party_size,
            "veg_guests_count": 0, "non_veg_guests_count": 0,
            "male_guests_count": 0, "female_guests_count": 0,
            "couple_guests_count": 0,
            "table_count": 1 if "TABLE" in slot.group_title else 0,
            "custom_guest_counts": [],
            "group": slot.group_config,  # mirror config verbatim
            "instructions": "",
        },
    }

# ════════════════════════════════════════════════════════════════════════════
# AVAILABILITY POLLER — runs from T-30s to T+5min, logs decay curve
# This is the data source for "Unknown #1" (T+0 to T+60s decay).
# ════════════════════════════════════════════════════════════════════════════
async def poll_availability_loop(page: Page, slots: list[tuple[int, Slot]],
                                 target_ts: float, out: dict, user_id: int):
    """Poll up to 5 top slots' availability every 2 seconds."""
    while time.time() < target_ts + 60:
        for _, slot in slots:
            try:
                pax = await check_availability(page, slot)
                key = f"{slot.group_title}|{slot.date_local}|{slot.time_local}"
                rel_t = round(time.time() - target_ts, 2)  # negative pre-fire
                out.setdefault(key, []).append({"t_rel": rel_t, "pax": pax})
                log("slot_pax", user_id=user_id, slot=key,
                    t_rel=rel_t, pax_left=pax)
            except Exception as e:
                log("slot_poll_error", error=str(e))
        await asyncio.sleep(2)

# ════════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ════════════════════════════════════════════════════════════════════════════
def format_success_message(user: User, r: BookingResult) -> str:
    s = r.slot
    return (
        f"🎯 <b>Booking secured!</b>\n\n"
        f"<b>Naru Noodle Bar</b>\n"
        f"📅 {s.date_local} ({DAY_NAMES[s.weekday].title()})\n"
        f"🕐 {s.time_local} IST\n"
        f"🍜 {s.group_title}\n"
        f"💰 ₹{float(s.price):.0f} (+ ₹150 fee)\n\n"
        f"<b>Pay within 5 minutes to confirm:</b>\n"
        f"{r.pay_url}\n\n"
        f"Booking #{r.invoice_no} · I'll confirm once payment goes through."
    )

def format_failure_message(user: User, r: BookingResult) -> str:
    return (
        f"😔 <b>Couldn't book this time.</b>\n\n"
        f"Reason: <code>{r.error or 'unknown'}</code>\n\n"
        f"Use /arm to update preferences for next Monday, "
        f"or check what's still available at:\n"
        f"https://bookings.airmenus.in/eatnaru/order"
    )

# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════
async def main():
    target_ts = compute_target_ts()
    target_iso = datetime.fromtimestamp(target_ts, IST).isoformat()
    now_iso = datetime.now(IST).isoformat()
    wait_s = max(0, target_ts - time.time())
    log("run_start", run_id=RUN_ID, target=target_iso, now=now_iso,
        wait_seconds=round(wait_s), dry_run=DRY_RUN, observe_only=OBSERVE_ONLY)

    users = await load_armed_users()
    if not users:
        log("no_armed_users")
        # Still run observation if no users
        if not OBSERVE_ONLY:
            log("nothing_to_do")
            return

    # If no users but observation mode, fake one to drive the poll loop
    if not users and OBSERVE_ONLY:
        users = [User(id=0, chat_id=0, name="Observer Bot",
                      email="obs@x.com", phone="+910000000000",
                      preferences=[{"day_filter": "any_day",
                                    "time_filter": "any",
                                    "group_filter": "any"}])]

    availability_log: dict = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"],
        )

        # Fire all users in parallel
        tasks = [book_for_user(browser, u, target_ts, availability_log) for u in users]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        for user, result in zip(users, results):
            if isinstance(result, Exception):
                log("user_task_exception", user_id=user.id, error=str(result))
                continue
            await save_attempt(user.id, result)
            if user.chat_id and result.success and not OBSERVE_ONLY:
                await tg_send(user.chat_id, format_success_message(user, result))
            elif user.chat_id and not result.success:
                await tg_send(user.chat_id, format_failure_message(user, result))

        await browser.close()

    # Persist the availability curve for unknown #1 analysis
    (LOG_DIR / f"availability-{RUN_ID}.json").write_text(
        json.dumps(availability_log, indent=2)
    )
    log("run_end", run_id=RUN_ID,
        successes=sum(1 for r in results if isinstance(r, BookingResult) and r.success),
        failures=sum(1 for r in results if isinstance(r, BookingResult) and not r.success),
    )
    _log_fp.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(1)
