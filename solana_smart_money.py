"""
🧬 Phase 6: Solana Smart Money Stalker (solana_smart_money.py)

Isolated module: NEVER writes to solana_breakout.db (opens it READ-ONLY
to harvest proven winners). Own database: solana_smart_money.db.

Pipeline (every 15 min via cron-job.org heartbeat #3):
  1. HARVEST   - read momentum DB for positions that hit TP2 (3x+),
                 pull pre-alert buyers from Helius, mint them into the
                 smart_wallets watchlist (wins counter per wallet).
  2. STAKEOUT  - poll watchlist wallets for new swaps; log every
                 SOL->token accumulation event into convergence_events.
  3. CONVERGE  - if >=2 distinct smart wallets accumulated the same mint
                 within 48h AND the mint passes safety filters
                 (liq >= $10k, mcap $50k-$3M, age >= 24h), fire a
                 🧬 SMART MONEY CONVERGENCE alert with mint-authority status.

Free-tier budget (Helius ~100k credits/day):
  stakeout: 50 sig calls + <=15 parses  ~= 200 credits/run
  harvest:  <=2 tokens x (3 sig pages + 10 parses) ~= 260 credits/run
  => ~96 runs/day * ~460 = ~45k credits/day. Safe.
"""

import base64
import os
import sqlite3
import time
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# ⚙️ CONFIG
# ---------------------------------------------------------------------------
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
PARSE_URL = f"https://api.helius.xyz/v0/transactions?api-key={HELIUS_API_KEY}"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DB_PATH = os.environ.get("DB_PATH", "solana_smart_money.db")
MOMENTUM_DB = os.environ.get("MOMENTUM_DB", "solana_breakout.db")
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

MAX_WATCH_WALLETS = 50
MAX_SIGS_PER_WALLET = 10
MAX_PARSES_PER_RUN = 15
MAX_HARVEST_PER_RUN = 2
MAX_HARVEST_PAGES = 3
MAX_HARVEST_PARSES = 10
CONVERGENCE_WINDOW_H = 48
MIN_CONVERGENCE = 2
REQUEST_DELAY_SEC = 0.6

MIN_LIQUIDITY_USD = 10000
MIN_MCAP_USD = 50000
MAX_MCAP_USD = 3000000
MIN_AGE_HOURS = 24

QUOTE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


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


# ---------------------------------------------------------------------------
# 🌐 HELIUS
# ---------------------------------------------------------------------------
def rpc(method: str, params: list, retries: int = 2):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    attempt = 0
    while attempt < retries:
        attempt += 1
        try:
            resp = requests.post(RPC_URL, json=payload, timeout=30)
        except requests.exceptions.RequestException as exc:
            log(f"⚠️ Network error on {method}: {exc}")
            if attempt < retries:
                time.sleep(3 * attempt)
            continue
        if resp.status_code == 429:
            log(f"⚠️ Helius 429 on {method}. Sleeping 5s.")
            time.sleep(5)
            continue
        if 400 <= resp.status_code < 500:
            log(f"❌ Helius {resp.status_code} on {method} (NOT retrying): {(resp.text or '')[:200]}")
            return None
        if resp.status_code >= 500:
            if attempt < retries:
                time.sleep(3 * attempt)
            continue
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, dict) or "error" in data:
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            log(f"⚠️ Helius RPC error on {method}: {err.get('message', 'unknown')}")
            return None
        time.sleep(REQUEST_DELAY_SEC)
        return data.get("result")
    return None


def parse_txs(signatures: list) -> list:
    if not signatures:
        return []
    try:
        resp = requests.post(PARSE_URL, json={"transactions": signatures}, timeout=30)
        if resp.status_code == 429:
            log("⚠️ Helius 429 on parse. Sleeping 5s.")
            time.sleep(5)
            resp = requests.post(PARSE_URL, json={"transactions": signatures}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        time.sleep(REQUEST_DELAY_SEC)
        return data if isinstance(data, list) else []
    except Exception as exc:
        log(f"⚠️ Parse failed (non-fatal): {exc}")
        return []


def get_sigs(address: str, limit: int) -> list:
    result = rpc("getSignaturesForAddress", [address, {"limit": limit}])
    return result if isinstance(result, list) else []


def extract_sol_buyers(tx: dict, mint: str) -> dict:
    """Wallets that received `mint` while spending SOL in this tx."""
    if not isinstance(tx, dict):
        return {}
    ts = tx.get("timestamp") or 0
    buyers = {}
    for tt in (tx.get("tokenTransfers") or []):
        if not isinstance(tt, dict):
            continue
        if (tt.get("mint") or "") != mint:
            continue
        wallet = tt.get("to") or ""
        if not wallet or wallet in QUOTE_MINTS:
            continue
        buyers.setdefault(wallet, {"wallet": wallet, "sol": 0.0, "ts": ts})
    for nt in (tx.get("nativeTransfers") or []):
        if not isinstance(nt, dict):
            continue
        sender = nt.get("from") or ""
        if sender in buyers:
            try:
                buyers[sender]["sol"] += float(nt.get("amount") or 0.0)
            except (TypeError, ValueError):
                pass
    return buyers


# ---------------------------------------------------------------------------
# 🗄️ SQLITE (Phase 6 own DB)
# ---------------------------------------------------------------------------
def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS smart_wallets (
            wallet TEXT PRIMARY KEY,
            wins INTEGER DEFAULT 1,
            last_slot INTEGER DEFAULT 0,
            last_seen TEXT DEFAULT '',
            added_at TEXT DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS harvest_log (
            token_address TEXT PRIMARY KEY,
            harvested_at TEXT,
            winners_found INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS convergence_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mint TEXT NOT NULL,
            wallet TEXT NOT NULL,
            ts INTEGER DEFAULT 0,
            sol_spent REAL DEFAULT 0,
            sig TEXT DEFAULT '',
            UNIQUE(sig, wallet, mint)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS convergence_alerts (
            mint TEXT PRIMARY KEY,
            alerted_at TEXT,
            wallet_count INTEGER DEFAULT 0,
            suppressed INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1️⃣ HARVEST: mint smart wallets from proven 3x+ momentum winners
# ---------------------------------------------------------------------------
def harvest(conn: sqlite3.Connection) -> int:
    if not os.path.exists(MOMENTUM_DB):
        log("⚠️ Momentum DB not found - skipping harvest.")
        return 0
    try:
        mconn = sqlite3.connect(f"file:{MOMENTUM_DB}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        log(f"⚠️ Cannot open momentum DB read-only: {exc}")
        return 0
    try:
        winners = mconn.execute(
            "SELECT token_address, started_ts FROM tp_state "
            "WHERE tp2 = 1 AND token_address != ''"
        ).fetchall()
    except sqlite3.Error as exc:
        log(f"⚠️ Momentum DB read issue: {exc}")
        winners = []
    finally:
        mconn.close()

    harvested = 0
    for token, started_iso in winners:
        if harvested >= MAX_HARVEST_PER_RUN:
            break
        cur = conn.execute("SELECT 1 FROM harvest_log WHERE token_address = ?", (token,))
        if cur.fetchone():
            continue
        started_dt = parse_iso(started_iso)
        found = 0
        if started_dt is not None:
            end_ts = int(started_dt.timestamp())
            start_ts = end_ts - 6 * 3600
            sigs = sigs_in_window(token, start_ts, end_ts)
            parses = sigs[-MAX_HARVEST_PARSES:] if sigs else []
            for tx in parse_txs([s.get("signature") for s in parses if s.get("signature")]):
                for wallet, info in extract_sol_buyers(tx, token).items():
                    conn.execute(
                        "INSERT INTO smart_wallets (wallet, wins, last_slot, last_seen, added_at) "
                        "VALUES (?, 1, 0, ?, ?) "
                        "ON CONFLICT(wallet) DO UPDATE SET wins = wins + 1, last_seen = excluded.last_seen",
                        (wallet, datetime.now(timezone.utc).isoformat(),
                         datetime.now(timezone.utc).isoformat()),
                    )
                    found += 1
            conn.commit()
        conn.execute(
            "INSERT OR IGNORE INTO harvest_log (token_address, harvested_at, winners_found) VALUES (?, ?, ?)",
            (token, datetime.now(timezone.utc).isoformat(), found),
        )
        conn.commit()
        harvested += 1
        log(f"🌾 Harvested {found} pre-alert buyer(s) from 3x+ winner {token[:8]}...")
    return harvested


def sigs_in_window(token: str, start_ts: int, end_ts: int) -> list:
    out = []
    before = None
    for _page in range(MAX_HARVEST_PAGES):
        params = {"limit": 1000}
        if before:
            params["before"] = before
        res = rpc("getSignaturesForAddress", [token, params])
        if not isinstance(res, list) or not res:
            break
        for s in res:
            bt = s.get("blockTime") or 0
            if start_ts <= bt <= end_ts and not s.get("err"):
                out.append(s)
        oldest = min((s.get("blockTime") or 0) for s in res)
        if oldest < start_ts:
            break
        before = res[-1].get("signature")
        time.sleep(REQUEST_DELAY_SEC)
    out.sort(key=lambda s: s.get("blockTime") or 0)
    return out


# ---------------------------------------------------------------------------
# 2️⃣ STAKEOUT: watch smart wallets for new accumulation swaps
# ---------------------------------------------------------------------------
def stakeout(conn: sqlite3.Connection) -> int:
    wallets = conn.execute(
        "SELECT wallet, last_slot FROM smart_wallets "
        "ORDER BY wins DESC, added_at ASC LIMIT ?", (MAX_WATCH_WALLETS,)
    ).fetchall()
    if not wallets:
        return 0
    log(f"👁️ Staking out {len(wallets)} smart wallet(s)...")
    parses_left = MAX_PARSES_PER_RUN
    events = 0

    for wallet, last_slot in wallets:
        sigs = get_sigs(wallet, MAX_SIGS_PER_WALLET)
        if sigs:
            max_slot = max((s.get("slot") or 0) for s in sigs if isinstance(s, dict))
            conn.execute("UPDATE smart_wallets SET last_slot = ? WHERE wallet = ?", (max_slot, wallet))
        fresh = [s for s in sigs
                 if isinstance(s, dict) and (s.get("slot") or 0) > (last_slot or 0) and not s.get("err")]
        if not fresh or parses_left <= 0:
            continue
        fresh.sort(key=lambda s: s.get("slot") or 0)
        take = [s.get("signature") for s in fresh[:parses_left] if s.get("signature")]
        parses_left -= len(take)
        for tx in parse_txs(take):
            if not isinstance(tx, dict):
                continue
            sig = tx.get("signature") or ""
            ts = tx.get("timestamp") or 0
            for tt in (tx.get("tokenTransfers") or []):
                if not isinstance(tt, dict):
                    continue
                mint = tt.get("mint") or ""
                if not mint or mint in QUOTE_MINTS or (tt.get("to") or "") != wallet:
                    continue
                sol_spent = 0.0
                for nt in (tx.get("nativeTransfers") or []):
                    if isinstance(nt, dict) and (nt.get("from") or "") == wallet:
                        sol_spent += float(nt.get("amount") or 0.0)
                cur = conn.execute(
                    "INSERT OR IGNORE INTO convergence_events (mint, wallet, ts, sol_spent, sig) "
                    "VALUES (?, ?, ?, ?, ?)", (mint, wallet, ts, sol_spent, sig)
                )
                events += cur.rowcount
        conn.commit()
        time.sleep(REQUEST_DELAY_SEC)
    return events


# ---------------------------------------------------------------------------
# 3️⃣ CONVERGE: 2+ smart wallets on the same sleeping giant = alert
# ---------------------------------------------------------------------------
def dexscreener_check(mint: str):
    """Returns (status, info). status: 'ok' | 'filtered' | 'error'."""
    try:
        resp = requests.get(f"{DEXSCREENER_TOKENS_URL}{mint}", timeout=15)
        if resp.status_code == 429:
            return "error", {}
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log(f"⚠️ DexScreener check failed: {exc}")
        return "error", {}
    pairs = data.get("pairs") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    if not isinstance(pairs, list) or not pairs:
        return "filtered", {}
    best = None
    for p in pairs:
        if not isinstance(p, dict):
            continue
        liq = (p.get("liquidity") or {}).get("usd") or 0
        mcap = p.get("marketCap") or p.get("fdv") or 0
        try:
            liq, mcap = float(liq), float(mcap)
        except (TypeError, ValueError):
            continue
        if best is None or liq > best["liq"]:
            best = {
                "liq": liq, "mcap": mcap,
                "pair": p.get("pairAddress") or "",
                "symbol": (p.get("baseToken") or {}).get("symbol") or "UNKNOWN",
                "created": p.get("pairCreatedAt") or 0,
            }
    if best is None:
        return "filtered", {}
    age_h = ((time.time() * 1000 - best["created"]) / 3600000.0) if best["created"] else 0
    if best["liq"] < MIN_LIQUIDITY_USD or best["mcap"] < MIN_MCAP_USD \
            or best["mcap"] > MAX_MCAP_USD or age_h < MIN_AGE_HOURS:
        return "filtered", best
    best["age_h"] = age_h
    return "ok", best


def check_mint_authorities(mint: str):
    """SPL Mint layout: bytes 0-4 mint_authority option, 46-50 freeze option."""
    result = rpc("getAccountInfo", [mint, {"encoding": "base64"}])
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


def converge(conn: sqlite3.Connection) -> int:
    cutoff = int(time.time()) - CONVERGENCE_WINDOW_H * 3600
    rows = conn.execute(
        "SELECT mint, COUNT(DISTINCT wallet) AS c FROM convergence_events "
        "WHERE ts >= ? GROUP BY mint HAVING c >= ?", (cutoff, MIN_CONVERGENCE)
    ).fetchall()
    fired = 0
    for mint, count in rows:
        cur = conn.execute("SELECT 1 FROM convergence_alerts WHERE mint = ?", (mint,))
        if cur.fetchone():
            continue
        status, info = dexscreener_check(mint)
        if status == "error":
            continue  # retry next run
        if status == "filtered":
            conn.execute(
                "INSERT OR IGNORE INTO convergence_alerts (mint, alerted_at, wallet_count, suppressed) "
                "VALUES (?, ?, ?, 1)", (mint, datetime.now(timezone.utc).isoformat(), count)
            )
            conn.commit()
            continue
        mint_rev, freeze_rev = check_mint_authorities(mint)
        wallets = [w for (w,) in conn.execute(
            "SELECT DISTINCT wallet FROM convergence_events WHERE mint = ? AND ts >= ?",
            (mint, cutoff)
        )]
        auth_txt = "unknown"
        if mint_rev is not None:
            auth_txt = ("Mint ❌ / Freeze ❌" if (mint_rev and freeze_rev)
                        else f"Mint {'❌' if mint_rev else '⚠️'} / Freeze {'❌' if freeze_rev else '⚠️'}")
        tags = "\n".join(f"• <code>{w[:6]}...{w[-4:]}</code>" for w in wallets[:5])
        send_telegram(
            f"🧬 <b>SOLANA SMART MONEY CONVERGENCE</b> 🧬\n"
            f"🪙 <b>{info.get('symbol', 'UNKNOWN')}</b> | age {info.get('age_h', 0):.0f}h\n"
            f"💰 MCap: ${info.get('mcap', 0):,.0f} | 💧 Liq: ${info.get('liq', 0):,.0f}\n"
            f"🔐 Authorities revoked: {auth_txt}\n"
            f"🐋 Smart wallets accumulating ({count}):\n{tags}\n"
            f"📋 <code>{mint}</code>\n"
            f"🔗 <a href='https://dexscreener.com/solana/{info.get('pair', '')}'>DexScreener</a> | "
            f"<a href='https://solscan.io/token/{mint}'>Solscan</a>"
        )
        conn.execute(
            "INSERT OR IGNORE INTO convergence_alerts (mint, alerted_at, wallet_count, suppressed) "
            "VALUES (?, ?, ?, 0)", (mint, datetime.now(timezone.utc).isoformat(), count)
        )
        conn.commit()
        fired += 1
        log(f"🧬 CONVERGENCE ALERT: {info.get('symbol')} | {count} smart wallets.")
        time.sleep(1)
    return fired


# ---------------------------------------------------------------------------
# 🚀 MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    log("🧬 Phase 6: Solana Smart Money Stalker starting...")
    if not HELIUS_API_KEY:
        log("❌ HELIUS_API_KEY missing. Add it to repo secrets. Exiting.")
        return

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    harvested = harvest(conn)
    events = stakeout(conn)
    fired = converge(conn)

    conn.close()
    log(f"✅ Phase 6 complete. Harvested {harvested} winner(s), "
        f"{events} new accumulation event(s), {fired} convergence alert(s).")


if __name__ == "__main__":
    main()
