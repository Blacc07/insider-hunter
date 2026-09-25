"""
🟣 Solana Momentum & Breakout Tracker (solana_breakout.py)
v4 UPGRADE: Added 1-Hour Trend Filter and raised liquidity floor.
Based on 170-trade paper data, this filters out 5-minute flash-pump 
wash trades that were causing the 131 hard-stops (-40%).
"""

import os
import sqlite3
import time
import requests
from datetime import datetime, timezone

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PATH = os.environ.get("DB_PATH", "solana_breakout.db")

# 🎯 TUNED FILTERS (Based on 170-trade autopsy)
MAX_PAIR_AGE_HOURS = 12      # Tightened from 24h (fresher momentum)
MIN_LIQUIDITY_USD = 25000    # Raised from 10k (filters out micro-cap instant dumps)
MIN_5M_VOLUME_USD = 15000    # Raised from 10k
MIN_5M_PRICE_CHANGE = 8.0    # Slightly lowered to catch the start of the move
MIN_1H_PRICE_CHANGE = 15.0   # NEW: Must be up 15% on the hour (kills 5m flash-in-the-pan wash trades)
MAX_MCAP_USD = 5000000       
MAX_PAIRS_PER_RUN = 30
MAX_ALERTS_PER_RUN = 5

PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerted_tokens (
            pair_address TEXT PRIMARY KEY,
            token_symbol TEXT,
            token_address TEXT,
            first_alerted TEXT,
            mcap_at_alert REAL,
            updates_sent TEXT DEFAULT ''
        )
        """
    )
    cols = [row[1] for row in conn.execute("PRAGMA table_info(alerted_tokens)").fetchall()]
    if 'token_address' not in cols:
        conn.execute("ALTER TABLE alerted_tokens ADD COLUMN token_address TEXT DEFAULT ''")
    if 'updates_sent' not in cols:
        conn.execute("ALTER TABLE alerted_tokens ADD COLUMN updates_sent TEXT DEFAULT ''")
    conn.commit()
    return conn

def is_already_alerted(conn: sqlite3.Connection, pair_address: str) -> bool:
    cur = conn.execute("SELECT 1 FROM alerted_tokens WHERE pair_address = ?", (pair_address,))
    return cur.fetchone() is not None

def record_alert(conn, pair_address, symbol, token_address, mcap) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO alerted_tokens (pair_address, token_symbol, token_address, first_alerted, mcap_at_alert, updates_sent) "
        "VALUES (?, ?, ?, ?, ?, '')",
        (pair_address, symbol, token_address, datetime.now(timezone.utc).isoformat(), mcap),
    )
    conn.commit()

def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        log(f"⚠️ Telegram notify failed (non-fatal): {exc}")

def fetch_latest_solana_pairs() -> list:
    try:
        resp = requests.get(PROFILES_URL, timeout=15)
        if resp.status_code == 429: time.sleep(10); return []
        resp.raise_for_status()
        profiles = resp.json()
    except Exception as e: log(f"⚠️ Profiles fetch failed: {e}"); return []

    if isinstance(profiles, dict): profiles = profiles.get("profiles") or []
    if not isinstance(profiles, list): return []

    sol_addresses = [p.get("tokenAddress") for p in profiles if isinstance(p, dict) and p.get("chainId") == "solana" and p.get("tokenAddress")]
    log(f"📡 Profiles fetched: {len(profiles)} | Solana profiles: {len(sol_addresses)}")
    if not sol_addresses: return []

    batch = sol_addresses[:MAX_PAIRS_PER_RUN]
    try:
        resp2 = requests.get(f"{TOKENS_URL}{','.join(batch)}", timeout=15)
        if resp2.status_code == 429: time.sleep(10); return []
        resp2.raise_for_status()
        data = resp2.json()
    except Exception as e: log(f"⚠️ Pair data fetch failed: {e}"); return []

    pairs = data.get("pairs") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    log(f"📦 Pairs loaded for batch of {len(batch)} token(s): {len(pairs)}")
    return pairs if isinstance(pairs, list) else []

def vol_5m(pair) -> float:
    try: return float((pair.get("volume") or {}).get("m5") or 0.0)
    except: return 0.0

def analyze_pair(pair: dict, conn: sqlite3.Connection) -> bool:
    if pair.get("chainId", "") != "solana": return False
    pair_addr = pair.get("pairAddress", "")
    if not pair_addr or is_already_alerted(conn, pair_addr): return False

    liquidity = (pair.get("liquidity") or {}).get("usd") or 0
    volume_5m = vol_5m(pair)
    price_change_5m = (pair.get("priceChange") or {}).get("m5") or 0
    price_change_1h = (pair.get("priceChange") or {}).get("h1") or 0 # NEW: 1-Hour Trend
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    created_at = pair.get("pairCreatedAt") or 0

    try: 
        liquidity = float(liquidity)
        volume_5m = float(volume_5m)
        price_change_5m = float(price_change_5m)
        price_change_1h = float(price_change_1h)
        mcap = float(mcap)
    except: return False

    age_hours = ((time.time() * 1000 - created_at) / (1000 * 60 * 60)) if created_at > 0 else 9999

    # 🛡️ STRICT V4 FILTERS
    if liquidity < MIN_LIQUIDITY_USD: return False
    if volume_5m < MIN_5M_VOLUME_USD: return False
    if price_change_5m < MIN_5M_PRICE_CHANGE: return False
    if price_change_1h < MIN_1H_PRICE_CHANGE: return False # KILLS THE FAKEOUTS
    if age_hours > MAX_PAIR_AGE_HOURS: return False
    if mcap > MAX_MCAP_USD: return False

    symbol = (pair.get("baseToken") or {}).get("symbol", "UNKNOWN")
    token_addr = (pair.get("baseToken") or {}).get("address", "")
    dex_id = pair.get("dexId", "unknown")

    record_alert(conn, pair_addr, symbol, token_addr, mcap)

    alert_msg = (
        f"🔥 <b>SOLANA MOMENTUM BREAKOUT</b> 🔥\n"
        f"🪙 <b>{symbol}</b>\n🏦 DEX: {str(dex_id).capitalize()}\n\n"
        f"💰 <b>MCap:</b> ${mcap:,.0f}\n💧 <b>Liquidity:</b> ${liquidity:,.0f}\n"
        f"📈 <b>5m Vol:</b> ${volume_5m:,.0f}\n"
        f"🚀 <b>5m Change:</b> +{price_change_5m:.1f}%\n"
        f"📊 <b>1h Change:</b> +{price_change_1h:.1f}%\n\n"
        f"🔗 <a href='https://dexscreener.com/solana/{pair_addr}'>DexScreener</a> | "
        f"<a href='https://birdeye.so/token/{token_addr}?chain=solana'>Birdeye</a>\n"
        f"📋 <code>{token_addr}</code>"
    )
    send_telegram(alert_msg)
    log(f"🔥 ALERT SENT: {symbol} | MCap: ${mcap:,.0f} | 1h: +{price_change_1h:.1f}%")
    return True

def main() -> None:
    started = time.time()
    log("🟣 Solana Breakout Tracker (v4 Trend Filter) starting scan...")
    conn = init_db()
    pairs = fetch_latest_solana_pairs()
    if not pairs:
        log("⚠️ No fresh pairs returned. Sleeping.")
        conn.close(); return

    pairs.sort(key=vol_5m, reverse=True)
    log(f"🔎 Analyzing {len(pairs)} fresh Solana pairs...")
    alerts = 0
    for pair in pairs:
        if alerts >= MAX_ALERTS_PER_RUN: break
        if isinstance(pair, dict) and analyze_pair(pair, conn): alerts += 1

    conn.close()
    log(f"✅ Scan complete. {alerts} alert(s) in {time.time() - started:.1f}s.")

if __name__ == "__main__":
    main()
