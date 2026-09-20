"""
🕵️ Insider Hunter — Phase 2: Analysis (wallet_profiler.py)
Scores unprocessed first-buyers from the SQLite DB to identify insiders/snipers.
Sends Telegram alerts for high-confidence insiders (Score >= 75).
"""

import os
import sys
import time
import sqlite3
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# ⚙️ CONFIGURATION
# ---------------------------------------------------------------------------
ALCHEMY_API_KEY = os.environ.get("ALCHEMY_API_KEY", "")
ALCHEMY_URL = "https://base-mainnet.g.alchemy.com/v2/" + ALCHEMY_API_KEY

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DB_PATH = os.environ.get("DB_PATH", "insider_hunter.db")
MAX_BUYERS_PER_RUN = 20  # Hard cap to protect free-tier API limits
REQUEST_DELAY_SEC = 1.0


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
    ensure_column(conn, "buyers", "score", "INTEGER DEFAULT 0")
    ensure_column(conn, "buyers", "nonce", "INTEGER DEFAULT 0")
    ensure_column(conn, "buyers", "block_delta", "INTEGER DEFAULT 0")
    ensure_column(conn, "buyers", "balance", "TEXT DEFAULT '0'")
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    cur = conn.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cur.fetchall()]
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()


# ---------------------------------------------------------------------------
# 🌐 ALCHEMY (Free-tier safe)
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
            if attempt < retries: time.sleep(2 * attempt)
            continue

        if resp.status_code == 429:
            log(f"⚠️ Alchemy 429 rate-limit. Sleeping 5s.")
            time.sleep(5)
            continue
        if 400 <= resp.status_code < 500:
            return None  # Deterministic client error, don't retry
        if resp.status_code >= 500:
            if attempt < retries: time.sleep(2 * attempt)
            continue

        try:
            data = resp.json()
        except ValueError:
            continue

        if not isinstance(data, dict) or "error" in data:
            return None

        time.sleep(REQUEST_DELAY_SEC)
        return data.get("result")
    return None


def get_nonce(wallet: str) -> int:
    result = alchemy_call("eth_getTransactionCount", [wallet, "latest"])
    if result and isinstance(result, str):
        try: return int(result, 16)
        except ValueError: pass
    return -1


def get_token_balance(wallet: str, token_address: str) -> int:
    if not token_address or not wallet: return 0
    # balanceOf(address) selector: 0x70a08231
    data = "0x70a08231" + wallet[2:].lower().zfill(64)
    result = alchemy_call("eth_call", [{"to": token_address, "data": data}, "latest"])
    if result and isinstance(result, str) and result != "0x":
        try: return int(result, 16)
        except ValueError: pass
    return 0


# ---------------------------------------------------------------------------
# 🧠 SCORING ENGINE
# ---------------------------------------------------------------------------
def score_wallet(wallet: str, pool_address: str, target_token: str, created_block: int, buyer_block: int, conn: sqlite3.Connection) -> dict:
    score = 0
    details = []
    
    # 1. Speed (Block Delta)
    block_delta = max(0, buyer_block - created_block)
    if block_delta <= 1:
        score += 40; details.append("⚡ Blk 0-1 (+40)")
    elif block_delta <= 3:
        score += 30; details.append("⚡ Blk 2-3 (+30)")
    elif block_delta <= 10:
        score += 15; details.append("⚡ Blk 4-10 (+15)")
    else:
        details.append(f"🐢 Blk {block_delta} (+0)")
        
    # 2. Freshness (Nonce)
    nonce = get_nonce(wallet)
    if nonce >= 0:
        if nonce <= 5:
            score += 20; details.append(f"👶 Nonce {nonce} (+20)")
        elif nonce <= 20:
            score += 10; details.append(f"🧒 Nonce {nonce} (+10)")
        else:
            details.append(f"🧔 Nonce {nonce} (+0)")
            
    # 3. Recidivism (Historical Snipes)
    cur = conn.execute("SELECT COUNT(*) FROM buyers WHERE wallet = ? AND pool != ?", (wallet, pool_address))
    historical = cur.fetchone()[0]
    recidivism_pts = min(historical * 15, 30)
    score += recidivism_pts
    if historical > 0:
        details.append(f"🔁 {historical} prev (+{recidivism_pts})")
        
    # 4. Dump Check
    balance = get_token_balance(wallet, target_token)
    if balance == 0:
        score += 10; details.append("💸 Dumped (+10)")
    else:
        details.append("💎 Holding (+0)")
        
    return {"score": score, "nonce": nonce, "block_delta": block_delta, "balance": balance, "details": details}


# ---------------------------------------------------------------------------
# 📣 TELEGRAM
# ---------------------------------------------------------------------------
def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        log(f"⚠️ Telegram notify failed: {exc}")


def send_insider_alert(wallet, pool, token, metrics):
    details_str = " | ".join(metrics["details"])
    msg = (
        f"🚨 <b>INSIDER DETECTED</b> 🚨\n"
        f"👛 <code>{wallet}</code>\n"
        f"🏭 Pool: <code>{short(pool)}</code>\n"
        f"🎯 Token: <code>{short(token)}</code>\n"
        f"🏆 Score: <b>{metrics['score']}/100</b>\n"
        f"📊 <i>{details_str}</i>\n"
        f"🔗 <a href='https://basescan.org/address/{wallet}'>BaseScan</a>"
    )
    send_telegram(msg)


# ---------------------------------------------------------------------------
# 🚀 MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    log("🕵️ Insider Hunter — Phase 2 (Profiler) starting...")
    if not ALCHEMY_API_KEY:
        log("❌ ALCHEMY_API_KEY missing. Exiting.")
        sys.exit(1)

    conn = init_db()
    cur = conn.execute(
        "SELECT b.id, b.wallet, b.pool, b.token, b.block_num, p.created_block "
        "FROM buyers b JOIN pools p ON b.pool = p.pool_address "
        "WHERE b.processed = 0 LIMIT ?", (MAX_BUYERS_PER_RUN,)
    )
    rows = cur.fetchall()
    
    if not rows:
        log("🔍 No unprocessed buyers in queue. Profiler sleeping.")
        conn.close()
        return
        
    log(f"🔬 Found {len(rows)} unprocessed buyer(s) to score.")
    scored = 0
    
    for row in rows:
        bid, wallet, pool, token, buyer_block, created_block = row
        log(f"🔬 Profiling {short(wallet)}...")
        
        metrics = score_wallet(wallet, pool, token, created_block, buyer_block, conn)
        
        conn.execute(
            "UPDATE buyers SET processed = 1, score = ?, nonce = ?, block_delta = ?, balance = ? WHERE id = ?",
            (metrics["score"], metrics["nonce"], metrics["block_delta"], str(metrics["balance"]), bid)
        )
        conn.commit()
        scored += 1
        
        log(f"📊 {short(wallet)} scored {metrics['score']}/100.")
        
        if metrics["score"] >= 75:
            send_insider_alert(wallet, pool, token, metrics)
            log(f"🚨 HIGH CONFIDENCE INSIDER ALERT SENT for {short(wallet)}!")

    conn.close()
    log(f"✅ Phase 2 complete. Scored {scored} wallets.")


if __name__ == "__main__":
    main()
