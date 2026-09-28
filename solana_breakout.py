"""
🟣 Solana Intraday Discovery (solana_breakout.py) — v7 PRO FILTERS
Professional intraday memecoin filter stack:
  depth, volume authenticity, exhaustion cap, HTF alignment, age/mcap bands,
  Helius mint/freeze + holder-concentration safety (fail-open on API errors).
Goal: delete rugs and top-of-spike entries BEFORE they become trades,
so the 2x/3x ladder's asymmetry can produce PF > 2.0.
"""

import base64
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

# 🎯 v7 PROFESSIONAL INTRADAY FILTERS
MIN_LIQUIDITY_USD = 30000
MIN_LIQ_MCAP_RATIO = 0.10
MIN_5M_VOLUME_USD = 20000
MAX_VOL_LIQ_RATIO = 4.0
MIN_5M_PRICE_CHANGE = 8.0
MAX_5M_PRICE_CHANGE = 35.0     # exhaustion cap: never buy the top of the spike
MIN_1H_PRICE_CHANGE = 10.0
MIN_PAIR_AGE_HOURS = 1.0
MAX_PAIR_AGE_HOURS = 24.0
MIN_MCAP_USD = 100000
MAX_MCAP_USD = 5000000
MAX_TOP10_CONC = 0.25
MAX_PAIRS_PER_RUN = 30
MAX_ALERTS_PER_RUN = 5
REQUEST_DELAY_SEC = 0.5

BURN_ADDR = "1nc1nerator11111111111111111111111111111111"
AMM_OWNERS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZh6SD2aMbLkjXe",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
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
            log("⚠️ Helius 429 - failing safety filters OPEN this cycle.")
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


def check_mint_authorities(mint: str):
    result = helius_rpc("getAccountInfo", [mint, {"encoding": "base64"}])
    try:
        data = (result or {}).get("data") or []
        raw = base64.b64decode(data[0])
        if len(raw) < 82:
            return None, None
        mint_revoked = int.from_bytes(raw[0:4], "little") == 0
        freeze_revoked = int.from_bytes(raw[46:50], "little") == 0
        return mint_revoked, freeze_revoked
    except Exception:
        return None, None


def holder_concentration(mint: str):
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
    pc5 = (pair.get("priceChange") or {}).get("m5") or 0
    pc1h = (pair.get("priceChange") or {}).get("h1") or 0
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    created_at = pair.get("pairCreatedAt") or 0

    try:
        liquidity, volume_5m = float(liquidity), float(volume_5m)
        pc5, pc1h, mcap = float(pc5), float(pc1h), float(mcap)
    except (TypeError, ValueError):
        return False

    age_hours = ((time.time() * 1000 - created_at) / (1000 * 60 * 60)) if created_at > 0 else 9999
    symbol = (pair.get("baseToken") or {}).get("symbol", "UNKNOWN")
    token_addr = (pair.get("baseToken") or {}).get("address", "")

    # --- Cheap DexScreener filters first ---
    if liquidity < MIN_LIQUIDITY_USD:
        return False
    if mcap <= 0 or mcap < MIN_MCAP_USD or mcap > MAX_MCAP_USD:
        return False
    if (liquidity / mcap) < MIN_LIQ_MCAP_RATIO:
        log(f"🛡️ {symbol}: depth ratio {liquidity / mcap:.2f} too thin")
        return False
    if volume_5m < MIN_5M_VOLUME_USD:
        return False
    if (volume_5m / liquidity) > MAX_VOL_LIQ_RATIO:
        log(f"🛡️ {symbol}: vol/liq {volume_5m / liquidity:.1f} looks wash-traded")
        return False
    if pc5 < MIN_5M_PRICE_CHANGE:
        return False
    if pc5 > MAX_5M_PRICE_CHANGE:
        log(f"🛡️ {symbol}: +{pc5:.0f}% in 5m = exhausted candle, refusing top-entry")
        return False
    if pc1h < MIN_1H_PRICE_CHANGE:
        return False
    if age_hours < MIN_PAIR_AGE_HOURS or age_hours > MAX_PAIR_AGE_HOURS:
        return False

    # --- Helius safety checks last (credits), fail-open on errors ---
    mint_rev, freeze_rev = check_mint_authorities(token_addr)
    if mint_rev is False or freeze_rev is False:
        log(f"🛡️ {symbol}: mint/freeze authority NOT revoked - honeypot risk")
        return False
    conc = holder_concentration(token_addr)
    if conc is not None and conc > MAX_TOP10_CONC:
        log(f"🛡️ {symbol}: top-10 conc {conc * 100:.0f}% > {MAX_TOP10_CONC * 100:.0f}%")
        return False

    record_alert(conn, pair_addr, symbol, token_addr, mcap)
    alert_msg = (
        f"⚡ <b>SOLANA INTRADAY SETUP (v7)</b> ⚡\n"
        f"🪙 <b>{symbol}</b>\n\n"
        f"💰 MCap: ${mcap:,.0f} | 💧 Liq: ${liquidity:,.0f} (depth {liquidity / mcap:.2f})\n"
        f"📈 5m: +{pc5:.1f}% | 1h: +{pc1h:.1f}% | age {age_hours:.1f}h\n"
        f"🔐 Mint/Freeze revoked: {'✅' if mint_rev else '?'} / {'✅' if freeze_rev else '?'}"
        + (f" | top10 {conc * 100:.0f}%" if conc is not None else "") + "\n\n"
        f"🎯 <b>PLAN:</b> TP1 2x (30%) | TP2 3x (20%) | TP3 5x (20%) | TP4 10x (10%)\n"
        f"🛡️ Stop -20% | Stall-out 60min <1.2x | Trail remainder\n"
        f"🔗 <a href='https://dexscreener.com/solana/{pair_addr}'>DexScreener</a>\n"
        f"📋 <code>{token_addr}</code>"
    )
    send_telegram(alert_msg)
    log(f"⚡ v7 ALERT: {symbol} | MCap ${mcap:,.0f} | 5m +{pc5:.1f}%")
    return True


def main() -> None:
    started = time.time()
    log("🟣 Solana Discovery v7 (Pro Intraday Filters) starting scan...")
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
