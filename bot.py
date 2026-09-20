"""
🕵️ Insider Hunter — Phase 1: Discovery (bot.py)
Runs every 2 min on GitHub Actions (Base network).
Finds new Uniswap V3 pools, extracts first buyers, stores them in SQLite.
Indentation: 4 spaces ONLY (fixes IndentationError at old line 65).
"""

import os
import sys
import time
import sqlite3
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# ⚙️ CONFIGURATION (free-tier safe tunables)
# ---------------------------------------------------------------------------
ALCHEMY_API_KEY = os.environ.get("ALCHEMY_API_KEY", "")
ALCHEMY_URL = "https://base-mainnet.g.alchemy.com/v2/" + ALCHEMY_API_KEY

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DB_PATH = os.environ.get("DB_PATH", "insider_hunter.db")

# Uniswap V3 Factory on Base + PoolCreated event topic0
FACTORY_ADDRESS = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"

SCAN_BLOCK_WINDOW = 100         # ~2 min of Base blocks (2s blocks) + overlap
MAX_PAGES = 5                   # hard pagination cap (free tier rule)
MAX_POOLS_PER_RUN = 5           # hard cap on pools processed per run
MAX_BUYERS_PER_POOL = 10        # hard cap on first buyers stored per pool
MAX_TRANSFERS_PER_PAGE = 1000   # Alchemy max per page
REQUEST_DELAY_SEC = 1.5         # polite delay between Alchemy calls

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Quote tokens on Base (the "non-insider" side of a pair)
QUOTE_TOKENS = {
    "0x4200000000000000000000000000000000000006",  # WETH
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
}


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def short(addr: str) -> str:
    addr = addr or ""
    return f"{addr[:6]}...{addr[-4:]}" if len(addr) >= 10 else addr


# ---------------------------------------------------------------------------
# 🗄️ SQLITE
# ---------------------------------------------------------------------------
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pools (
            pool_address  TEXT PRIMARY KEY,
            token0        TEXT,
            token1        TEXT,
            target_token  TEXT,
            created_block INTEGER,
            scanned_at    TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS buyers (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet     TEXT NOT NULL,
            pool       TEXT NOT NULL,
            token      TEXT,
            tx_hash    TEXT,
            block_num  INTEGER,
            first_seen TEXT,
            processed  INTEGER DEFAULT 0,
            UNIQUE(wallet, pool)
        )
        """
    )
    conn.commit()
    return conn


def is_known_pool(conn: sqlite3.Connection, pool_address: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM pools WHERE pool_address = ?", (pool_address,)
    )
    return cur.fetchone() is not None


def record_pool(conn, pool_address, token0, token1, target_token, created_block) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO pools "
        "(pool_address, token0, token1, target_token, created_block, scanned_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            pool_address,
            token0 or "",
            token1 or "",
            target_token or "",
            created_block or 0,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def save_buyers(conn, buyers: list, pool_address: str, target_token: str) -> int:
    saved = 0
    for buyer in buyers:
        if not isinstance(buyer, dict):
            continue
        wallet = (buyer.get("wallet") or "").lower()
        if not wallet:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO buyers "
            "(wallet, pool, token, tx_hash, block_num, first_seen, processed) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (
                wallet,
                pool_address,
                target_token or "",
                buyer.get("tx_hash", ""),
                buyer.get("block_num", 0),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        saved += cur.rowcount
    conn.commit()
    return saved


# ---------------------------------------------------------------------------
# 🌐 ALCHEMY (rate-limit + retry safe)
# ---------------------------------------------------------------------------
def alchemy_call(method: str, params: list, retries: int = 3):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(ALCHEMY_URL, json=payload, timeout=30)
            if resp.status_code == 429:
                wait = 5 * attempt
                log(f"⚠️ Alchemy 429 rate-limit. Sleeping {wait}s (attempt {attempt}/{retries}).")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict) or "error" in data:
                err = (data.get("error") or {}) if isinstance(data, dict) else {}
                log(f"⚠️ Alchemy RPC error: {err.get('message', 'unknown')}")
                return None
            time.sleep(REQUEST_DELAY_SEC)  # free-tier courtesy delay
            return data.get("result")
        except requests.exceptions.RequestException as exc:
            log(f"⚠️ Network error (attempt {attempt}/{retries}): {exc}")
            time.sleep(3 * attempt)
        except ValueError as exc:
            log(f"⚠️ Bad JSON from Alchemy (attempt {attempt}/{retries}): {exc}")
            time.sleep(3 * attempt)
    return None


def get_latest_block():
    result = alchemy_call("eth_blockNumber", [])
    if not result or not isinstance(result, str):
        return None
    try:
        return int(result, 16)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 🔎 DISCOVERY
# ---------------------------------------------------------------------------
def get_new_pools(from_block: int, to_block: int) -> list:
    logs = alchemy_call(
        "eth_getLogs",
        [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": FACTORY_ADDRESS,
                "topics": [POOL_CREATED_TOPIC],
            }
        ],
    )
    pools = []
    if not logs or not isinstance(logs, list):
        return pools
    for entry in logs:
        if not isinstance(entry, dict):
            continue
        topics = entry.get("topics") or []
        data = entry.get("data") or "0x"
        block_hex = entry.get("blockNumber") or "0x0"
        # PoolCreated(address idx token0, address idx token1, uint24 idx fee,
        #             int24 tickSpacing, address pool) -> data = 2 words
        if len(topics) < 3 or len(data) < 130:
            continue
        try:
            created_block = int(block_hex, 16)
        except ValueError:
            created_block = 0
        pools.append(
            {
                "token0": "0x" + topics[1][-40:],
                "token1": "0x" + topics[2][-40:],
                "pool": "0x" + data[-40:],
                "created_block": created_block,
            }
        )
    return pools


def pick_target_token(token0, token1):
    t0 = (token0 or "").lower()
    t1 = (token1 or "").lower()
    if not t0 or not t1:
        return None
    if t1 in QUOTE_TOKENS and t0 not in QUOTE_TOKENS:
        return t0
    if t0 in QUOTE_TOKENS and t1 not in QUOTE_TOKENS:
        return t1
    return None  # ambiguous pair -> skip, never guess


def get_first_buyers(pool_address: str, target_token: str, created_block: int) -> list:
    """
    Heuristic: a BUY = ERC-20 transfer FROM the pool TO a wallet
    (the pool pays out the token when someone swaps into it).
    Pages ascending, capped at MAX_PAGES and MAX_BUYERS_PER_POOL.
    NOTE: the `return buyers` below is indented exactly 4 spaces —
    this is the line that was broken (old line 65).
    """
    buyers = []
    seen = set()
    page_key = None

    for _page in range(MAX_PAGES):
        params = {
            "fromBlock": hex(created_block),
            "toBlock": "latest",
            "category": ["erc20"],
            "fromAddress": pool_address,
            "contractAddress": target_token,
            "order": "asc",
            "maxCount": hex(MAX_TRANSFERS_PER_PAGE),
            "withMetadata": False,
            "excludeZeroValue": True,
        }
        if page_key:
            params["pageKey"] = page_key

        result = alchemy_call("alchemy_getAssetTransfers", [params])
        if not result or not isinstance(result, dict):
            break

        transfers = result.get("transfers") or []
        if not isinstance(transfers, list) or not transfers:
            break

        for transfer in transfers:
            if not isinstance(transfer, dict):
                continue
            wallet = (transfer.get("to") or "").lower()
            if not wallet or wallet in (pool_address.lower(), ZERO_ADDRESS):
                continue
            if wallet in seen:
                continue
            seen.add(wallet)
            try:
                block_num = int(transfer.get("blockNum") or "0x0", 16)
            except ValueError:
                block_num = 0
            buyers.append(
                {
                    "wallet": wallet,
                    "tx_hash": transfer.get("hash") or "",
                    "block_num": block_num,
                }
            )
            if len(buyers) >= MAX_BUYERS_PER_POOL:
                break

        if len(buyers) >= MAX_BUYERS_PER_POOL:
            break

        page_key = result.get("pageKey")
        if not page_key:
            break

    return buyers


# ---------------------------------------------------------------------------
#  TELEGRAM (optional, non-fatal)
# ---------------------------------------------------------------------------
def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        log(f"⚠️ Telegram notify failed (non-fatal): {exc}")


# ---------------------------------------------------------------------------
# 🚀 MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    log("🕵️ Insider Hunter — Phase 1 (Discovery) starting...")

    if not ALCHEMY_API_KEY:
        log("❌ ALCHEMY_API_KEY is not set. Add it to repo secrets. Exiting.")
        sys.exit(1)

    conn = init_db()

    latest_block = get_latest_block()
    if latest_block is None:
        log("⚠️ Could not read latest block (transient). Safe-exit; next cron run retries.")
        conn.close()
        sys.exit(0)

    from_block = max(0, latest_block - SCAN_BLOCK_WINDOW)
    pools = get_new_pools(from_block, latest_block)
    log(f"🔎 Blocks {from_block}->{latest_block}: {len(pools)} PoolCreated event(s).")

    processed = 0
    discovered = 0

    for pool in pools:
        pool_address = (pool.get("pool") or "").lower()
        if not pool_address or is_known_pool(conn, pool_address):
            continue
        if processed >= MAX_POOLS_PER_RUN:
            log(f"⛔ Hard cap {MAX_POOLS_PER_RUN} pools/run reached — remainder next run.")
            break

        processed += 1
        token0 = (pool.get("token0") or "").lower()
        token1 = (pool.get("token1") or "").lower()
        created_block = pool.get("created_block", 0)

        target_token = pick_target_token(token0, token1)
        if not target_token:
            log(f"⏭️ Pool {short(pool_address)} has no quote-token pair — skipping.")
            record_pool(conn, pool_address, token0, token1, "", created_block)
            continue

        log(f"🆕 New pool {short(pool_address)} | target {short(target_token)} | block {created_block}")
        buyers = get_first_buyers(pool_address, target_token, created_block)
        saved = save_buyers(conn, buyers, pool_address, target_token)
        record_pool(conn, pool_address, token0, token1, target_token, created_block)
        discovered += 1
        log(f"💾 Stored {saved} first-buyer(s) for {short(pool_address)}.")

        if buyers:
            lines = "\n".join(
                f"  • <code>{(b.get('wallet') or '')}</code>" for b in buyers[:5]
            )
            send_telegram(
                f"️ <b>New Base pool</b> <code>{pool_address}</code>\n"
                f"🎯 Target: <code>{target_token}</code>\n"
                f"👛 First buyers:\n{lines}"
            )

    conn.close()
    log(f"✅ Phase 1 complete. New pools processed: {discovered}.")


if __name__ == "__main__":
    main()
