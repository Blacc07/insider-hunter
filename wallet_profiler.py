"""
🕵️ Insider Hunter — Phase 2 v2: Forensic Wallet Profiler (wallet_profiler.py)
Scores unprocessed first-buyers with on-chain forensics tuned for Base
(FCFS sequencer => bundle/creator/funder/cluster signals, NOT gas bidding).

Signals: bundle detection, creator match, burner funding age, fund recycling,
funder clustering (syndicate), exit velocity, contract-wallet, nonce, recidivism.
Free-tier safe: ~6 RPC calls per wallet, hard cap 8 wallets per run.
"""

import os
import sys
import time
import sqlite3
import requests
from datetime import datetime, timezone
from Crypto.Hash import keccak

# ---------------------------------------------------------------------------
# ⚙️ CONFIGURATION
# ---------------------------------------------------------------------------
ALCHEMY_API_KEY = os.environ.get("ALCHEMY_API_KEY", "")
ALCHEMY_URL = "https://base-mainnet.g.alchemy.com/v2/" + ALCHEMY_API_KEY

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DB_PATH = os.environ.get("DB_PATH", "insider_hunter.db")

MAX_BUYERS_PER_RUN = 8          # hard cap: protects free-tier CU budget
REQUEST_DELAY_SEC = 0.6         # polite delay between Alchemy calls
BURNER_WINDOW_BLOCKS = 7200     # ~4h on Base (2s blocks)
HOT_BURNER_WINDOW_BLOCKS = 300  # ~10 min
FAST_EXIT_WINDOW_BLOCKS = 14400 # ~8h
DUMP_RATIO = 0.9                # >=90% of tokens sold = exited
ALERT_INSIDER = 75
ALERT_SUSPICIOUS = 55

# Factory metadata needed to re-locate pool-creation tx (labels match pools.factory)
FACTORY_BY_LABEL = {
    "Uniswap V3": {
        "address": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        "signature": "PoolCreated(address,address,uint24,int24,address)",
        "pool_word_index": 1,
    },
    "Aerodrome": {
        "address": "0x420DD381b31aEf6683db6B902084cB0FFECe40Da",
        "signature": "PoolCreated(address,address,bool,address,uint256)",
        "pool_word_index": 0,
    },
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


# ---------------------------------------------------------------------------
# 🗄️ SQLITE (safe migrations for new forensic columns)
# ---------------------------------------------------------------------------
def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    cur = conn.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cur.fetchall()]
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    ensure_column(conn, "buyers", "score", "INTEGER DEFAULT 0")
    ensure_column(conn, "buyers", "nonce", "INTEGER DEFAULT 0")
    ensure_column(conn, "buyers", "block_delta", "INTEGER DEFAULT 0")
    ensure_column(conn, "buyers", "balance", "TEXT DEFAULT '0'")
    ensure_column(conn, "buyers", "funder", "TEXT DEFAULT ''")
    ensure_column(conn, "buyers", "funding_block", "INTEGER DEFAULT 0")
    ensure_column(conn, "buyers", "sell_ratio", "REAL DEFAULT 0")
    ensure_column(conn, "buyers", "flags", "TEXT DEFAULT ''")
    ensure_column(conn, "buyers", "bundled", "INTEGER DEFAULT 0")
    ensure_column(conn, "buyers", "is_creator", "INTEGER DEFAULT 0")
    return conn


# ---------------------------------------------------------------------------
# 🌐 ALCHEMY (smart retries: never retry deterministic 4xx)
# ---------------------------------------------------------------------------
def alchemy_call(method: str, params: list, retries: int = 2):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    attempt = 0
    while attempt < retries:
        attempt += 1
        try:
            resp = requests.post(ALCHEMY_URL, json=payload, timeout=30)
        except requests.exceptions.RequestException as exc:
            log(f"⚠️ Network error on {method} (attempt {attempt}/{retries}): {exc}")
            if attempt < retries:
                time.sleep(2 * attempt)
            continue

        if resp.status_code == 429:
            log(f"⚠️ Alchemy 429 rate-limit on {method}. Sleeping 5s.")
            time.sleep(5)
            continue
        if 400 <= resp.status_code < 500:
            body = (resp.text or "")[:200].replace("\n", " ")
            log(f"❌ Alchemy {resp.status_code} on {method} (client error, NOT retrying): {body}")
            return None
        if resp.status_code >= 500:
            if attempt < retries:
                time.sleep(2 * attempt)
            continue

        try:
            data = resp.json()
        except ValueError:
            continue

        if not isinstance(data, dict) or "error" in data:
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            log(f"⚠️ Alchemy RPC error on {method}: {err.get('message', 'unknown')}")
            return None

        time.sleep(REQUEST_DELAY_SEC)
        return data.get("result")
    return None


# ---------------------------------------------------------------------------
# 🔬 FORENSIC PRIMITIVES (all null-safe)
# ---------------------------------------------------------------------------
def get_pool_creation_info(pool_address: str, factory_label: str, created_block: int, cache: dict) -> dict:
    """Returns {'creation_tx':..,'creator':..} for a pool, cached per run."""
    if pool_address in cache:
        return cache[pool_address]
    info = {"creation_tx": None, "creator": None}
    cfg = FACTORY_BY_LABEL.get(factory_label)
    if cfg and created_block and created_block > 0:
        logs = alchemy_call(
            "eth_getLogs",
            [
                {
                    "fromBlock": hex(created_block),
                    "toBlock": hex(created_block),
                    "address": cfg.get("address", ""),
                    "topics": [event_topic(cfg.get("signature", ""))],
                }
            ],
        )
        creation_tx = None
        if isinstance(logs, list):
            word_index = cfg.get("pool_word_index", 0)
            for entry in logs:
                if not isinstance(entry, dict):
                    continue
                data_hex = (entry.get("data") or "0x")[2:]
                words = [data_hex[i:i + 64] for i in range(0, len(data_hex), 64)]
                if len(words) <= word_index:
                    continue
                if ("0x" + words[word_index][-40:]).lower() == pool_address.lower():
                    creation_tx = (entry.get("transactionHash") or "").lower() or None
                    break
        if creation_tx:
            tx = alchemy_call("eth_getTransactionByHash", [creation_tx])
            if isinstance(tx, dict):
                info["creation_tx"] = creation_tx
                info["creator"] = (tx.get("from") or "").lower() or None
    cache[pool_address] = info
    return info


def get_funding_info(wallet: str):
    """First-ever inbound ETH transfer = funding source. Returns (funder, block) or (None, None)."""
    result = alchemy_call(
        "alchemy_getAssetTransfers",
        [
            {
                "fromBlock": "0x0",
                "toBlock": "latest",
                "category": ["external"],
                "toAddress": wallet,
                "order": "asc",
                "maxCount": hex(5),
                "excludeZeroValue": True,
            }
        ],
    )
    if not result or not isinstance(result, dict):
        return None, None
    transfers = result.get("transfers") or []
    if not transfers or not isinstance(transfers[0], dict):
        return None, None
    first = transfers[0]
    funder = (first.get("from") or "").lower() or None
    try:
        funding_block = int(first.get("blockNum") or "0x0", 16)
    except ValueError:
        funding_block = None
    return funder, funding_block


def sent_back_to_funder(wallet: str, funder: str, buy_block: int) -> bool:
    if not funder:
        return False
    result = alchemy_call(
        "alchemy_getAssetTransfers",
        [
            {
                "fromBlock": hex(buy_block),
                "toBlock": "latest",
                "category": ["external"],
                "fromAddress": wallet,
                "toAddress": funder,
                "order": "asc",
                "maxCount": hex(1),
            }
        ],
    )
    if not result or not isinstance(result, dict):
        return False
    transfers = result.get("transfers") or []
    return bool(transfers)


def sum_token_flow(from_addr, to_addr, token: str, from_block: int):
    """Sums ERC-20 token amounts (float) + last block for a directed flow. Returns (total, last_block)."""
    params = {
        "fromBlock": hex(from_block),
        "toBlock": "latest",
        "category": ["erc20"],
        "contractAddress": token,
        "order": "asc",
        "maxCount": hex(100),
        "excludeZeroValue": True,
    }
    if from_addr:
        params["fromAddress"] = from_addr
    if to_addr:
        params["toAddress"] = to_addr
    result = alchemy_call("alchemy_getAssetTransfers", [params])
    total = 0.0
    last_block = 0
    if result and isinstance(result, dict):
        for t in (result.get("transfers") or []):
            if not isinstance(t, dict):
                continue
            try:
                total += float(t.get("value") or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                last_block = max(last_block, int(t.get("blockNum") or "0x0", 16))
            except ValueError:
                pass
    return total, last_block


def is_contract(address: str) -> bool:
    if not address:
        return False
    code = alchemy_call("eth_getCode", [address, "latest"])
    return bool(code and isinstance(code, str) and code != "0x")


def get_nonce(wallet: str) -> int:
    result = alchemy_call("eth_getTransactionCount", [wallet, "latest"])
    if result and isinstance(result, str):
        try:
            return int(result, 16)
        except ValueError:
            pass
    return -1


# ---------------------------------------------------------------------------
# 🧠 SCORING ENGINE
# ---------------------------------------------------------------------------
def score_wallet(row: dict, conn: sqlite3.Connection, pool_cache: dict) -> dict:
    wallet = row["wallet"]
    pool = row["pool"]
    token = row["token"]
    buy_tx = row["tx_hash"]
    buy_block = row["block_num"] or 0
    created_block = row["created_block"] or 0

    score = 0
    flags = []

    # 1) Speed
    block_delta = max(0, buy_block - created_block)
    if block_delta <= 1:
        score += 30; flags.append("⚡ Blk0-1 +30")
    elif block_delta <= 3:
        score += 22; flags.append("⚡ Blk2-3 +22")
    elif block_delta <= 10:
        score += 12; flags.append("⚡ Blk4-10 +12")

    # 2) Bundle + creator match
    creation = get_pool_creation_info(pool, row["factory"], created_block, pool_cache)
    bundled = 0
    is_creator = 0
    if creation.get("creation_tx") and buy_tx and buy_tx.lower() == creation["creation_tx"]:
        bundled = 1
        score += 35; flags.append("📦 Bundled w/ creation +35")
    if creation.get("creator") and creation["creator"] == wallet:
        is_creator = 1
        score += 35; flags.append("👑 Buyer=creator +35")

    # 3) Burner funding trail
    funder, funding_block = get_funding_info(wallet)
    if funder and funding_block is not None:
        age = buy_block - funding_block
        if 0 <= age <= HOT_BURNER_WINDOW_BLOCKS:
            score += 20; flags.append("🔥 Funded<10min +20")
        elif age <= BURNER_WINDOW_BLOCKS:
            score += 15; flags.append("💰 Funded<4h +15")
        if sent_back_to_funder(wallet, funder, buy_block):
            score += 15; flags.append("🔄 Funds returned +15")
        if is_contract(funder):
            score += 10; flags.append("🏭 Funder=contract +10")
    else:
        funder = ""
        funding_block = 0

    # 4) Syndicate clustering (our own DB memory = the edge)
    cluster = 0
    if funder:
        cur = conn.execute(
            "SELECT COUNT(*) FROM buyers WHERE funder = ? AND wallet != ?",
            (funder, wallet),
        )
        cluster = cur.fetchone()[0] or 0
        if cluster >= 1:
            score += 20; flags.append(f"🕸️ Funder x{cluster + 1} +20")

    # 5) Exit velocity
    bought, _ = sum_token_flow(pool, wallet, token, created_block)
    sold, last_sell = sum_token_flow(wallet, None, token, buy_block)
    sell_ratio = (sold / bought) if bought > 0 else 0.0
    if sell_ratio >= DUMP_RATIO:
        if (last_sell - buy_block) <= FAST_EXIT_WINDOW_BLOCKS:
            score += 10; flags.append("💸 Fast full exit +10")
        else:
            score += 5; flags.append("💸 Full exit +5")

    # 6) Contract wallet
    if is_contract(wallet):
        score += 5; flags.append("🤖 Contract wallet +5")

    # 7) Nonce freshness
    nonce = get_nonce(wallet)
    if 0 <= nonce <= 5:
        score += 10; flags.append(f"👶 Nonce {nonce} +10")
    elif 0 <= nonce <= 20:
        score += 5; flags.append(f"🧒 Nonce {nonce} +5")

    # 8) Recidivism (wallet identity)
    cur = conn.execute(
        "SELECT COUNT(*) FROM buyers WHERE wallet = ? AND pool != ? AND processed = 1",
        (wallet, pool),
    )
    prior = cur.fetchone()[0] or 0
    if prior >= 1:
        pts = min(prior * 10, 20)
        score += pts; flags.append(f"🔁 {prior} prior +{pts}")

    score = min(score, 100)
    return {
        "score": score,
        "nonce": nonce,
        "block_delta": block_delta,
        "funder": funder or "",
        "funding_block": funding_block or 0,
        "sell_ratio": round(sell_ratio, 4),
        "flags": " | ".join(flags) if flags else "clean",
        "bundled": bundled,
        "is_creator": is_creator,
    }


# ---------------------------------------------------------------------------
# 📣 TELEGRAM
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


def send_alert(tier: str, row: dict, m: dict) -> None:
    header = "🚨 <b>INSIDER DETECTED</b> 🚨" if tier == "INSIDER" else "⚠️ <b>SUSPICIOUS WALLET</b> ⚠️"
    lines = [
        header,
        f"👛 <code>{row['wallet']}</code>",
        f"🏭 Pool: <code>{short(row['pool'])}</code> | 🎯 Token: <code>{short(row['token'])}</code>",
        f"🏆 Score: <b>{m['score']}/100</b>",
        f"📊 {m['flags']}",
    ]
    if m.get("funder"):
        lines.append(f"💰 Funder: <code>{short(m['funder'])}</code>")
    lines.append(f"🔗 <a href='https://basescan.org/address/{row['wallet']}'>Wallet</a>")
    send_telegram("\n".join(lines))


# ---------------------------------------------------------------------------
# 🚀 MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    log("🕵️ Insider Hunter — Phase 2 v2 (Forensic Profiler) starting...")
    if not ALCHEMY_API_KEY:
        log("❌ ALCHEMY_API_KEY missing. Exiting.")
        sys.exit(1)

    conn = init_db()
    cur = conn.execute(
        "SELECT b.id, b.wallet, b.pool, b.token, b.tx_hash, b.block_num, "
        "p.created_block, p.factory "
        "FROM buyers b JOIN pools p ON b.pool = p.pool_address "
        "WHERE b.processed = 0 LIMIT ?",
        (MAX_BUYERS_PER_RUN,),
    )
    raw = cur.fetchall()
    if not raw:
        log("🔍 No unprocessed buyers in queue. Profiler sleeping.")
        conn.close()
        return

    log(f"🔬 Found {len(raw)} unprocessed buyer(s) to forensically score.")
    pool_cache = {}
    scored = 0

    for r in raw:
        row = {
            "id": r[0], "wallet": (r[1] or "").lower(), "pool": (r[2] or "").lower(),
            "token": (r[3] or "").lower(), "tx_hash": (r[4] or "").lower(),
            "block_num": r[5] or 0, "created_block": r[6] or 0, "factory": r[7] or "",
        }
        if not row["wallet"]:
            conn.execute("UPDATE buyers SET processed = 1 WHERE id = ?", (row["id"],))
            conn.commit()
            continue

        log(f"🔬 Profiling {short(row['wallet'])} on pool {short(row['pool'])}...")
        m = score_wallet(row, conn, pool_cache)

        conn.execute(
            "UPDATE buyers SET processed = 1, score = ?, nonce = ?, block_delta = ?, "
            "funder = ?, funding_block = ?, sell_ratio = ?, flags = ?, bundled = ?, is_creator = ? "
            "WHERE id = ?",
            (
                m["score"], m["nonce"], m["block_delta"], m["funder"], m["funding_block"],
                m["sell_ratio"], m["flags"], m["bundled"], m["is_creator"], row["id"],
            ),
        )
        conn.commit()
        scored += 1
        log(f"📊 {short(row['wallet'])} scored {m['score']}/100 | {m['flags']}")

        if m["score"] >= ALERT_INSIDER:
            send_alert("INSIDER", row, m)
            log(f"🚨 INSIDER ALERT SENT for {short(row['wallet'])}!")
        elif m["score"] >= ALERT_SUSPICIOUS:
            send_alert("SUSPICIOUS", row, m)
            log(f"⚠️ SUSPICIOUS ALERT SENT for {short(row['wallet'])}!")

    conn.close()
    log(f"✅ Phase 2 v2 complete. Scored {scored} wallets.")


if __name__ == "__main__":
    main()
