"""
💼 Exit Engine (tp_tracker.py) — v7.1
Ladder: TP1 2x (30%), TP2 3x (20%), TP3 5x (20%), TP4 10x (10%).
v7.1 CHANGE: remainder trailing stop is now a FLAT -25% from peak mcap at
ALL multiples (the -40% widening above 3x is removed). Locks profits harder
on runners, consistent with the intraday mandate.
Risk rules: hard stop -20% pre-TP1, stall stop 60min <1.2x, max age 12h.
MAE (pre-TP1 trough) logging retained for stop-width sensitivity reports.
"""

import os
import sqlite3
import time
import requests
from datetime import datetime, timezone

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PATH = os.environ.get("DB_PATH", "solana_breakout.db")
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

TP1_MULT, TP2_MULT, TP3_MULT, TP4_MULT = 2.0, 3.0, 5.0, 10.0
TRAIL_DROP = 0.25          # 🛑 FLAT: -25% from peak mcap at every multiple
HARD_STOP_DROP = 0.20
STALL_MINUTES = 60
STALL_MIN_MULT = 1.20
MAX_AGE_HOURS = 12
BATCH_LIMIT = 30


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def parse_iso(value, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        return fallback


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
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
        time.sleep(1)
    return sent


def table_exists(conn, name: str) -> bool:
    cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None


def init_tp_table(conn) -> None:
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
            tp1 INTEGER DEFAULT 0, tp2 INTEGER DEFAULT 0,
            tp3 INTEGER DEFAULT 0, tp4 INTEGER DEFAULT 0,
            closed INTEGER DEFAULT 0,
            close_reason TEXT DEFAULT '',
            last_status_ts TEXT DEFAULT '',
            exit_mcap REAL DEFAULT 0,
            closed_ts TEXT DEFAULT '',
            entry_status TEXT DEFAULT 'active',
            limit_mcap REAL DEFAULT 0,
            entry_deadline TEXT DEFAULT '',
            size_mult REAL DEFAULT 1.0,
            mae_mcap REAL DEFAULT 0
        )
        """
    )
    conn.commit()


def ensure_columns(conn) -> None:
    if not table_exists(conn, "tp_state"):
        init_tp_table(conn)
        return
    cols = [row[1] for row in conn.execute("PRAGMA table_info(tp_state)").fetchall()]
    for name, ddl in (
        ("last_status_ts", "TEXT DEFAULT ''"), ("exit_mcap", "REAL DEFAULT 0"),
        ("closed_ts", "TEXT DEFAULT ''"), ("entry_status", "TEXT DEFAULT 'active'"),
        ("limit_mcap", "REAL DEFAULT 0"), ("entry_deadline", "TEXT DEFAULT ''"),
        ("size_mult", "REAL DEFAULT 1.0"), ("mae_mcap", "REAL DEFAULT 0"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE tp_state ADD COLUMN {name} {ddl}")
    conn.commit()


def seed_new_positions(conn) -> int:
    cur = conn.execute(
        "SELECT pair_address, token_symbol, token_address, first_alerted, mcap_at_alert "
        "FROM alerted_tokens WHERE token_address IS NOT NULL AND token_address != '' "
        "AND token_address NOT IN (SELECT token_address FROM tp_state)"
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    n = 0
    for pair, symbol, token, first_alerted, mcap in cur.fetchall():
        try:
            entry = float(mcap or 0.0)
        except (TypeError, ValueError):
            entry = 0.0
        ts = first_alerted or now_iso
        conn.execute(
            "INSERT OR IGNORE INTO tp_state (token_address, symbol, pair_address, entry_mcap, "
            "peak_mcap, last_peak_ts, started_ts, tp1, tp2, tp3, tp4, closed, close_reason, "
            "last_status_ts, exit_mcap, closed_ts, entry_status, limit_mcap, entry_deadline, "
            "size_mult, mae_mcap) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0,0,0,0, 0, '', ?, 0, '', 'active', 0, '', 1.0, ?)",
            (token, symbol or "UNKNOWN", pair or "", entry, entry, ts, ts, ts, entry),
        )
        n += 1
    conn.commit()
    return n


def fetch_mcaps(addresses: list):
    if not addresses:
        return True, {}
    batch = addresses[:BATCH_LIMIT]
    try:
        resp = requests.get(TOKENS_URL + ",".join(batch), timeout=15)
        if resp.status_code == 429:
            log("⚠️ DexScreener 429 - skipping cycle.")
            return False, {}
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log(f"⚠️ DexScreener error: {exc}")
        return False, {}
    pairs = data.get("pairs") if isinstance(data, dict) else (data if isinstance(data, list) else None)
    if not isinstance(pairs, list):
        return False, {}
    mcap_map = {}
    for p in pairs:
        if not isinstance(p, dict):
            continue
        token = (p.get("baseToken") or {}).get("address") or ""
        liq = (p.get("liquidity") or {}).get("usd") or 0
        mcap = p.get("marketCap") or p.get("fdv") or 0
        try:
            liq, mcap = float(liq), float(mcap)
        except (TypeError, ValueError):
            continue
        if not token or mcap <= 0 or liq <= 0:
            continue
        if token not in mcap_map or mcap > mcap_map[token]:
            mcap_map[token] = mcap
    return True, mcap_map


def build_message(emoji, title, symbol, entry, peak, current, action, pair) -> str:
    mult = (current / entry) if (entry > 0 and current > 0) else 0.0
    peak_mult = (peak / entry) if (entry > 0 and peak > 0) else 0.0
    lines = [
        f"{emoji} <b>{title}</b> {emoji}",
        f"🪙 <b>{symbol}</b>",
        f"💰 Entry: ${entry:,.0f} | 📡 Now: ${current:,.0f} ({mult:.2f}x)",
        f"🏔 Peak: ${peak:,.0f} ({peak_mult:.2f}x)",
        f"📋 <b>ACTION:</b> {action}",
    ]
    if pair:
        lines.append(f"🔗 <a href='https://dexscreener.com/solana/{pair}'>Chart</a>")
    return "\n".join(lines)


def evaluate_position(conn, r, now: datetime, mcap_map: dict) -> int:
    token, symbol, pair = r[0], (r[1] or "UNKNOWN"), (r[2] or "")
    entry = r[3] or 0.0
    peak = r[4] or 0.0
    last_peak_dt = parse_iso(r[5], now)
    started_dt = parse_iso(r[6], now)
    tp1, tp2, tp3, tp4 = r[7], r[8], r[9], r[10]
    last_status_dt = parse_iso(r[11], now)
    mae = r[12] or 0.0

    msgs = []
    closed, reason = 0, ""
    exit_mcap, closed_ts = 0.0, ""
    current = mcap_map.get(token)

    if entry <= 0:
        closed, reason, exit_mcap = 1, "bad_entry", 0.0
        closed_ts = now.isoformat()
    elif current is None or current <= 0:
        msgs.append(build_message("🕳️", "LIQUIDITY VANISHED", symbol, entry, peak, 0.0,
                                  "EXIT NOW IF POSSIBLE - pair unpriced (likely rug).", pair))
        closed, reason, exit_mcap = 1, "liquidity_gone", 0.0
        closed_ts = now.isoformat()
    else:
        mult = current / entry

        # 📉 MAE logging: pre-TP1 trough only (frozen once TP1 hits)
        if tp1 == 0:
            base = mae if mae > 0 else entry
            mae = min(base, current)

        if current > peak:
            peak, last_peak_dt = current, now

        # 🪜 LADDER
        if tp1 == 0 and mult >= TP1_MULT:
            tp1 = 1
            msgs.append(build_message("1️⃣", "TP1 - 2x PRINCIPAL+", symbol, entry, peak, current,
                                      "SELL 30%. Initial capital recovered; 70% rides.", pair))
        if tp1 == 1 and tp2 == 0 and mult >= TP2_MULT:
            tp2 = 1
            msgs.append(build_message("2️⃣", "TP2 - 3x LADDER", symbol, entry, peak, current,
                                      "SELL 20% of INITIAL size.", pair))
        if tp1 == 1 and tp3 == 0 and mult >= TP3_MULT:
            tp3 = 1
            msgs.append(build_message("3️⃣", "TP3 - 5x LADDER", symbol, entry, peak, current,
                                      "SELL 20% of INITIAL size.", pair))
        if tp1 == 1 and tp4 == 0 and mult >= TP4_MULT:
            tp4 = 1
            msgs.append(build_message("4️⃣", "TP4 - 10x LADDER", symbol, entry, peak, current,
                                      "SELL 10% of INITIAL size. 20% rides the -25% trail.", pair))

        # 🛑 FLAT TRAILING STOP: -25% from peak mcap at every multiple
        if closed == 0 and tp1 == 1 and peak > 0:
            drop = (peak - current) / peak
            if drop >= TRAIL_DROP:
                closed, reason, exit_mcap = 1, "trailing_stop", current
                closed_ts = now.isoformat()
                msgs.append(build_message("🛑", "TRAILING STOP (-25% from peak)",
                                          symbol, entry, peak, current,
                                          "SELL REMAINDER (20%).", pair))

        # ⛔ Hard stop -20% (pre-TP1 only)
        if closed == 0 and tp1 == 0:
            loss = (entry - current) / entry
            if loss >= HARD_STOP_DROP:
                closed, reason, exit_mcap = 1, "hard_stop", current
                closed_ts = now.isoformat()
                msgs.append(build_message("⛔", "HARD STOP (-20%)", symbol, entry, peak, current,
                                          "SELL EVERYTHING.", pair))

        # ⏱️ Stall stop
        if closed == 0 and tp1 == 0:
            age_min = (now - started_dt).total_seconds() / 60.0
            if age_min >= STALL_MINUTES and mult < STALL_MIN_MULT:
                closed, reason, exit_mcap = 1, "stall_stop", current
                closed_ts = now.isoformat()
                msgs.append(build_message("⏱️", "STALL STOP (60min, no strength)", symbol, entry, peak, current,
                                          "SELL EVERYTHING - momentum never arrived.", pair))

        age_h = (now - started_dt).total_seconds() / 3600.0
        if closed == 0 and age_h >= MAX_AGE_HOURS:
            closed, reason, exit_mcap = 1, "max_age", current
            closed_ts = now.isoformat()
            msgs.append(build_message("🏁", "MAX AGE (12h)", symbol, entry, peak, current,
                                      "Intraday window closed - exit remainder.", pair))

        if closed == 0:
            status_age_h = (now - last_status_dt).total_seconds() / 3600.0
            if status_age_h >= 1.0:
                mae_pct = ((entry - mae) / entry * 100.0) if (entry > 0 and mae > 0) else 0.0
                msgs.append(build_message("📊", "HOURLY STATUS", symbol, entry, peak, current,
                                          f"Holding {mult:.2f}x | MAE -{mae_pct:.0f}%", pair))
                last_status_dt = now

    conn.execute(
        "UPDATE tp_state SET entry_mcap=?, peak_mcap=?, last_peak_ts=?, tp1=?, tp2=?, tp3=?, tp4=?, "
        "closed=?, close_reason=?, last_status_ts=?, exit_mcap=?, closed_ts=?, mae_mcap=? "
        "WHERE token_address=?",
        (entry, peak, last_peak_dt.isoformat(), tp1, tp2, tp3, tp4,
         closed, reason, last_status_dt.isoformat(), exit_mcap, closed_ts, mae, token),
    )
    conn.commit()
    return send_all(msgs)


def main() -> None:
    log("💼 Exit Engine v7.1 (flat -25% peak trail) starting...")
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
    if seeded:
        log(f"🌱 Seeded {seeded} new position(s).")

    rows = conn.execute(
        "SELECT token_address, symbol, pair_address, entry_mcap, peak_mcap, last_peak_ts, started_ts, "
        "tp1, tp2, tp3, tp4, last_status_ts, mae_mcap FROM tp_state WHERE closed = 0"
    ).fetchall()
    if not rows:
        log("🔍 No open positions. Sleeping.")
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
    log(f"✅ Exit engine complete. {sent} Telegram alert(s) fired.")


if __name__ == "__main__":
    main()
