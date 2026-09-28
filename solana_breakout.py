"""
🟣 Solana Momentum Discovery (solana_breakout.py) — v6 SCALPER
Reverted to high-frequency discovery (v3-era thresholds, mild rug guards).
REMOVED: liq/mcap ratio, holder-concentration (Helius), 1h-change gate,
and pullback limit entries (they cancel in pumps and catch knives in dumps).
Entries seed ACTIVE at alert mcap; the scalper exit engine lives in tp_tracker.py.
"""

import os
import sqlite3
import time
import requests
from datetime import datetime, timezone

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PATH = os.environ.get("DB_PATH", "solana_breakout.db")

PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

# 🎯 SCALPER DISCOVERY FILTERS (high frequency, light rug guards)
MAX_PAIR_AGE_HOURS = 24
MIN_LIQUIDITY_USD = 12000
MIN_5M_VOLUME_USD = 12000
MIN_5M_PRICE_CHANGE = 6.0
MAX_MCAP_USD = 5000000
MAX_PAIRS_PER_RUN = 30
MAX_ALERTS_PER_RUN = 5


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


def is_already_alerted(conn, pair_address: str) -> bool:
    cur = conn.execute("SELECT 1 FROM alerted_tokens WHERE pair_address = ?", (pair_address,))
    return cur.fetchone() is not None


def record_alert(conn, pair_address, symbol, token_address, mcap) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO alerted_tokens (pair_address, token_symbol, token_address, "
        "first_alerted, mcap_at_alert, updates_sent) VALUES (?, ?, ?, ?, ?, '')",
        (pair_address, symbol, token_address, datetime.now(timezone.utc).isoformat(), mcap),
    )
    conn.commit()


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


def fetch_latest_solana_pairs() -> list:
    try:
        resp = requests.get(PROFILES_URL, timeout=15)
        if resp.status_code == 429:
            time.sleep(10)
            return []
        resp.raise_for_status()
        profiles = resp.json()
    except Exception as e:
        log(f"⚠️ Profiles fetch failed: {e}")
        return []
    if isinstance(profiles, dict):
        profiles = profiles.get("profiles") or []
    if not isinstance(profiles, list):
        return []
    sol_addresses = [p.get("tokenAddress") for p in profiles
                     if isinstance(p, dict) and p.get("chainId") == "solana" and p.get("tokenAddress")]
    log(f"📡 Profiles fetched: {len(profiles)} | Solana profiles: {len(sol_addresses)}")
    if not sol_addresses:
        return []
    batch = sol_addresses[:MAX_PAIRS_PER_RUN]
    try:
        resp2 = requests.get(f"{TOKENS_URL}{','.join(batch)}", timeout=15)
        if resp2.status_code == 429:
            time.sleep(10)
            return []
        resp2.raise_for_status()
        data = resp2.json()
    except Exception as e:
        log(f"⚠️ Pair data fetch failed: {e}")
        return []
    pairs = data.get("pairs") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    log(f"📦 Pairs loaded for batch of {len(batch)} token(s): {len(pairs)}")
    return pairs if isinstance(pairs, list) else []


def vol_5m(pair) -> float:
    try:
        return float((pair.get("volume") or {}).get("m5") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def analyze_pair(pair: dict, conn) -> bool:
    if pair.get("chainId", "") != "solana":
        return False
    pair_addr = pair.get("pairAddress", "")
    if not pair_addr or is_already_alerted(conn, pair_addr):
        return False

    liquidity = (pair.get("liquidity") or {}).get("usd") or 0
    volume_5m = vol_5m(pair)
    price_change_5m = (pair.get("priceChange") or {}).get("m5") or 0
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    created_at = pair.get("pairCreatedAt") or 0

    try:
        liquidity, volume_5m = float(liquidity), float(volume_5m)
        price_change_5m, mcap = float(price_change_5m), float(mcap)
    except (TypeError, ValueError):
        return False

    age_hours = ((time.time() * 1000 - created_at) / (1000 * 60 * 60)) if created_at > 0 else 9999

    if liquidity < MIN_LIQUIDITY_USD:
        return False
    if volume_5m < MIN_5M_VOLUME_USD:
        return False
    if price_change_5m < MIN_5M_PRICE_CHANGE:
        return False
    if age_hours > MAX_PAIR_AGE_HOURS:
        return False
    if mcap > MAX_MCAP_USD:
        return False

    symbol = (pair.get("baseToken") or {}).get("symbol", "UNKNOWN")
    token_addr = (pair.get("baseToken") or {}).get("address", "")
    dex_id = pair.get("dexId", "unknown")

    record_alert(conn, pair_addr, symbol, token_addr, mcap)
    alert_msg = (
        f"⚡ <b>SOLANA SCALP SETUP</b> ⚡\n"
        f"🪙 <b>{symbol}</b> | 🏦 {str(dex_id).capitalize()}\n\n"
        f"💰 MCap: ${mcap:,.0f} | 💧 Liq: ${liquidity:,.0f}\n"
        f"📈 5m Vol: ${volume_5m:,.0f} | 🚀 5m: +{price_change_5m:.1f}%\n\n"
        f"🎯 <b>PLAN:</b> TP1 1.35x sell 50% | TP2 1.75x sell 30% | "
        f"Stop -18% | Stall-out 60min\n"
        f"🔗 <a href='https://dexscreener.com/solana/{pair_addr}'>DexScreener</a>\n"
        f"📋 <code>{token_addr}</code>"
    )
    send_telegram(alert_msg)
    log(f"⚡ SCALP ALERT: {symbol} | MCap ${mcap:,.0f}")
    return True


def main() -> None:
    started = time.time()
    log("🟣 Solana Discovery v6 (Scalper) starting scan...")
    conn = init_db()
    pairs = fetch_latest_solana_pairs()
    if not pairs:
        log("⚠️ No fresh pairs returned. Sleeping.")
        conn.close()
        return
    pairs.sort(key=vol_5m, reverse=True)
    log(f"🔎 Analyzing {len(pairs)} fresh Solana pairs...")
    alerts = 0
    for pair in pairs:
        if alerts >= MAX_ALERTS_PER_RUN:
            break
        if isinstance(pair, dict) and analyze_pair(pair, conn):
            alerts += 1
    conn.close()
    log(f"✅ Scan complete. {alerts} alert(s) in {time.time() - started:.1f}s.")


if __name__ == "__main__":
    main()
