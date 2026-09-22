"""
📊 Solana Performance Tracker (solana_tracker.py)
Monitors tokens alerted in the last 8 hours.
Sends milestone updates to Telegram at 1h, 4h, and 8h marks.
"""

import os
import sqlite3
import time
import requests
from datetime import datetime, timezone, timedelta

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PATH = os.environ.get("DB_PATH", "solana_breakout.db")
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)

def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as exc:
        log(f"⚠️ Telegram failed: {exc}")

def ensure_columns(conn: sqlite3.Connection):
    """Self-healing migration: ensures required columns exist before querying."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(alerted_tokens)").fetchall()]
    if 'token_address' not in cols:
        conn.execute("ALTER TABLE alerted_tokens ADD COLUMN token_address TEXT DEFAULT ''")
    if 'updates_sent' not in cols:
        conn.execute("ALTER TABLE alerted_tokens ADD COLUMN updates_sent TEXT DEFAULT ''")
    conn.commit()

def get_active_tracks(conn: sqlite3.Connection) -> list:
    """Fetches tokens alerted within the last 8 hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()
    cur = conn.execute(
        "SELECT pair_address, token_symbol, token_address, first_alerted, mcap_at_alert, updates_sent "
        "FROM alerted_tokens WHERE first_alerted >= ? AND token_address != ''",
        (cutoff,)
    )
    return cur.fetchall()

def fetch_current_mcaps(addresses: list) -> dict:
    """Batches addresses and fetchs current MCap from DexScreener."""
    if not addresses: return {}
    batch = addresses[:30] # DexScreener limit
    try:
        resp = requests.get(f"{TOKENS_URL}{','.join(batch)}", timeout=15)
        if resp.status_code == 429: time.sleep(10); return {}
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        
        # Map token_address -> current_mcap (take the highest liquidity pair for that token)
        mcap_map = {}
        for p in pairs:
            if not isinstance(p, dict): continue
            token_addr = (p.get("baseToken") or {}).get("address", "")
            mcap = p.get("marketCap") or p.get("fdv") or 0
            if token_addr and mcap:
                try:
                    mcap = float(mcap)
                    if token_addr not in mcap_map or mcap > mcap_map[token_addr]:
                        mcap_map[token_addr] = mcap
                except: pass
        return mcap_map
    except Exception as e:
        log(f"⚠️ Tracker fetch failed: {e}")
        return {}

def main() -> None:
    log("📊 Solana Tracker starting 8h performance check...")
    if not os.path.exists(DB_PATH):
        log("⚠️ DB not found. Exiting.")
        return

    conn = sqlite3.connect(DB_PATH)
    ensure_columns(conn) # Prevents the 'no such column' crash
    
    tracks = get_active_tracks(conn)
    if not tracks:
        log("🔍 No active tracks in the last 8 hours. Sleeping.")
        conn.close()
        return

    log(f"🔎 Tracking {len(tracks)} active token(s)...")
    addresses = [row[2] for row in tracks] # token_address is index 2
    current_mcaps = fetch_current_mcaps(addresses)

    updates_fired = 0
    for row in tracks:
        pair_addr, symbol, token_addr, first_alerted_str, initial_mcap, updates_sent = row
        updates_sent = updates_sent or ""
        
        current_mcap = current_mcaps.get(token_addr)
        if not current_mcap or initial_mcap <= 0:
            continue

        roi_pct = ((current_mcap - initial_mcap) / initial_mcap) * 100
        
        # Calculate age in hours
        first_alerted_dt = datetime.fromisoformat(first_alerted_str)
        age_hours = (datetime.now(timezone.utc) - first_alerted_dt).total_seconds() / 3600
        
        # Determine which milestone to fire
        milestone = None
        if age_hours >= 8.0 and "8h" not in updates_sent:
            milestone = "8h"
            emoji = "🏁"
        elif age_hours >= 4.0 and "4h" not in updates_sent:
            milestone = "4h"
            emoji = "⏳"
        elif age_hours >= 1.0 and "1h" not in updates_sent:
            milestone = "1h"
            emoji = "🕒"
            
        if milestone:
            roi_emoji = "🟢" if roi_pct >= 0 else "🔴"
            msg = (
                f"{emoji} <b>{milestone.upper()} PERFORMANCE UPDATE</b> {emoji}\n"
                f"🪙 <b>{symbol}</b>\n\n"
                f"💰 <b>Entry MCap:</b> ${initial_mcap:,.0f}\n"
                f"💸 <b>Current MCap:</b> ${current_mcap:,.0f}\n"
                f"📊 <b>ROI:</b> {roi_emoji} <b>{roi_pct:+.1f}%</b>\n\n"
                f"🔗 <a href='https://dexscreener.com/solana/{pair_addr}'>View Chart</a>"
            )
            send_telegram(msg)
            
            # Update DB to prevent duplicate alerts
            new_updates = f"{updates_sent},{milestone}" if updates_sent else milestone
            conn.execute("UPDATE alerted_tokens SET updates_sent = ? WHERE pair_address = ?", (new_updates, pair_addr))
            conn.commit()
            updates_fired += 1
            log(f"📈 {symbol} {milestone} Update: {roi_pct:+.1f}%")

    conn.close()
    log(f"✅ Tracker complete. Fired {updates_fired} milestone update(s).")

if __name__ == "__main__":
    main()
