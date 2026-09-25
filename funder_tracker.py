"""
🕵️‍♂️ Phase 4: Pre-Launch Syndicate Tracker (funder_tracker.py)
Monitors known high-value "Parent Funders" on Base chain.
Alerts the second they fund a new "Burner" wallet, giving a 2-10 minute 
head start before the actual token contract is deployed.
"""

import os
import sqlite3
import time
import requests
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# ⚙️ CONFIGURATION
# ---------------------------------------------------------------------------
ALCHEMY_API_KEY = os.environ.get("ALCHEMY_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PATH = os.environ.get("DB_PATH", "insider_hunter.db")

# Base Chain Alchemy Endpoint
BASE_RPC_URL = f"https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"

# We only track funders who have spawned high-score insiders
MIN_FUNDER_SCORE = 80 
# Look back at transfers from the last 2 hours
LOOKBACK_HOURS = 2 
# Max funders to check per run to respect Alchemy free-tier rate limits
MAX_FUNDERS_PER_RUN = 20 


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)

def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as exc:
        log(f"⚠️ Telegram notify failed: {exc}")

# ---------------------------------------------------------------------------
# 🗄️ SQLITE SETUP
# ---------------------------------------------------------------------------
def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_launches (
            burner_address TEXT PRIMARY KEY,
            funder_address TEXT,
            funded_at TEXT,
            tx_hash TEXT,
            alerted INTEGER DEFAULT 1
        )
    """)
    conn.commit()

def get_high_value_funders(conn: sqlite3.Connection) -> list:
    """Extracts unique parent funders linked to high-scoring insider wallets."""
    cur = conn.execute("""
        SELECT DISTINCT funder, COUNT(*) as launch_count 
        FROM buyers 
        WHERE score >= ? AND funder != '' AND funder IS NOT NULL
        GROUP BY funder
        ORDER BY launch_count DESC
        LIMIT ?
    """, (MIN_FUNDER_SCORE, MAX_FUNDERS_PER_RUN))
    return [row[0] for row in cur.fetchall()]

def is_already_alerted(conn: sqlite3.Connection, burner_address: str) -> bool:
    cur = conn.execute("SELECT 1 FROM pending_launches WHERE burner_address = ?", (burner_address,))
    return cur.fetchone() is not None

def record_alert(conn: sqlite3.Connection, burner: str, funder: str, tx_hash: str, funded_at: str):
    conn.execute(
        "INSERT OR IGNORE INTO pending_launches (burner_address, funder_address, funded_at, tx_hash) VALUES (?, ?, ?, ?)",
        (burner, funder, funded_at, tx_hash)
    )
    conn.commit()

# ---------------------------------------------------------------------------
# 🌐 ALCHEMY API (Base Chain)
# ---------------------------------------------------------------------------
def get_recent_transfers(funder_address: str) -> list:
    """Fetches outgoing external ETH transfers from the funder."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "alchemy_getAssetTransfers",
        "params": [{
            "fromBlock": "0x0", 
            "toBlock": "latest",
            "fromAddress": funder_address,
            "category": ["external"],
            "withMetadata": True,
            "maxCount": "0x14" # Last 20 transfers
        }]
    }
    try:
        resp = requests.post(BASE_RPC_URL, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", {}).get("transfers", [])
    except Exception as e:
        log(f"⚠️ Alchemy fetch failed for {funder_address[:6]}...: {e}")
        return []

# ---------------------------------------------------------------------------
# 🧠 THE INTERCEPTION LOGIC
# ---------------------------------------------------------------------------
def analyze_funder(conn: sqlite3.Connection, funder_address: str):
    transfers = get_recent_transfers(funder_address)
    if not transfers:
        return

    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    alerts_fired = 0

    for tx in transfers:
        # Parse Alchemy timestamp (e.g., "2026-09-25T15:28:28.388Z")
        try:
            tx_time_str = tx.get("metadata", {}).get("blockTimestamp", "")
            if tx_time_str:
                # Handle Z suffix for fromisoformat in Python 3.10
                tx_time = datetime.fromisoformat(tx_time_str.replace("Z", "+00:00"))
            else:
                continue
        except ValueError:
            continue

        # Only look at transfers in the last X hours
        if tx_time < cutoff_time:
            continue

        burner = tx.get("to", "").lower()
        tx_hash = tx.get("hash", "")
        value = float(tx.get("value", 0))
        
        # Filter: We only care about small ETH funding (0.001 to 0.5 ETH)
        # Large transfers are usually exchange withdrawals, not burner funding
        if not (0.001 <= value <= 0.5) or not burner:
            continue

        if is_already_alerted(conn, burner):
            continue

        # 🚨 PRE-LAUNCH DETECTED
        record_alert(conn, burner, funder_address.lower(), tx_hash, tx_time.isoformat())
        
        msg = (
            f"🚨 <b>PRE-LAUNCH SYNDICATE ALERT</b> 🚨\n"
            f"⚠️ <b>Imminent Base Token Launch Detected!</b>\n\n"
            f"🕸️ <b>Parent Funder:</b>\n<code>{funder_address}</code>\n\n"
            f"🔥 <b>New Burner Wallet:</b>\n<code>{burner}</code>\n\n"
            f"💸 <b>Funding Amount:</b> {value:.4f} ETH\n"
            f"⏱️ <b>Funded At:</b> {tx_time.strftime('%H:%M:%S UTC')}\n\n"
            f"👉 <b>ACTION:</b> Paste the Burner Wallet into your terminal/deployer tracker. "
            f"The contract deployment transaction will originate from this address shortly."
        )
        send_telegram(msg)
        log(f"🚨 PRE-LAUNCH ALERT: Funder {funder_address[:6]}... funded {burner[:6]}...")
        alerts_fired += 1
        time.sleep(1) # Telegram rate limit pacing

    return alerts_fired

# ---------------------------------------------------------------------------
# 🚀 MAIN
# ---------------------------------------------------------------------------
def main():
    log("🕵️‍♂️ Phase 4: Pre-Launch Syndicate Tracker starting...")
    if not ALCHEMY_API_KEY:
        log("❌ Missing ALCHEMY_API_KEY. Exiting.")
        return

    if not os.path.exists(DB_PATH):
        log("⚠️ insider_hunter.db not found. Run Phase 1 first.")
        return

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    funders = get_high_value_funders(conn)
    if not funders:
        log("🔍 No high-value syndicate funders found in DB yet. Keep harvesting.")
        conn.close()
        return

    log(f"🕸️ Staking out {len(funders)} known syndicate funders...")
    
    total_alerts = 0
    for funder in funders:
        alerts = analyze_funder(conn, funder)
        if alerts:
            total_alerts += alerts
        time.sleep(0.5) # Polite pacing for Alchemy Free Tier

    conn.close()
    log(f"✅ Phase 4 complete. Fired {total_alerts} pre-launch alert(s).")

if __name__ == "__main__":
    main()
