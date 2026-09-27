"""
🔮 Phase 5: Smart Money Convergence Tracker (accumulation_tracker.py)
Scans the live token balances of your top 50 highest-scoring insider wallets.
Detects "Stealth Accumulation" - when multiple elite wallets quietly hold 
the same low-cap token before the public pump happens.
"""

import os
import sqlite3
import time
import requests
from datetime import datetime, timezone
from collections import defaultdict

# ---------------------------------------------------------------------------
# ⚙️ CONFIGURATION
# ---------------------------------------------------------------------------
ALCHEMY_API_KEY = os.environ.get("ALCHEMY_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DB_PATH = os.environ.get("DB_PATH", "insider_hunter.db")

BASE_RPC_URL = f"https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"

# Only scan the absolute best wallets to save API calls and filter noise
MIN_WALLET_SCORE = 80
MAX_WALLETS_TO_SCAN = 50

# Require at least 2 elite wallets to hold the same token to trigger an alert
MIN_CONVERGENCE_COUNT = 2 

# Ignore tokens with massive supplies, stablecoins, and blue-chip memes
IGNORE_SYMBOLS = {
    "USDC", "USDT", "DAI", "WETH", "ETH", "WBTC", "CBETH", "AERO",
    # Blue-chip memes (Too large for a quick 10x micro-cap play)
    "PEPE", "WIF", "BONK", "FLOKI", "SHIB", "DOGE", "BRETT", "MOG", "POPCAT", "TURBO"
}

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
        CREATE TABLE IF NOT EXISTS convergence_alerts (
            token_address TEXT PRIMARY KEY,
            alerted_at TEXT,
            wallet_count INTEGER
        )
    """)
    conn.commit()

def get_elite_wallets(conn: sqlite3.Connection) -> list:
    """Gets the top N unique wallets with the highest insider scores."""
    cur = conn.execute("""
        SELECT wallet, MAX(score) as max_score 
        FROM buyers 
        WHERE score >= ? AND wallet IS NOT NULL
        GROUP BY wallet
        ORDER BY max_score DESC
        LIMIT ?
    """, (MIN_WALLET_SCORE, MAX_WALLETS_TO_SCAN))
    return [row[0] for row in cur.fetchall()]

def is_already_alerted(conn: sqlite3.Connection, token_address: str) -> bool:
    cur = conn.execute("SELECT 1 FROM convergence_alerts WHERE token_address = ?", (token_address.lower(),))
    return cur.fetchone() is not None

def record_alert(conn: sqlite3.Connection, token_address: str, count: int):
    conn.execute(
        "INSERT OR IGNORE INTO convergence_alerts (token_address, alerted_at, wallet_count) VALUES (?, ?, ?)",
        (token_address.lower(), datetime.now(timezone.utc).isoformat(), count)
    )
    conn.commit()

# ---------------------------------------------------------------------------
# 🌐 ALCHEMY API (Base Chain)
# ---------------------------------------------------------------------------
def get_wallet_balances(wallet_address: str) -> list:
    """Fetches all ERC20 token balances for a specific wallet."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "alchemy_getTokenBalances",
        "params": [wallet_address, "erc20"]
    }
    try:
        resp = requests.post(BASE_RPC_URL, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", {}).get("tokenBalances", [])
    except Exception as e:
        log(f"⚠️ Alchemy fetch failed for {wallet_address[:6]}...: {e}")
        return []

def get_token_metadata(token_address: str) -> dict:
    """Fetches token name and symbol to filter out spam."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "alchemy_getTokenMetadata",
        "params": [token_address]
    }
    try:
        resp = requests.post(BASE_RPC_URL, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", {})
    except Exception:
        return {}

# ---------------------------------------------------------------------------
# 🧠 THE CONVERGENCE LOGIC
# ---------------------------------------------------------------------------
def main():
    log("🔮 Phase 5: Smart Money Convergence Tracker starting...")
    if not ALCHEMY_API_KEY:
        log("❌ Missing ALCHEMY_API_KEY. Exiting.")
        return

    if not os.path.exists(DB_PATH):
        log("⚠️ insider_hunter.db not found. Run Phase 1 first.")
        return

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    elite_wallets = get_elite_wallets(conn)
    if not elite_wallets:
        log("🔍 No elite wallets (score >= 80) found in DB yet. Keep harvesting.")
        conn.close()
        return

    log(f"🕵️‍♂️ Scanning token holdings of {len(elite_wallets)} elite insider wallets...")
    
    # Map: Token Address -> List of Wallets holding it
    token_holders = defaultdict(list)
    
    for wallet in elite_wallets:
        balances = get_wallet_balances(wallet)
        
        for bal in balances:
            token_addr = bal.get("contractAddress", "").lower()
            # Filter out zero balances and spam
            if not token_addr or bal.get("tokenBalance") == "0x0" or bal.get("tokenBalance") == "0":
                continue
            
            token_holders[token_addr].append(wallet)
        
        # Guardrail 3: Respect Alchemy Free Tier rate limits
        time.sleep(0.25) 

    log(f"📊 Mapped holdings across {len(token_holders)} unique tokens.")

    alerts_fired = 0
    for token_addr, holders in token_holders.items():
        if len(holders) < MIN_CONVERGENCE_COUNT:
            continue
            
        if is_already_alerted(conn, token_addr):
            continue

        # Fetch metadata to ensure it's a real token and not a stablecoin
        meta = get_token_metadata(token_addr)
        symbol = meta.get("symbol", "UNKNOWN").upper()
        name = meta.get("name", "Unknown Token")
        
        if symbol in IGNORE_SYMBOLS or not symbol:
            continue

        # 🚨 CONVERGENCE DETECTED
        record_alert(conn, token_addr, len(holders))
        
        # Format the list of elite wallets holding it for the alert
        holder_tags = "\n".join([f"• <code>{w[:6]}...{w[-4:]}</code>" for w in holders[:5]])
        
        msg = (
            f"🔮 <b>PRE-PUMP SMART MONEY CONVERGENCE</b> 🔮\n"
            f"⚠️ <b>Multiple Elite Insiders are holding the same token!</b>\n\n"
            f"🪙 <b>Token:</b> {name} (${symbol})\n"
            f"📋 <b>Contract:</b>\n<code>{token_addr}</code>\n\n"
            f"🕵️‍♂️ <b>Elite Wallets Holding ({len(holders)}):</b>\n{holder_tags}\n\n"
            f"👉 <b>THESIS:</b> Insiders are quietly positioned. Check DexScreener for liquidity and volume anomalies before entering."
        )
        send_telegram(msg)
        log(f"🔮 CONVERGENCE ALERT: {symbol} held by {len(holders)} elite wallets.")
        alerts_fired += 1
        time.sleep(1) # Telegram pacing

    conn.close()
    log(f"✅ Phase 5 complete. Fired {alerts_fired} convergence alert(s).")

if __name__ == "__main__":
    main()
