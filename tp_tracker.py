"""
💼 Cloud Take-Profit Tracker v5 (tp_tracker.py)
v5 UPGRADE (Phase 7):
  1. PENDING entries: positions seed as limit orders at -10% of alert mcap,
     valid 15 minutes. No fill = cancelled (never counted as a trade).
  2. PROVE-IT STOP: if not >= +25% within 45 minutes of fill, close at market.
  3. CONVERGENCE GATE: full size (1.0) only if the mint appears in Phase 6
     convergence_events (72h); otherwise half size (0.5).
  4. Circuit breaker hard stop -25%, dynamic trailing (-25% / -40% above 3x).
"""

import os
import sqlite3
import time
import requests
from datetime import datetime, timezone, timedelta

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PATH = os.environ.get("DB_PATH", "solana_breakout.db")
SMART_MONEY_DB = os.environ.get("SMART_MONEY_DB", "solana_smart_money.db")
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

TP1_MULT, TP2_MULT, TP3_MULT, TP4_MULT = 2.0, 3.0, 5.0, 10.0
TRAIL_TIGHT, TRAIL_WIDE = 0.25, 0.40
HARD_STOP_DROP = 0.25
PROVE_IT_MULT = 1.25
PROVE_IT_MINUTES = 45
PULLBACK_PCT = 0.10
ENTRY_WINDOW_MIN = 15
GATE_WINDOW_H = 72
TIME_DECAY_HOURS = 2.0
MAX_AGE_HOURS = 24.0
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
            size_mult REAL DEFAULT 1.0
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
        ("size_mult", "REAL DEFAULT 1.0"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE tp_state ADD COLUMN {name} {ddl}")
    conn.commit()


def convergence_gate(token: str) -> float:
    if not os.path.exists(SMART_MONEY_DB):
        return 0.5
    try:
        c2 = sqlite3.connect(f"file:{SMART_MONEY_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0.5
    try:
        cutoff = int(time.time()) - GATE_WINDOW_H * 3600
        row = c2.execute(
            "SELECT 1 FROM convergence_events WHERE mint=? AND ts>=? LIMIT 1", (token, cutoff)
        ).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        c2.close()
    return 1.0 if row else 0.5


def seed_new_positions(conn) -> int:
    cur = conn.execute(
        "SELECT pair_address, token_symbol, token_address, first_alerted, mcap_at_alert "
        "FROM alerted_tokens WHERE token_address IS NOT NULL AND token_address != '' "
        "AND token_address NOT IN (SELECT token_address FROM tp_state)"
    )
    now = datetime.now(timezone.utc)
    n = 0
    for pair, symbol, token, first_alerted, mcap in cur.fetchall():
        try:
            entry = float(mcap or 0.0)
        except (TypeError, ValueError):
            entry = 0.0
        alert_dt = parse_iso(first_alerted, now)
        limit = entry * (1.0 - PULLBACK_PCT) if entry > 0 else 0.0
        deadline = (alert_dt + timedelta(minutes=ENTRY_WINDOW_MIN)).isoformat()
        size = convergence_gate(token) if entry > 0 else 0.5
        conn.execute(
            "INSERT OR IGNORE INTO tp_state (token_address, symbol, pair_address, entry_mcap, "
            "peak_mcap, last_peak_ts, started_ts, tp1, tp2, tp3, tp4, closed, close_reason, "
            "last_status_ts, exit_mcap, closed_ts, entry_status, limit_mcap, entry_deadline, size_mult) "
            "VALUES (?, ?, ?, 0, 0, ?, ?, 0,0,0,0, 0, '', ?, 0, '', 'pending', ?, ?, ?)",
            (token, symbol or "UNKNOWN", pair or "", alert_dt.isoformat(), alert_dt.isoformat(),
             alert_dt.isoformat(), limit, deadline, size),
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
        f"💰 Entry MCap: ${entry:,.0f}",
        f"📡 Current MCap: ${current:,.0f} ({mult:.2f}x)",
        f"🏔 Peak MCap: ${peak:,.0f} ({peak_mult:.2f}x)",
        f"📋 <b>STATUS:</b> {action}",
    ]
    if pair:
        lines.append(f"🔗 <a href='https://dexscreener.com/solana/{pair}'>View Chart</a>")
    return "\n".join(lines)


def evaluate_position(conn, r, now: datetime, mcap_map: dict) -> int:
    token, symbol, pair = r[0], (r[1] or "UNKNOWN"), (r[2] or "")
    entry = r[3] or 0.0
    peak = r[4] or 0.0
    last_peak_dt = parse_iso(r[5], now)
    started_dt = parse_iso(r[6], now)
    tp1, tp2, tp3, tp4 = r[7], r[8], r[9], r[10]
    last_status_dt = parse_iso(r[11], now)
    entry_status = r[12] or "active"
    limit_mcap = r[13] or 0.0
    deadline_dt = parse_iso(r[14], now)

    msgs = []
    closed, reason = 0, ""
    exit_mcap, closed_ts = 0.0, ""
    current = mcap_map.get(token)

    # ---------- PENDING LIMIT ENTRY LOGIC ----------
    if entry_status == "pending":
        if current is not None and current > 0 and limit_mcap > 0 and current <= limit_mcap:
            entry_status = "active"
            entry, peak = limit_mcap, limit_mcap
            started_dt, last_peak_dt, last_status_dt = now, now, now
            msgs.append(build_message("🎯", "PULLBACK ENTRY FILLED", symbol, entry, peak, current,
                                      f"Limit filled at -{PULLBACK_PCT * 100:.0f}% of alert price. Size x{r[15] or 1.0}.", pair))
        elif now > deadline_dt:
            entry_status = "cancelled"
            closed, reason = 1, "entry_expired"
            closed_ts = now.isoformat()
            msgs.append(f"⏭️ <b>ENTRY SKIPPED</b> — {symbol}: no pullback fill within {ENTRY_WINDOW_MIN}min. No trade taken.")
        else:
            conn.execute(
                "UPDATE tp_state SET entry_status=?, closed=?, close_reason=?, closed_ts=? WHERE token_address=?",
                (entry_status, closed, reason, closed_ts, token))
            conn.commit()
            return send_all(msgs)

    # ---------- ACTIVE POSITION RULES ----------
    if entry_status == "active":
        if entry <= 0:
            closed, reason, exit_mcap = 1, "bad_entry", 0.0
            closed_ts = now.isoformat()
        elif current is None or current <= 0:
            msgs.append(build_message("🕳️", "LIQUIDITY VANISHED", symbol, entry, peak, 0.0,
                                      "SELL REMAINDER IF POSSIBLE.", pair))
            closed, reason, exit_mcap = 1, "liquidity_gone", 0.0
            closed_ts = now.isoformat()
        else:
            mult = current / entry
            if current > peak:
                peak, last_peak_dt = current, now

            if tp1 == 0 and mult >= TP1_MULT:
                tp1 = 1
                msgs.append(build_message("1️⃣", "TP1 - CAPITAL RECOVERED (2x)", symbol, entry, peak, current,
                                          "SELL 30%. 70% moonbag riding.", pair))
            if tp1 == 1 and tp2 == 0 and mult >= TP2_MULT:
                tp2 = 1
                msgs.append(build_message("2️⃣", "TP2 - 3x LADDER", symbol, entry, peak, current, "SELL 20% of INITIAL.", pair))
            if tp1 == 1 and tp3 == 0 and mult >= TP3_MULT:
                tp3 = 1
                msgs.append(build_message("3️⃣", "TP3 - 5x LADDER", symbol, entry, peak, current, "SELL 20% of INITIAL.", pair))
            if tp1 == 1 and tp4 == 0 and mult >= TP4_MULT:
                tp4 = 1
                msgs.append(build_message("4️⃣", "TP4 - 10x LADDER", symbol, entry, peak, current, "SELL 10% of INITIAL.", pair))

            # ⏱️ PROVE-IT STOP
            if closed == 0 and tp1 == 0:
                age_min = (now - started_dt).total_seconds() / 60.0
                if age_min >= PROVE_IT_MINUTES and mult < PROVE_IT_MULT:
                    closed, reason, exit_mcap = 1, "prove_it", current
                    closed_ts = now.isoformat()
                    msgs.append(build_message("⏱️", "PROVE-IT STOP", symbol, entry, peak, current,
                                              f"No strength in {age_min:.0f}min - closing at market.", pair))

            # 🛑 Dynamic trailing stop
            if closed == 0 and tp1 == 1 and peak > 0:
                trail = TRAIL_WIDE if mult >= 3.0 else TRAIL_TIGHT
                drop = (peak - current) / peak
                if drop >= trail:
                    closed, reason, exit_mcap = 1, "trailing_stop", current
                    closed_ts = now.isoformat()
                    msgs.append(build_message("🛑", f"TRAILING STOP (-{int(trail * 100)}%)", symbol, entry, peak, current,
                                              "SELL REMAINDER.", pair))

            # ⛔ Circuit breaker
            if closed == 0 and tp1 == 0 and HARD_STOP_DROP > 0:
                loss = (entry - current) / entry
                if loss >= HARD_STOP_DROP:
                    closed, reason, exit_mcap = 1, "hard_stop", current
                    closed_ts = now.isoformat()
                    msgs.append(build_message("⛔", "CIRCUIT BREAKER STOP", symbol, entry, peak, current,
                                              f"SELL EVERYTHING -{loss * 100:.0f}%.", pair))

            # ⏳ Time decay / max age / hourly status
            if closed == 0:
                flat_h = (now - last_peak_dt).total_seconds() / 3600.0
                if flat_h >= TIME_DECAY_HOURS:
                    closed, reason, exit_mcap = 1, "time_decay", current
                    closed_ts = now.isoformat()
                    msgs.append(build_message("⏳", "TIME DECAY", symbol, entry, peak, current,
                                              f"SELL REMAINDER - flat {flat_h:.1f}h.", pair))
            age_h = (now - started_dt).total_seconds() / 3600.0
            if closed == 0 and age_h >= MAX_AGE_HOURS:
                closed, reason, exit_mcap = 1, "max_age", current
                closed_ts = now.isoformat()
                msgs.append(build_message("🏁", "MAX AGE (24h)", symbol, entry, peak, current, "Tracking stopped.", pair))
            if closed == 0:
                status_age_h = (now - last_status_dt).total_seconds() / 3600.0
                if status_age_h >= 1.0:
                    msgs.append(build_message("📊", "HOURLY STATUS", symbol, entry, peak, current,
                                              f"Holding {mult:.2f}x | Peak {(peak / entry):.2f}x", pair))
                    last_status_dt = now

    conn.execute(
        "UPDATE tp_state SET entry_mcap=?, peak_mcap=?, last_peak_ts=?, started_ts=?, tp1=?, tp2=?, tp3=?, tp4=?, "
        "closed=?, close_reason=?, last_status_ts=?, exit_mcap=?, closed_ts=?, entry_status=?, limit_mcap=? "
        "WHERE token_address=?",
        (entry, peak, last_peak_dt.isoformat(), started_dt.isoformat(), tp1, tp2, tp3, tp4,
         closed, reason, last_status_dt.isoformat(), exit_mcap, closed_ts, entry_status, limit_mcap, token),
    )
    conn.commit()
    return send_all(msgs)


def main() -> None:
    log("💼 Cloud Take-Profit Tracker v5 (Phase 7) starting...")
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
        log(f"🌱 Seeded {seeded} pending limit entr(ies).")

    rows = conn.execute(
        "SELECT token_address, symbol, pair_address, entry_mcap, peak_mcap, last_peak_ts, started_ts, "
        "tp1, tp2, tp3, tp4, last_status_ts, entry_status, limit_mcap, entry_deadline, size_mult "
        "FROM tp_state WHERE closed = 0"
    ).fetchall()
    if not rows:
        log("🔍 No open positions. Sleeping.")
        conn.close()
        return

    log(f"📡 Watching {len(rows)} position(s) (pending + active)...")
    ok, mcap_map = fetch_mcaps([r[0] for r in rows])
    if not ok:
        log("⚠️ DexScreener fetch failed - skipping cycle.")
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
