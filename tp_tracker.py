"""
💼 Cloud Take-Profit Tracker v2 (tp_tracker.py)
Runs on GitHub Actions every 2 minutes.

Silent on the 2-min intervals to prevent spam, but fires a 
📊 HOURLY STATUS UPDATE every 60 minutes for all open positions.
Fires immediate alerts for Take Profits, Trailing Stops, and Hard Stops.
"""

import os
import sqlite3
import time
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# ⚙️ CONFIG
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PATH = os.environ.get("DB_PATH", "solana_breakout.db")
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

TP1_MULT = 2.0
TP2_MULT = 3.0
TP3_MULT = 5.0
TP4_MULT = 10.0
TRAILING_DROP = 0.30       # Rule 3: -30% from peak
TIME_DECAY_HOURS = 2.0     # Rule 4: flat for 2h
HARD_STOP_DROP = 0.40      # Rule 0: -40% from entry before TP1
MAX_AGE_HOURS = 24.0       # hard cap on tracking lifetime
BATCH_LIMIT = 30           # DexScreener max addresses per request


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def parse_iso(value, fallback: datetime) -> datetime:
    if not value: return fallback
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return fallback


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        log(f"⚠️ Telegram notify failed (non-fatal): {exc}")


def send_all(messages: list) -> int:
    sent = 0
    for m in messages:
        send_telegram(m)
        sent += 1
        time.sleep(1)  # polite pacing for Telegram limits
    return sent


# ---------------------------------------------------------------------------
# 🗄️ SQLITE
# ---------------------------------------------------------------------------
def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None


def init_tp_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tp_state (
            token_address TEXT PRIMARY KEY,
            symbol TEXT DEFAULT '',
            pair_address TEXT DEFAULT '',
            entry_mcap REAL DEFAULT 0,
            peak_mcap REAL DEFAULT 0,
            last_peak_ts TEXT DEFAULT '',
            started_ts TEXT DEFAULT '',
            tp1 INTEGER DEFAULT 0,
            tp2 INTEGER DEFAULT 0,
            tp3 INTEGER DEFAULT 0,
            tp4 INTEGER DEFAULT 0,
            closed INTEGER DEFAULT 0,
            close_reason TEXT DEFAULT '',
            last_status_ts TEXT DEFAULT ''
        )
        """
    )
    conn.commit()

def ensure_columns(conn: sqlite3.Connection) -> None:
    """Self-healing migration for existing DBs."""
    if not table_exists(conn, "tp_state"):
        init_tp_table(conn)
        return
    cols = [row[1] for row in conn.execute("PRAGMA table_info(tp_state)").fetchall()]
    if 'last_status_ts' not in cols:
        conn.execute("ALTER TABLE tp_state ADD COLUMN last_status_ts TEXT DEFAULT ''")
        conn.commit()


def seed_new_positions(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "SELECT pair_address, token_symbol, token_address, first_alerted, mcap_at_alert "
        "FROM alerted_tokens WHERE token_address IS NOT NULL AND token_address != '' "
        "AND token_address NOT IN (SELECT token_address FROM tp_state)"
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    n = 0
    for pair, symbol, token, first_alerted, mcap in cur.fetchall():
        try: entry = float(mcap or 0.0)
        except (TypeError, ValueError): entry = 0.0
        ts = first_alerted or now_iso
        conn.execute(
            "INSERT OR IGNORE INTO tp_state (token_address, symbol, pair_address, "
            "entry_mcap, peak_mcap, last_peak_ts, started_ts, tp1, tp2, tp3, tp4, closed, close_reason, last_status_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, '', ?)",
            (token, symbol or "UNKNOWN", pair or "", entry, entry, ts, ts, ts),
        )
        n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# 🌐 DEXSCREENER
# ---------------------------------------------------------------------------
def fetch_mcaps(addresses: list):
    if not addresses: return True, {}
    batch = addresses[:BATCH_LIMIT]
    try:
        resp = requests.get(TOKENS_URL + ",".join(batch), timeout=15)
        if resp.status_code == 429:
            log("⚠️ DexScreener 429 rate limit - skipping cycle.")
            return False, {}
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as exc:
        log(f"⚠️ DexScreener network error: {exc}")
        return False, {}
    except ValueError as exc:
        log(f"⚠️ DexScreener bad JSON: {exc}")
        return False, {}

    pairs = data.get("pairs") if isinstance(data, dict) else (data if isinstance(data, list) else None)
    if not isinstance(pairs, list):
        log("⚠️ Unexpected DexScreener payload - skipping cycle.")
        return False, {}

    mcap_map = {}
    for p in pairs:
        if not isinstance(p, dict): continue
        token = (p.get("baseToken") or {}).get("address") or ""
        liq = (p.get("liquidity") or {}).get("usd") or 0
        mcap = p.get("marketCap") or p.get("fdv") or 0
        try:
            liq = float(liq); mcap = float(mcap)
        except (TypeError, ValueError): continue
        if not token or mcap <= 0 or liq <= 0: continue
        if token not in mcap_map or mcap > mcap_map[token]:
            mcap_map[token] = mcap
    return True, mcap_map


# ---------------------------------------------------------------------------
# 🧠 THE ENGINE
# ---------------------------------------------------------------------------
def build_message(emoji, title, symbol, entry, peak, current, action, pair) -> str:
    mult = (current / entry) if (entry > 0 and current > 0) else 0.0
    peak_mult = (peak / entry) if (entry > 0 and peak > 0) else 0.0
    lines = [
        f"{emoji} <b>{title}</b> {emoji}",
        f"🪙 <b>{symbol}</b>",
        f"💰 Entry MCap: ${entry:,.0f}",
        f"📡 Current MCap: ${current:,.0f} ({mult:.2f}x)",
        f"🏔 Peak MCap: ${peak:,.0f} ({peak_mult:.2f}x)",
        f"📋 <b>STATUS:</b> {action}",
    ]
    if pair: lines.append(f"🔗 <a href='https://dexscreener.com/solana/{pair}'>View Chart</a>")
    return "\n".join(lines)


def evaluate_position(conn: sqlite3.Connection, r, now: datetime, mcap_map: dict) -> int:
    token = r[0]
    symbol = r[1] or "UNKNOWN"
    pair = r[2] or ""
    entry = r[3] or 0.0
    peak = r[4] or 0.0
    last_peak_dt = parse_iso(r[5], now)
    started_dt = parse_iso(r[6], now)
    tp1, tp2, tp3, tp4 = r[7], r[8], r[9], r[10]
    last_status_dt = parse_iso(r[11], now)

    msgs = []
    closed = 0
    reason = ""
    current = mcap_map.get(token)

    if entry <= 0:
        closed, reason = 1, "bad_entry"
    elif current is None or current <= 0:
        msgs.append(build_message("🕳️", "LIQUIDITY VANISHED", symbol, entry, peak, 0.0, "SELL REMAINDER IF POSSIBLE - pair no longer priced (likely rug).", pair))
        closed, reason = 1, "liquidity_gone"
    else:
        mult = current / entry
        peak_mult = peak / entry if entry > 0 else 0.0
        
        if current > peak:
            peak = current
            last_peak_dt = now

        # Rule 1: principal recovery at 2x
        if tp1 == 0 and mult >= TP1_MULT:
            tp1 = 1
            msgs.append(build_message("1️⃣", "TP1 - PRINCIPAL RECOVERED", symbol, entry, peak, current, "SELL 50% of position - initial investment is back.", pair))

        # Rule 2: ladder at 3x / 5x / 10x
        if tp1 == 1 and tp2 == 0 and mult >= TP2_MULT:
            tp2 = 1
            msgs.append(build_message("2️⃣", "TP2 - 3x LADDER", symbol, entry, peak, current, "SELL 10% of INITIAL size.", pair))
        if tp1 == 1 and tp3 == 0 and mult >= TP3_MULT:
            tp3 = 1
            msgs.append(build_message("3️⃣", "TP3 - 5x LADDER", symbol, entry, peak, current, "SELL 10% of INITIAL size.", pair))
        if tp1 == 1 and tp4 == 0 and mult >= TP4_MULT:
            tp4 = 1
            msgs.append(build_message("4️⃣", "TP4 - 10x LADDER", symbol, entry, peak, current, "SELL 10% of INITIAL size.", pair))

        # Rule 3: trailing stop (-30% from peak), armed only after TP1
        if closed == 0 and tp1 == 1 and peak > 0:
            drop = (peak - current) / peak
            if drop >= TRAILING_DROP:
                closed, reason = 1, "trailing_stop"
                msgs.append(build_message("🛑", "TRAILING STOP HIT", symbol, entry, peak, current, f"SELL REMAINDER - price dropped {drop * 100:.0f}% from peak.", pair))

        # Rule 0: hard stop before TP1
        if closed == 0 and tp1 == 0 and HARD_STOP_DROP > 0:
            loss = (entry - current) / entry
            if loss >= HARD_STOP_DROP:
                closed, reason = 1, "hard_stop"
                msgs.append(build_message("⛔", "HARD STOP-LOSS", symbol, entry, peak, current, f"SELL EVERYTHING - down {loss * 100:.0f}% from entry.", pair))

        # Rule 4: time decay (no new peak for 2h)
        if closed == 0:
            flat_h = (now - last_peak_dt).total_seconds() / 3600.0
            if flat_h >= TIME_DECAY_HOURS:
                closed, reason = 1, "time_decay"
                msgs.append(build_message("⏳", "TIME DECAY - MOMENTUM DEAD", symbol, entry, peak, current, f"SELL REMAINDER - no new peak for {flat_h:.1f}h.", pair))

        # Max tracking age
        age_h = (now - started_dt).total_seconds() / 3600.0
        if closed == 0 and age_h >= MAX_AGE_HOURS:
            closed, reason = 1, "max_age"
            msgs.append(build_message("🏁", "MAX TRACKING AGE (24h)", symbol, entry, peak, current, "Tracking stopped - manage any remainder manually.", pair))

        # 📊 HOURLY STATUS UPDATE (v2 Feature)
        if closed == 0:
            status_age_h = (now - last_status_dt).total_seconds() / 3600.0
            if status_age_h >= 1.0:
                msgs.append(build_message(
                    "📊", "HOURLY STATUS UPDATE", symbol, entry, peak, current,
                    f"Holding... Current: {mult:.2f}x | Peak: {peak_mult:.2f}x", pair))
                last_status_dt = now 

    conn.execute(
        "UPDATE tp_state SET peak_mcap=?, last_peak_ts=?, tp1=?, tp2=?, tp3=?, tp4=?, "
        "closed=?, close_reason=?, last_status_ts=? WHERE token_address=?",
        (peak, last_peak_dt.isoformat(), tp1, tp2, tp3, tp4, closed, reason, last_status_dt.isoformat(), token),
    )
    conn.commit()
    return send_all(msgs)


# ---------------------------------------------------------------------------
# 🚀 MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    log("💼 Cloud Take-Profit Tracker v2 starting...")
    if not os.path.exists(DB_PATH):
        log("⚠️ DB not found yet. Exiting safely.")
        return

    conn = sqlite3.connect(DB_PATH)
    ensure_columns(conn)

    if not table_exists(conn, "alerted_tokens"):
        log("⚠️ alerted_tokens table missing. Exiting safely.")
        conn.close()
        return

    seeded = seed_new_positions(conn)
    if seeded: log(f"🌱 Seeded {seeded} new position(s) from breakout alerts.")

    rows = conn.execute(
        "SELECT token_address, symbol, pair_address, entry_mcap, peak_mcap, "
        "last_peak_ts, started_ts, tp1, tp2, tp3, tp4, last_status_ts "
        "FROM tp_state WHERE closed = 0"
    ).fetchall()

    if not rows:
        log("🔍 No open positions. Sleeping until next cycle.")
        conn.close()
        return

    log(f"📡 Watching {len(rows)} open position(s)...")
    ok, mcap_map = fetch_mcaps([r[0] for r in rows])
    if not ok:
        log("⚠️ DexScreener fetch failed - skipping cycle, positions untouched.")
        conn.close()
        return

    now = datetime.now(timezone.utc)
    sent = 0
    for r in rows:
        sent += evaluate_position(conn, r, now, mcap_map)

    conn.close()
    log(f"✅ TP Tracker complete. {sent} Telegram alert(s) fired.")


if __name__ == "__main__":
    main()
