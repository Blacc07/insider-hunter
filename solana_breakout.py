"""
🟣 Solana Momentum & Breakout Tracker (solana_breakout.py)
Strategy: Catches organic volume breakouts and Raydium graduations.
Filters out micro-rugs by enforcing strict Liquidity and Volume minimums.
Uses DexScreener Public API (Free, no key required) + SQLite for deduplication.
"""

import os
import sys
import time
import sqlite3
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# ⚙️ CONFIGURATION (Tune these to your risk tolerance)
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PATH = os.environ.get("DB_PATH", "solana_breakout.db")

# 🎯 CALIBRATION MODE (Looser filters to map the current market)
MAX_PAIR_AGE_HOURS = 24      # Look back a full day
MIN_LIQUIDITY_USD = 10000    # $10k LP (Still filters out micro-rugs)
MIN_5M_VOLUME_USD = 10000    # $10k volume (Catches earlier momentum)
MIN_5M_PRICE_CHANGE = 5.0    # +5% pump (Catches steady grinds, not just vertical spikes)
MAX_MCAP_USD = 5000000       # Up to $5M mcap

# ⛔ HARD CAPS 
MAX_PAIRS_PER_RUN = 200      
MAX_ALERTS_PER_RUN = 5       # Allow up to 5 alerts per run so we can study them

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/search?q=SOL"
REQUEST_DELAY_SEC = 2.0      # Polite delay for free public API

def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)

def short(addr: str) -> str:
    addr = addr or ""
    return f"{addr[:4]}...{addr[-4:]}" if len(addr) >= 8 else addr

# ---------------------------------------------------------------------------
# 🗄️ SQLITE (Deduplication Memory)
# ---------------------------------------------------------------------------
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerted_tokens (
            pair_address TEXT PRIMARY KEY,
            token_symbol TEXT,
            first_alerted TEXT,
            mcap_at_alert REAL
        )
        """
    )
    conn.commit()
    return conn

def is_already_alerted(conn: sqlite3.Connection, pair_address: str) -> bool:
    cur = conn.execute("SELECT 1 FROM alerted_tokens WHERE pair_address = ?", (pair_address,))
    return cur.fetchone() is not None

def record_alert(conn, pair_address, symbol, mcap) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO alerted_tokens (pair_address, token_symbol, first_alerted, mcap_at_alert) VALUES (?, ?, ?, ?)",
        (pair_address, symbol, datetime.now(timezone.utc).isoformat(), mcap)
    )
    conn.commit()

# ---------------------------------------------------------------------------
# 📣 TELEGRAM
# ---------------------------------------------------------------------------
def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        log(f"⚠️ Telegram notify failed (non-fatal): {exc}")

# ---------------------------------------------------------------------------
# 🌐 DEXSCREENER API (Null-safe parsing)
# ---------------------------------------------------------------------------
def fetch_solana_pairs() -> list:
    """Fetches latest Solana pairs from DexScreener."""
    try:
        # DexScreener search endpoint returns pairs matching the query
        resp = requests.get(DEXSCREENER_URL, timeout=15)
        if resp.status_code == 429:
            log("⚠️ DexScreener 429 Rate Limit. Sleeping 10s.")
            time.sleep(10)
            return []
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs") if isinstance(data, dict) else []
        return pairs if isinstance(pairs, list) else []
    except Exception as e:
        log(f"⚠️ DexScreener fetch failed: {e}")
        return []

# ---------------------------------------------------------------------------
# 🧠 THE ALPHA ENGINE (Filtering the noise)
# ---------------------------------------------------------------------------
def analyze_pair(pair: dict, conn: sqlite3.Connection) -> None:
    # 1. Safety Checks: Ensure it's Solana and has valid data
    chain = pair.get("chainId", "")
    if chain != "solana":
        return
        
    pair_addr = pair.get("pairAddress", "")
    if not pair_addr or is_already_alerted(conn, pair_addr):
        return

    # 2. Extract Metrics (Defensive .get() with safe defaults)
    liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
    volume_5m = pair.get("volume", {}).get("m5", 0) or 0
    price_change_5m = pair.get("priceChange", {}).get("m5", 0) or 0
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    created_at = pair.get("pairCreatedAt") or 0
    
    # DexScreener returns creation time in milliseconds
    age_hours = (time.time() * 1000 - created_at) / (1000 * 60 * 60) if created_at > 0 else 999

    symbol = pair.get("baseToken", {}).get("symbol", "UNKNOWN")
    name = pair.get("baseToken", {}).get("name", "Unknown Token")
    token_addr = pair.get("baseToken", {}).get("address", "")
    dex_id = pair.get("dexId", "unknown")

    # 3. Apply The Alpha Filters
    if liquidity < MIN_LIQUIDITY_USD:
        return
    if volume_5m < MIN_5M_VOLUME_USD:
        return
    if price_change_5m < MIN_5M_PRICE_CHANGE:
        return
    if age_hours > MAX_PAIR_AGE_HOURS:
        return
    if mcap > MAX_MCAP_USD:
        return

    # 🚨 WE HAVE A BREAKOUT! 🚨
    record_alert(conn, pair_addr, symbol, mcap)
    
    alert_msg = (
        f"🔥 <b>SOLANA MOMENTUM BREAKOUT</b> 🔥\n"
        f"🪙 <b>{symbol}</b> ({name})\n"
        f"🏦 DEX: {dex_id.capitalize()}\n\n"
        f"💰 <b>MCap:</b> ${mcap:,.0f}\n"
        f"💧 <b>Liquidity:</b> ${liquidity:,.0f}\n"
        f"📈 <b>5m Vol:</b> ${volume_5m:,.0f}\n"
        f"🚀 <b>5m Change:</b> +{price_change_5m:.1f}%\n\n"
        f"🔗 <a href='https://dexscreener.com/solana/{pair_addr}'>DexScreener</a> | "
        f"<a href='https://birdeye.so/token/{token_addr}?chain=solana'>Birdeye</a>\n"
        f"📋 <code>{token_addr}</code>"
    )
    
    send_telegram(alert_msg)
    log(f"🔥 ALERT SENT: {symbol} | MCap: ${mcap:,.0f} | Vol: ${volume_5m:,.0f}")

# ---------------------------------------------------------------------------
# 🚀 MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    log("🟣 Solana Breakout Tracker starting scan...")
    conn = init_db()
    
    pairs = fetch_solana_pairs()
    if not pairs:
        log("⚠️ No pairs returned from DexScreener. Sleeping.")
        conn.close()
        return
        
    log(f"🔎 Analyzing {len(pairs)} Solana pairs against Alpha Filters...")
    
    alerts_sent = 0
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        # Check if we already hit our max alerts per run to prevent spam
        if alerts_sent >= 3: 
            break
            
        before_count = conn.execute("SELECT COUNT(*) FROM alerted_tokens").fetchone()[0]
        analyze_pair(pair, conn)
        after_count = conn.execute("SELECT COUNT(*) FROM alerted_tokens").fetchone()[0]
        
        if after_count > before_count:
            alerts_sent += 1
            
        time.sleep(0.1) # Micro-sleep to respect CPU

    conn.close()
    log(f"✅ Scan complete. {alerts_sent} new breakout(s) alerted.")

if __name__ == "__main__":
    main()
