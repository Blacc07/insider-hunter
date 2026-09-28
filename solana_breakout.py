"""
🟣 Solana Momentum & Breakout Tracker (solana_breakout.py)
v5 UPGRADE (Phase 7 filter stack):
  1. Liquidity-depth filter: liquidity/mcap >= 0.12 (thin books gap through stops)
  2. Holder-concentration filter: top-10 non-AMM holders <= 20% of supply (Helius)
  3. Pullback entries are now handled by tp_tracker v5 (positions seed as PENDING)
Filters fail OPEN on Helius errors (logged), never silently kill the pipeline.
"""

import os
import sqlite3
import time
import requests
from datetime import datetime, timezone

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
DB_PATH = os.environ.get("DB_PATH", "solana_breakout.db")

HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

# 🎯 v5 FILTERS
MAX_PAIR_AGE_HOURS = 12
MIN_LIQUIDITY_USD = 25000
MIN_5M_VOLUME_USD = 15000
MIN_5M_PRICE_CHANGE = 8.0
MIN_1H_PRICE_CHANGE = 15.0
MAX_MCAP_USD = 5000000
MIN_LIQ_MCAP_RATIO = 0.12      # NEW: depth filter
MAX_TOP10_CONC = 0.20          # NEW: holder concentration cap
MAX_PAIRS_PER_RUN = 30
MAX_ALERTS_PER_RUN = 5
REQUEST_DELAY_SEC = 0.5

BURN_ADDR = "1nc1nerator11111111111111111111111111111111"
AMM_OWNERS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp",  # Raydium AMM v4
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZh6SD2aMbLkjXe",  # Meteora DLMM
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpool
}


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def helius_rpc(method: str, params: list):
    if not HELIUS_API_KEY:
        return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        resp = requests.post(HELIUS_RPC, json=payload, timeout=20)
        if resp.status_code == 429:
            log("⚠️ Helius 429 - failing filter OPEN this cycle.")
            time.sleep(3)
            return None
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log(f"⚠️ Helius error on {method}: {exc}")
        return None
    if not isinstance(data, dict) or "error" in data:
        return None
    time.sleep(REQUEST_DELAY_SEC)
    return data.get("result")


def holder_concentration(mint: str):
    """Top-10 non-AMM, non-burn holder share of supply. None = unverifiable."""
    largest = helius_rpc("getTokenLargestAccounts", [mint])
    if not isinstance(largest, dict):
        return None
    entries = (largest.get("value") or [])[:10]
    if not entries:
        return None
    addrs = [e.get("address") for e in entries if isinstance(e, dict) and e.get("address")]
    addrs = [a for a in addrs if a != BURN_ADDR]
    if not addrs:
        return None
    infos = helius_rpc("getMultipleAccounts", [addrs, {"encoding": "jsonParsed"}])
    amm_owned = set()
    if isinstance(infos, dict):
        vals = infos.get("value") or []
        for i, v in enumerate(vals):
            if not isinstance(v, dict):
                continue
            parsed = ((v.get("data") or {}).get("parsed") or {})
            owner = ((parsed.get("info") or {}).get("owner")) or ""
            if owner in AMM_OWNERS:
                amm_owned.add(addrs[i])
    kept_raw = 0
    for e in entries:
        addr = e.get("address") or ""
        if addr == BURN_ADDR or addr in amm_owned:
            continue
        try:
            kept_raw += int(e.get("amount") or 0)
        except (TypeError, ValueError):
            continue
    supply = helius_rpc("getTokenSupply", [mint])
    try:
        supply_raw = int(((supply or {}).get("value") or {}).get("amount") or 0)
    except (TypeError, ValueError):
        supply_raw = 0
    if supply_raw <= 0:
        return None
    return kept_raw / supply_raw


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
    price_change_1h = (pair.get("priceChange") or {}).get("h1") or 0
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    created_at = pair.get("pairCreatedAt") or 0

    try:
        liquidity, volume_5m = float(liquidity), float(volume_5m)
        price_change_5m, price_change_1h, mcap = float(price_change_5m), float(price_change_1h), float(mcap)
    except (TypeError, ValueError):
        return False

    age_hours = ((time.time() * 1000 - created_at) / (1000 * 60 * 60)) if created_at > 0 else 9999
    symbol = (pair.get("baseToken") or {}).get("symbol", "UNKNOWN")
    token_addr = (pair.get("baseToken") or {}).get("address", "")

    if liquidity < MIN_LIQUIDITY_USD or volume_5m < MIN_5M_VOLUME_USD:
        return False
    if price_change_5m < MIN_5M_PRICE_CHANGE or price_change_1h < MIN_1H_PRICE_CHANGE:
        return False
    if age_hours > MAX_PAIR_AGE_HOURS or mcap > MAX_MCAP_USD:
        return False

    # 🛡️ v5 Filter 1: liquidity depth
    if mcap > 0:
        ratio = liquidity / mcap
        if ratio < MIN_LIQ_MCAP_RATIO:
            log(f"🛡️ Filtered {symbol}: liq/mcap {ratio:.2f} < {MIN_LIQ_MCAP_RATIO}")
            return False

    # 🛡️ v5 Filter 2: holder concentration (fail-open on API errors)
    conc = holder_concentration(token_addr)
    if conc is not None and conc > MAX_TOP10_CONC:
        log(f"🛡️ Filtered {symbol}: top-10 concentration {conc * 100:.0f}% > {MAX_TOP10_CONC * 100:.0f}%")
        return False

    record_alert(conn, pair_addr, symbol, token_addr, mcap)
    gate_txt = ""
    alert_msg = (
        f"🔥 <b>SOLANA MOMENTUM BREAKOUT</b> 🔥\n"
        f"🪙 <b>{symbol}</b>\n\n"
        f"💰 <b>MCap:</b> ${mcap:,.0f}\n💧 <b>Liquidity:</b> ${liquidity:,.0f}\n"
        f"📈 <b>5m:</b> +{price_change_5m:.1f}% | <b>1h:</b> +{price_change_1h:.1f}%\n"
        f"🎯 <b>Entry mode:</b> limit at -10% (15min validity){gate_txt}\n\n"
        f"🔗 <a href='https://dexscreener.com/solana/{pair_addr}'>DexScreener</a>\n"
        f"📋 <code>{token_addr}</code>"
    )
    send_telegram(alert_msg)
    log(f"🔥 ALERT SENT: {symbol} | MCap: ${mcap:,.0f} | conc: {conc if conc is not None else 'n/a'}")
    return True


def main() -> None:
    started = time.time()
    log("🟣 Solana Breakout Tracker (v5 Filter Stack) starting scan...")
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
