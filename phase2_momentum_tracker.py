#!/usr/bin/env python3
"""
Solana Momentum Bot - Phase 2: Momentum Tracking & Telegram Alerting (The Referee)
"""

import os
import json
import time
import sqlite3
import logging
import requests
from typing import Any, Dict, List, Optional

# ==============================================================================
# CONSTANTS
# ==============================================================================

CONFIG = {
    "GECKO_POOL_URL": "https://api.geckoterminal.com/api/v2/networks/solana/pools",
    "SOLANA_RPC_URL": os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
    
    # Telegram Config
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", ""),
    
    # Momentum Filters
    "MIN_VOLUME_TO_LIQ_RATIO": float(os.getenv("MIN_VOLUME_TO_LIQ_RATIO", "1.5")), 
    "MIN_WHALE_DILUTION_PCT": float(os.getenv("MIN_WHALE_DILUTION_PCT", "5.0")),   
    
    # Rate Limit Protection
    "HTTP_SLEEP_SECONDS": float(os.getenv("HTTP_SLEEP_SECONDS", "1.5")),
    "RPC_SLEEP_SECONDS": float(os.getenv("RPC_SLEEP_SECONDS", "0.5")),
    "MAX_CANDIDATES_PER_RUN": int(os.getenv("MAX_CANDIDATES_PER_RUN", "20")),
    
    "DB_PATH": os.getenv("DB_PATH", "solana_momentum_phase1.db"),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ==============================================================================
# TELEGRAM ALERTING
# ==============================================================================

def send_telegram_alert(pool_address: str, base_mint: str, volume: float, dilution: float) -> None:
    """Sends a formatted alert when a coin passes the Referee."""
    if not CONFIG["TELEGRAM_BOT_TOKEN"] or not CONFIG["TELEGRAM_CHAT_ID"]:
        logging.warning("Telegram credentials missing. Skipping alert.")
        return

    token = CONFIG["TELEGRAM_BOT_TOKEN"]
    chat_id = CONFIG["TELEGRAM_CHAT_ID"]
    
    # Solscan and Birdeye links for quick manual verification
    solscan_link = f"https://solscan.io/token/{base_mint}"
    birdeye_link = f"https://birdeye.so/token/{base_mint}?chain=solana"
    gecko_link = f"https://www.geckoterminal.com/solana/pools/{pool_address}"

    message = (
        f"🚨 *SOLANA MOMENTUM ALERT* 🚨\n\n"
        f"🏆 *Status:* PHASE 2 READY (Referee Passed)\n"
        f"💧 *Pool:* `{pool_address[:8]}...{pool_address[-8:]}`\n"
        f"📊 *1H Volume:* ${volume:,.0f}\n"
        f"📉 *Whale Dilution:* {dilution:.2f}% (Organic Buying Confirmed)\n\n"
        f"🔗 [Birdeye Chart]({birdeye_link})\n"
        f"🔗 [Solscan]({solscan_link})\n"
        f"🔗 [GeckoTerminal]({gecko_link})\n\n"
        f"_Review manually. Phase 3 Auto-Execution pending._"
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }

    try:
        time.sleep(0.5) # Respect Telegram rate limits
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logging.info("✅ Telegram alert sent successfully.")
        else:
            logging.warning(f"Telegram API error: {resp.text}")
    except Exception as e:
        logging.error(f"Failed to send Telegram alert: {e}")

# ==============================================================================
# DATABASE HELPERS (Unchanged from previous logic)
# ==============================================================================

def init_phase2_db() -> None:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE phase1_candidates ADD COLUMN phase2_status TEXT DEFAULT 'pending'")
        conn.commit()
    except sqlite3.OperationalError:
        pass 

    for col in ["current_volume_usd", "current_top10_pct", "whale_dilution_pct", "phase2_reject_reason"]:
        try:
            col_type = "REAL" if "pct" in col or "volume" in col else "TEXT"
            cursor.execute(f"ALTER TABLE phase1_candidates ADD COLUMN {col} {col_type}")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    conn.close()

def get_pending_candidates() -> List[Dict[str, Any]]:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT value FROM strategy_metadata WHERE key = 'entry_max_age_minutes' LIMIT 1")
    row = cursor.fetchone()
    max_age = float(row[0]) if row else 20.0
    
    cursor.execute("""
        SELECT * FROM phase1_candidates 
        WHERE phase2_status = 'pending' 
        ORDER BY discovered_at_epoch DESC 
        LIMIT ?
    """, (CONFIG["MAX_CANDIDATES_PER_RUN"],))
    
    rows = cursor.fetchall()
    conn.close()
    
    candidates = []
    now = time.time()
    
    for r in rows:
        age_minutes = (now - r["discovered_at_epoch"]) / 60.0
        if age_minutes <= max_age:
            candidates.append(dict(r))
        else:
            update_status(r["pool_address"], "stale_reject", "pool_too_old_for_phase2")
            
    return candidates

def update_status(pool_address: str, status: str, reason: str = "", extra_data: Dict = None) -> None:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    updates = ["phase2_status = ?"]
    params = [status]
    if reason:
        updates.append("phase2_reject_reason = ?")
        params.append(reason)
    if extra_data:
        for k, v in extra_data.items():
            updates.append(f"{k} = ?")
            params.append(v)
    params.append(pool_address)
    query = f"UPDATE phase1_candidates SET {', '.join(updates)} WHERE pool_address = ?"
    cursor.execute(query, params)
    conn.commit()
    conn.close()

# ==============================================================================
# API & RPC HELPERS
# ==============================================================================

def get_gecko_pool_data(pool_address: str) -> Optional[Dict[str, Any]]:
    url = f"{CONFIG['GECKO_POOL_URL']}/{pool_address}"
    headers = {"accept": "application/json", "user-agent": "SolanaMomentumPhase2/1.0"}
    try:
        time.sleep(CONFIG["HTTP_SLEEP_SECONDS"])
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 404: return None
        resp.raise_for_status()
        return resp.json().get("data", {}).get("attributes", {})
    except Exception as e:
        logging.warning(f"Gecko API failed for {pool_address}: {e}")
        return None

def get_current_top10_pct(mint_address: str, supply_raw: int) -> Optional[float]:
    if supply_raw <= 0: return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [mint_address]}
    try:
        time.sleep(CONFIG["RPC_SLEEP_SECONDS"])
        resp = requests.post(CONFIG["SOLANA_RPC_URL"], json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data: return None
        accounts = data.get("result", {}).get("value", [])
        if not accounts: return None
        top10_raw = sum(int(acc.get("amount", 0)) for acc in accounts[:10])
        return (top10_raw / supply_raw) * 100.0
    except Exception as e:
        logging.warning(f"RPC failed for {mint_address}: {e}")
        return None

# ==============================================================================
# MOMENTUM LOGIC
# ==============================================================================

def evaluate_candidate(c: Dict[str, Any]) -> None:
    pool = c["pool_address"]
    mint = c["base_mint"]
    initial_liq = c["liquidity_usd"]
    phase1_top10 = c["top10_gross_pct"]
    
    logging.info(f"🔍 Evaluating {pool}...")
    gecko_data = get_gecko_pool_data(pool)
    if not gecko_data:
        update_status(pool, "api_fail", "gecko_data_missing")
        return
        
    current_volume = float(gecko_data.get("volume_usd", {}).get("h1", 0) or 0)
    
    if initial_liq > 0 and (current_volume / initial_liq) < CONFIG["MIN_VOLUME_TO_LIQ_RATIO"]:
        reason = f"low_volume_velocity: {current_volume:.0f} vol / {initial_liq:.0f} liq"
        update_status(pool, "rejected", reason, {"current_volume_usd": current_volume})
        return

    current_top10 = get_current_top10_pct(mint, c["supply_raw"])
    if current_top10 is None or phase1_top10 is None:
        update_status(pool, "api_fail", "top10_calc_failed")
        return
        
    dilution = phase1_top10 - current_top10
    
    if dilution < CONFIG["MIN_WHALE_DILUTION_PCT"]:
        reason = f"whales_accumulating: dilution {dilution:.2f}%"
        update_status(pool, "rejected", reason, {"current_top10_pct": current_top10, "whale_dilution_pct": dilution, "current_volume_usd": current_volume})
        return

    # 🏆 PASSED PHASE 2!
    logging.info(f"🚀 PHASE 2 READY! {pool} | Vol: ${current_volume:,.0f} | Dilution: {dilution:.2f}%")
    update_status(pool, "phase2_ready", "momentum_confirmed", {
        "current_top10_pct": current_top10, "whale_dilution_pct": dilution, "current_volume_usd": current_volume
    })
    
    # 📱 SEND TELEGRAM ALERT
    send_telegram_alert(pool, mint, current_volume, dilution)

# ==============================================================================
# MAIN
# ==============================================================================

def main():
    logging.info("🚀 Starting Phase 2: Momentum Tracker & Alerter.")
    init_phase2_db()
    candidates = get_pending_candidates()
    logging.info(f"📋 Found {len(candidates)} fresh candidates to evaluate.")
    for c in candidates:
        try:
            evaluate_candidate(c)
        except Exception as e:
            logging.error(f"Critical error evaluating {c['pool_address']}: {e}")
    logging.info("✅ Phase 2 Run Complete.")

if __name__ == "__main__":
    main()
