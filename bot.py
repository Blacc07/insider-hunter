"""
🕵️ Insider Hunter — Phase 1: Discovery (bot.py)
v4 CHANGES: Added IGNORE_ADDRESSES to filter out Burn Addresses and 
Uniswap V3 Protocol Contracts (Position Managers/Routers) so we only 
track real human/syndicate wallets.
"""

import os
import sys
import time
import sqlite3
import requests
from datetime import datetime, timezone
from Crypto.Hash import keccak

# ---------------------------------------------------------------------------
# ⚙️ CONFIGURATION (free-tier safe tunables)
# ---------------------------------------------------------------------------
ALCHEMY_API_KEY = os.environ.get("ALCHEMY_API_KEY", "")
ALCHEMY_URL = "https://base-mainnet.g.alchemy.com/v2/" + ALCHEMY_API_KEY

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DB_PATH = os.environ.get("DB_PATH", "insider_hunter.db")

SCAN_BLOCK_WINDOW = 200          # ~6.7 min of Base blocks + cron-jitter overlap
FREE_TIER_LOG_RANGE = 10         # Alchemy free tier: max blocks per eth_getLogs query (Base)
MAX_SLICES_PER_FACTORY = 22      # hard cap: 22 slices x 10 blocks = 220 blocks max per factory/run
MAX_PAGES = 5                    # hard pagination cap (free tier rule)
MAX_POOLS_PER_RUN = 5            # hard cap on pools processed per run
MAX_BUYERS_PER_POOL = 10         # hard cap on first buyers stored per pool
MAX_TRANSFERS_PER_PAGE = 1000    # Alchemy max per page
REQUEST_DELAY_SEC = 1.0          # polite delay between Alchemy calls

# 🛑 THE BOUNCER IGNORE LIST (Protocol mechanics, not real buyers)
IGNORE_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",  # Zero address
    "0x000000000000000000000000000000000000dead",  # Dead/Burn address
    "0x0000000000000000000000000000000000000001",  # Precompile
    "0x03a520b32c04bf3beef7beb72e919cf822ed34f1",  # Uniswap V3 Position Manager (Base)
    "0x2626664c2603336e57b271c5c0b26f421741e481",  # Uniswap V3 Swap Router (Base)
}

# Canonical factory deployments on Base.
FACTORIES = {
    "uniswap_v3": {
        "label": "Uniswap V3",
        "address": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        "signature": "PoolCreated(address,address,uint24,int24,address)",
        "pool_word_index": 1,
    },
    "aerodrome": {
        "label": "Aerodrome",
        "address": "0x420DD381b31aEf6683db6B902084cB0FFECe40Da",
        "signature": "PoolCreated(address,address,bool,address,uint256)",
        "pool_word_index": 0,
    },
}

# Quote tokens on Base (the "non-insider" side of a pair), lowercase
QUOTE_TOKENS = {
    "0x4200000000000000000000000000000000000006",  # WETH
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
}


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def short(addr: str) -> str:
    addr = addr or ""
    return f"{addr[:6]}...{addr[-4:]}" if len(addr) >= 10 else addr


def event_topic(signature: str) -> str:
    digest = keccak.new(digest_bits=256)
    digest.update(signature.encode("utf-8"))
    return "0x" + digest.hexdigest()


def prepare_factories() -> list:
    prepared = []
    for key, cfg in FACTORIES.items():
        entry = dict(cfg)
        entry["key"] = key
        entry["topic0"] = event_topic(cfg.get("signature", ""))
        prepared.append(entry)
    return prepared


# ---------------------------------------------------------------------------
# 🗄️ SQLITE
# ---------------------------------------------------------------------------
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pools (
            pool_address  TEXT PRIMARY KEY,
            factory       TEXT DEFAULT '',
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
    ensure_column(conn, "pools", "factory", "TEXT DEFAULT ''")
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    cur = conn.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cur.fetchall()]
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()


def is_known_pool(conn: sqlite3.Connection, pool_address: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM pools WHERE pool_address = ?", (pool_address,)
    )
    return cur.fetchone() is not None


def record_pool(conn, pool_address, factory_label, token0, token1, target_token, created_block) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO pools "
        "(pool_address, factory, token0, token1, target_token, created_block, scanned_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            pool_address,
            factory_label or "",
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
# 🌐 ALCHEMY (free-tier safe: smart retries + exact error bodies)
# ---------------------------------------------------------------------------
def alchemy_call(method: str, params: list, retries: int = 3):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    attempt = 0
    while attempt < retries:
        attempt += 1
        try:
            resp = requests.post(ALCHEMY_URL, json=payload, timeout=30)
        except requests.exceptions.RequestException as exc:
            log(f"⚠️ Network error on {method} (attempt {attempt}/{retries}): {exc}")
            if attempt < retries:
                time.sleep(3 * attempt)
            continue

        if resp.status_code == 429:
            wait = 5 * attempt
            log(f"⚠️ Alchemy 429 rate-limit on {method}. Sleeping {wait}s (attempt {attempt}/{retries}).")
            time.sleep(wait)
            continue

        if 400 <= resp.status_code < 500:
            body = (resp.text or "")[:300].replace("\n", " ")
            log(f"❌ Alchemy {resp.status_code} on {method} (client error, NOT retrying): {body}")
            return None

        if resp.status_code >= 500:
            log(f"⚠️ Alchemy {resp.status_code} on {method} (server error, attempt {attempt}/{retries}).")
            if attempt < retries:
                time.sleep(3 * attempt)
            continue

        try:
            data = resp.json()
        except ValueError as exc:
            log(f"⚠️ Bad JSON from Alchemy on {method} (attempt {attempt}/{retries}): {exc}")
            if attempt < retries:
                time.sleep(3 * attempt)
            continue

        if not isinstance(data, dict) or "error" in data:
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            log(f"⚠️ Alchemy RPC error on {method}: {err.get('message', 'unknown')}")
            return None

        time.sleep(REQUEST_DELAY_SEC)
        return data.get("result")
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
# 🔎 DISCOVERY (10-block slices = free-tier legal)
# ---------------------------------------------------------------------------
def build_slices(from_block: int, to_block: int) -> tuple:
    slices = []
    start = from_block
    while start <= to_block and len(slices) < MAX_SLICES_PER_FACTORY:
        end = min(start + FREE_TIER_LOG_RANGE - 1, to_block)
        slices.append((start, end))
        start = end + 1
    return slices, start


def parse_pool_created(factory: dict, entry: dict) -> dict:
    if not isinstance(entry, dict):
        return None
    topics = entry.get("topics") or []
    data_hex = (entry.get("data") or "0x")[2:]
    block_hex = entry.get("blockNumber") or "0x0"
    words = [data_hex[i:i + 64] for i in range(0, len(data_hex), 64)]
    word_index = factory.get("pool_word_index", 0)
    if len(topics) < 3 or len(words) <= word_index:
        return None
    try:
        created_block = int(block_hex, 16)
    except ValueError:
        created_block = 0
    return {
        "factory": factory.get("label", "unknown"),
        "pool": ("0x" + words[word_index][-40:]).lower(),
        "token0": ("0x" + topics[1][-40:]).lower(),
        "token1": ("0x" + topics[2][-40:]).lower(),
        "created_block": created_block,
    }


def get_new_pools(factory: dict, from_block: int, to_block: int) -> list:
    slices, stopped_at = build_slices(from_block, to_block)
    if stopped_at <= to_block:
        log(
            f"⛔ {factory.get('label')}: slice cap reached at block {stopped_at - 1}; "
            f"remainder deferred to next run."
        )

    pools = []
    seen = set()
    for (sb, eb) in slices:
        logs = alchemy_call(
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(sb),
                    "toBlock": hex(eb),
                    "address": factory.get("address", ""),
                    "topics": [factory.get("topic0", "")],
                }
            ],
        )
        if logs is None:
            log(f"⚠️ {factory.get('label')}: getLogs failed for blocks {sb}-{eb}; skipping slice.")
            continue
        if not isinstance(logs, list):
            continue
        for entry in logs:
            parsed = parse_pool_created(factory, entry)
            if not parsed:
                continue
            if parsed["pool"] in seen:
                continue
            seen.add(parsed["pool"])
            pools.append(parsed)
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
    return None


def get_first_buyers(pool_address: str, target_token: str, created_block: int) -> list:
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
            
            # 🛑 BOUNCER CHECK: Ignore pool, zero, dead, and router addresses
            if not wallet or wallet in IGNORE_ADDRESSES or wallet == pool_address.lower():
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
# 📣 TELEGRAM (optional, non-fatal)
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
    factories = prepare_factories()

    all_pools = []
    for factory in factories:
        found = get_new_pools(factory, from_block, latest_block)
        log(f"🏭 {factory.get('label')}: {len(found)} PoolCreated event(s).")
        all_pools.extend(found)

    processed = 0
    discovered = 0

    for pool in all_pools:
        pool_address = (pool.get("pool") or "").lower()
        if not pool_address or is_known_pool(conn, pool_address):
            continue
        if processed >= MAX_POOLS_PER_RUN:
            log(f"⛔ Hard cap {MAX_POOLS_PER_RUN} pools/run reached — remainder next run.")
            break

        processed += 1
        factory_label = pool.get("factory", "unknown")
        token0 = (pool.get("token0") or "").lower()
        token1 = (pool.get("token1") or "").lower()
        created_block = pool.get("created_block", 0)

        target_token = pick_target_token(token0, token1)
        if not target_token:
            log(f"⏭️ Pool {short(pool_address)} ({factory_label}) has no quote-token pair — skipping.")
            record_pool(conn, pool_address, factory_label, token0, token1, "", created_block)
            continue

        log(
            f"🆕 New {factory_label} pool {short(pool_address)} | "
            f"target {short(target_token)} | block {created_block}"
        )
        buyers = get_first_buyers(pool_address, target_token, created_block)
        saved = save_buyers(conn, buyers, pool_address, target_token)
        record_pool(conn, pool_address, factory_label, token0, token1, target_token, created_block)
        discovered += 1
        log(f"💾 Stored {saved} first-buyer(s) for {short(pool_address)}.")

        if buyers:
            lines = "\n".join(
                f"  • <code>{(b.get('wallet') or '')}</code>" for b in buyers[:5]
            )
            send_telegram(
                f"🏭 <b>{factory_label}</b> | 🆕 <b>New Base pool</b> <code>{pool_address}</code>\n"
                f"🎯 Target: <code>{target_token}</code>\n"
                f"👛 First buyers:\n{lines}"
            )

    conn.close()
    log(f"✅ Phase 1 complete. New pools processed: {discovered}.")


if __name__ == "__main__":
    main()
