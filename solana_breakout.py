"""
🟣 Solana Momentum & Breakout Tracker (solana_breakout.py)
v3.1 FIX: DexScreener's /latest/dex/tokens endpoint returns an OBJECT
({"pairs": [...]}), not a bare list. v3 discarded it silently, causing
"No fresh pairs returned" on every run. Now parses both shapes and
logs every pipeline stage so nothing can fail silently again.
"""

import os
import sqlite3
import time
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# ⚙️ CONFIGURATION (Calibration Mode active)
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PATH = os.environ.get("DB_PATH", "solana_breakout.db")

MAX_PAIR_AGE_HOURS = 24      # only young tokens
MIN_LIQUIDITY_USD = 10000    # rug filter
MIN_5M_VOLUME_USD = 10000    # momentum filter
MIN_5M_PRICE_CHANGE = 5.0    # breakout filter
MAX_MCAP_USD = 5000000       # skip already-huge caps

MAX_PAIRS_PER_RUN = 30       # DexScreener batch limit + hard cap
MAX_ALERTS_PER_RUN = 5       # Telegram spam guard

PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


# ---------------------------------------------------------------------------
# 🗄️ SQLITE (dedup memory)
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
        "INSERT OR IGNORE INTO alerted_tokens (pair_address, token_symbol, first_alerted, mcap_at_alert) "
        "VALUES (?, ?, ?, ?)",
        (pair_address, symbol, datetime.now(timezone.utc).isoformat(), mcap),
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
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        log(f"⚠️ Telegram notify failed (non-fatal): {exc}")


# ---------------------------------------------------------------------------
# 🌐 DEXSCREENER (v3.1: profiles firehose -> pair data, fully logged)
# ---------------------------------------------------------------------------
def fetch_latest_solana_pairs() -> list:
    # Step 1: latest token profiles across all chains (returns a JSON array)
    try:
        resp = requests.get(PROFILES_URL, timeout=15)
        if resp.status_code == 429:
            log("⚠️ DexScreener 429 on profiles. Sleeping 10s, skipping run.")
            time.sleep(10)
            return []
        resp.raise_for_status()
        profiles = resp.json()
    except requests.exceptions.RequestException as exc:
        log(f"⚠️ Profiles fetch failed: {exc}")
        return []
    except ValueError as exc:
        log(f"⚠️ Profiles bad JSON: {exc}")
        return []

    if isinstance(profiles, dict):
        profiles = profiles.get("profiles") or profiles.get("tokens") or []
    if not isinstance(profiles, list):
        log(f"⚠️ Unexpected profiles payload type: {type(profiles).__name__}")
        return []

    sol_addresses = []
    for p in profiles:
        if isinstance(p, dict) and p.get("chainId") == "solana":
            addr = p.get("tokenAddress") or ""
            if addr:
                sol_addresses.append(addr)

    log(f"📡 Profiles fetched: {len(profiles)} | Solana profiles: {len(sol_addresses)}")

    if not sol_addresses:
        log("⚠️ No new Solana profiles in the latest feed (market quiet right now).")
        return []

    # Step 2: live pair data for those tokens (comma-separated batch, max 30)
    batch = sol_addresses[:MAX_PAIRS_PER_RUN]
    try:
        resp2 = requests.get(f"{TOKENS_URL}{','.join(batch)}", timeout=15)
        if resp2.status_code == 429:
            log("⚠️ DexScreener 429 on token batch. Sleeping 10s, skipping run.")
            time.sleep(10)
            return []
        resp2.raise_for_status()
        data = resp2.json()
    except requests.exceptions.RequestException as exc:
        log(f"⚠️ Pair data fetch failed: {exc}")
        return []
    except ValueError as exc:
        log(f"⚠️ Pair data bad JSON: {exc}")
        return []

    # v3.1 FIX: endpoint returns {"pairs": [...]} — tolerate bare list too
    if isinstance(data, dict):
        pairs = data.get("pairs") or []
    elif isinstance(data, list):
        pairs = data
    else:
        log(f"⚠️ Unexpected token-batch payload type: {type(data).__name__}")
        return []

    if not isinstance(pairs, list):
        log(f"⚠️ Unexpected pairs payload type: {type(pairs).__name__}")
        return []

    log(f"📦 Pairs loaded for batch of {len(batch)} token(s): {len(pairs)}")
    return pairs


def vol_5m(pair) -> float:
    if not isinstance(pair, dict):
        return 0.0
    volume = pair.get("volume") or {}
    try:
        return float(volume.get("m5") or 0.0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# 🧠 ALPHA ENGINE (returns True only when an alert fired)
# ---------------------------------------------------------------------------
def analyze_pair(pair: dict, conn: sqlite3.Connection) -> bool:
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
        liquidity = float(liquidity)
        price_change_5m = float(price_change_5m)
        mcap = float(mcap)
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
    name = (pair.get("baseToken") or {}).get("name", "Unknown Token")
    token_addr = (pair.get("baseToken") or {}).get("address", "")
    dex_id = pair.get("dexId", "unknown")

    record_alert(conn, pair_addr, symbol, mcap)

    alert_msg = (
        f"🔥 <b>SOLANA MOMENTUM BREAKOUT</b> 🔥\n"
        f"🪙 <b>{symbol}</b> ({name})\n"
        f"🏦 DEX: {str(dex_id).capitalize()}\n\n"
        f"💰 <b>MCap:</b> ${mcap:,.0f}\n"
        f"💧 <b>Liquidity:</b> ${liquidity:,.0f}\n"
        f"📈 <b>5m Vol:</b> ${volume_5m:,.0f}\n"
        f"🚀 <b>5m Change:</b> +{price_change_5m:.1f}%\n\n"
        f"🔗 <a href='https://dexscreener.com/solana/{pair_addr}'>DexScreener</a> | "
        f"<a href='https://birdeye.so/token/{token_addr}?chain=solana'>Birdeye</a>\n"
        f"📋 <code>{token_addr}</code>"
    )
    send_telegram(alert_msg)
    log(f"🔥 ALERT SENT: {symbol} | MCap: ${mcap:,.0f} | 5m Vol: ${volume_5m:,.0f}")
    return True


# ---------------------------------------------------------------------------
# 🚀 MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    started = time.time()
    log("🟣 Solana Breakout Tracker (v3.1 Firehose) starting scan...")
    conn = init_db()

    pairs = fetch_latest_solana_pairs()
    if not pairs:
        log("⚠️ No fresh pairs returned. Sleeping until next run.")
        conn.close()
        return

    pairs.sort(key=vol_5m, reverse=True)
    log(f"🔎 Analyzing {len(pairs)} fresh Solana pairs (hottest first)...")

    alerts = 0
    for pair in pairs:
        if alerts >= MAX_ALERTS_PER_RUN:
            break
        if not isinstance(pair, dict):
            continue
        if analyze_pair(pair, conn):
            alerts += 1

    conn.close()
    log(f"✅ Scan complete. Analyzed {len(pairs)} pair(s), {alerts} new breakout(s) "
        f"alerted in {time.time() - started:.1f}s.")


if __name__ == "__main__":
    main()
