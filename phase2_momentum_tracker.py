#!/usr/bin/env python3
"""
Solana Momentum Bot - Phase 2: Momentum Tracking (The Referee) - REGIME-AWARE v2.2

v2.2 changes:
- Telegram messages now use HTML parse mode with escaped dynamic values.
  (Fixes 400 error: "can't parse entities" caused by underscores in DB reasons.)

v2.1 features (unchanged):
- Logs EVERY rejection / api_fail for full run transparency.
- Sends a once-per-day Telegram Referee Digest (proof-of-life in COLD regimes).
- Instant Telegram alerts on passes (LIVE or PAPER).
- Reads Phase 0 adaptive parameters from the notebook.

This module does NOT execute trades.
"""

import os
import html
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

    # Telegram
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", ""),

    # Fallback defaults if Phase 0 metadata is missing (capital-protective)
    "DEFAULT_VOLUME_RATIO": 1.5,
    "DEFAULT_DILUTION_PCT": 5.0,

    # Rate limit protection
    "HTTP_SLEEP_SECONDS": float(os.getenv("HTTP_SLEEP_SECONDS", "1.5")),
    "RPC_SLEEP_SECONDS": float(os.getenv("RPC_SLEEP_SECONDS", "0.5")),
    "MAX_CANDIDATES_PER_RUN": int(os.getenv("MAX_CANDIDATES_PER_RUN", "20")),

    # Daily digest cadence
    "DIGEST_INTERVAL_SECONDS": float(os.getenv("DIGEST_INTERVAL_SECONDS", "86400")),

    "DB_PATH": os.getenv("DB_PATH", "solana_momentum_phase1.db"),
}

REGIME_EMOJI = {"HOT": "🟥", "WARM": "🟨", "COLD": "🟦"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


# ==============================================================================
# SAFE HELPERS
# ==============================================================================

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def reason_prefix(reason: str) -> str:
    if not reason:
        return "unknown"
    if ":" in reason:
        return reason.split(":", 1)[0].strip()
    return reason.strip()


# ==============================================================================
# DATABASE
# ==============================================================================

PHASE2_COLUMNS = {
    "phase2_status": "TEXT DEFAULT 'pending'",
    "current_volume_usd": "REAL",
    "current_market_cap_usd": "REAL",
    "current_top10_pct": "REAL",
    "whale_dilution_pct": "REAL",
    "phase2_reject_reason": "TEXT",
}


def init_phase2_db() -> None:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    for col, col_type in PHASE2_COLUMNS.items():
        try:
            cursor.execute(f"ALTER TABLE phase1_candidates ADD COLUMN {col} {col_type}")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    conn.close()
    logging.info("✅ Phase 2 schema verified.")


def get_metadata(key: str) -> Optional[str]:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM strategy_metadata WHERE key = ? LIMIT 1", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def set_metadata(key: str, value: str) -> None:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO strategy_metadata (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def load_regime_context() -> Dict[str, Any]:
    regime = get_metadata("current_regime") or "COLD"
    live_raw = get_metadata("regime_live_trading")
    live = (live_raw == "1") if live_raw is not None else False

    vol_raw = get_metadata("required_volume_ratio")
    volume_ratio = safe_float(vol_raw, CONFIG["DEFAULT_VOLUME_RATIO"]) if vol_raw else CONFIG["DEFAULT_VOLUME_RATIO"]

    dil_raw = get_metadata("required_dilution_pct")
    dilution_pct = safe_float(dil_raw, CONFIG["DEFAULT_DILUTION_PCT"]) if dil_raw else CONFIG["DEFAULT_DILUTION_PCT"]

    size_mult = safe_float(get_metadata("position_size_multiplier"), 0.0)

    return {
        "regime": regime,
        "emoji": REGIME_EMOJI.get(regime, "🟦"),
        "live_trading": live,
        "volume_ratio": volume_ratio,
        "dilution_pct": dilution_pct,
        "size_multiplier": size_mult,
    }


def get_pending_candidates() -> List[Dict[str, Any]]:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT value FROM strategy_metadata WHERE key = 'entry_max_age_minutes' LIMIT 1")
    row = cursor.fetchone()
    max_age = safe_float(row[0], 20.0) if row else 20.0

    cursor.execute("""
        SELECT * FROM phase1_candidates
        WHERE phase2_status IS NULL OR phase2_status = 'pending'
        ORDER BY discovered_at_epoch DESC
        LIMIT ?
    """, (CONFIG["MAX_CANDIDATES_PER_RUN"],))
    rows = cursor.fetchall()
    conn.close()

    candidates = []
    now = time.time()
    for r in rows:
        age_minutes = (now - safe_float(r["discovered_at_epoch"], now)) / 60.0
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
    cursor.execute(f"UPDATE phase1_candidates SET {', '.join(updates)} WHERE pool_address = ?", params)
    conn.commit()
    conn.close()


# ==============================================================================
# API & RPC
# ==============================================================================

def get_gecko_pool_data(pool_address: str) -> Optional[Dict[str, Any]]:
    url = f"{CONFIG['GECKO_POOL_URL']}/{pool_address}"
    headers = {"accept": "application/json", "user-agent": "SolanaMomentumPhase2/2.2"}
    try:
        time.sleep(CONFIG["HTTP_SLEEP_SECONDS"])
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", {}).get("attributes", {})
    except Exception as e:
        logging.warning(f"Gecko API failed for {pool_address}: {e}")
        return None


def get_current_top10_pct(mint_address: str, supply_raw: int) -> Optional[float]:
    if supply_raw <= 0:
        return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [mint_address]}
    try:
        time.sleep(CONFIG["RPC_SLEEP_SECONDS"])
        resp = requests.post(CONFIG["SOLANA_RPC_URL"], json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            return None
        accounts = data.get("result", {}).get("value", [])
        if not accounts:
            return None
        top10_raw = sum(int(acc.get("amount", 0)) for acc in accounts[:10] if isinstance(acc, dict))
        if top10_raw <= 0:
            return None
        return (top10_raw / supply_raw) * 100.0
    except Exception as e:
        logging.warning(f"RPC failed for {mint_address}: {e}")
        return None


# ==============================================================================
# TELEGRAM (HTML-SAFE v2.2)
# ==============================================================================

def send_telegram(message: str) -> bool:
    token = CONFIG["TELEGRAM_BOT_TOKEN"]
    chat_id = CONFIG["TELEGRAM_CHAT_ID"]
    if not token or not chat_id:
        logging.warning("⚠️ Telegram credentials missing (check workflow secrets). Skipping message.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",   # HTML mode: underscores/symbols in data can't break parsing
        "disable_web_page_preview": True,
    }
    try:
        time.sleep(0.5)
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        logging.warning(f"Telegram API error: {resp.text}")
        return False
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")
        return False


def send_momentum_alert(pool: str, mint: str, volume: float, dilution: float,
                        current_mc: float, ctx: Dict[str, Any], live: bool) -> None:
    status_line = "🏆 <b>PHASE 2 READY (LIVE)</b>" if live else "📝 <b>PHASE 2 READY (PAPER TRADE ONLY)</b>"
    note = ("Phase 3 will auto-execute when the regime allows live trading."
            if not live else "Phase 3 may auto-execute this setup.")

    birdeye = f"https://birdeye.so/token/{mint}?chain=solana"
    solscan = f"https://solscan.io/token/{mint}"
    gecko = f"https://www.geckoterminal.com/solana/pools/{pool}"

    message = (
        f"🚨 <b>SOLANA MOMENTUM ALERT</b>\n\n"
        f"{status_line}\n"
        f"{ctx['emoji']} <b>Regime:</b> {html.escape(ctx['regime'])} | "
        f"Size mult: {ctx['size_multiplier']:.1f}x\n"
        f"💧 <b>Pool:</b> <code>{html.escape(pool[:8])}...{html.escape(pool[-8:])}</code>\n"
        f"📊 <b>1H Volume:</b> ${volume:,.0f}\n"
        f"📉 <b>Whale Dilution:</b> {dilution:.2f}% (organic buying)\n"
        f"💰 <b>Live MC:</b> ${current_mc:,.0f}\n\n"
        f'🔗 <a href="{birdeye}">Birdeye</a>\n'
        f'🔗 <a href="{solscan}">Solscan</a>\n'
        f'🔗 <a href="{gecko}">GeckoTerminal</a>\n\n'
        f"<i>{html.escape(note)}</i>"
    )
    if send_telegram(message):
        logging.info("✅ Telegram momentum alert sent.")


def send_daily_digest(counters: Dict[str, int], reasons: Dict[str, int], ctx: Dict[str, Any]) -> None:
    top = sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)[:3]
    reason_lines = "\n".join([f"• {html.escape(name)}: {n}" for name, n in top]) or "• none"

    message = (
        f"📋 <b>REFEREE DAILY DIGEST</b>\n\n"
        f"{ctx['emoji']} <b>Regime:</b> {html.escape(ctx['regime'])} | "
        f"live={str(ctx['live_trading'])}\n"
        f"🔎 Evaluated: {counters['evaluated']} | ✅ Passed: {counters['passed']} | "
        f"❌ Rejected: {counters['rejected']}\n\n"
        f"<b>Top rejection reasons:</b>\n{reason_lines}\n\n"
        f"<i>Instant alerts fire on any pass. Next digest in 24h.</i>"
    )

    if send_telegram(message):
        logging.info("✅ Telegram daily digest sent.")
        set_metadata("last_phase2_digest_epoch", str(int(time.time())))


# ==============================================================================
# MOMENTUM LOGIC
# ==============================================================================

def evaluate_candidate(c: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    pool = c["pool_address"]
    mint = c["base_mint"]
    initial_liq = safe_float(c["liquidity_usd"], 0.0)
    phase1_top10 = c["top10_gross_pct"]

    logging.info(f"🔍 Evaluating {pool} under regime {ctx['emoji']} {ctx['regime']}...")

    gecko_data = get_gecko_pool_data(pool)
    if not gecko_data:
        logging.info(f"❌ API FAIL {pool}: gecko_data_missing")
        update_status(pool, "api_fail", "gecko_data_missing")
        return "api_fail"

    volume_obj = gecko_data.get("volume_usd", {})
    if not isinstance(volume_obj, dict):
        volume_obj = {}
    current_volume = safe_float(volume_obj.get("h1"), 0.0)

    current_mc = safe_float(gecko_data.get("market_cap_usd"), 0.0)
    if current_mc <= 0:
        current_mc = safe_float(gecko_data.get("fdv_usd"), 0.0)

    # Filter 1: Volume Velocity (regime-adaptive)
    ratio = (current_volume / initial_liq) if initial_liq > 0 else 0.0
    if ratio < ctx["volume_ratio"]:
        reason = f"low_volume_velocity: {current_volume:.0f} vol / {initial_liq:.0f} liq = {ratio:.2f}x < {ctx['volume_ratio']:.1f}x"
        logging.info(f"❌ REJECT {pool}: {reason}")
        update_status(pool, "rejected", reason, {
            "current_volume_usd": current_volume,
            "current_market_cap_usd": current_mc,
        })
        return "rejected"

    # Filter 2: Whale Dilution Trick (regime-adaptive)
    supply_raw = int(safe_float(c["supply_raw"], 0))
    current_top10 = get_current_top10_pct(mint, supply_raw)
    if current_top10 is None or phase1_top10 is None:
        logging.info(f"❌ API FAIL {pool}: top10_calc_failed")
        update_status(pool, "api_fail", "top10_calc_failed")
        return "api_fail"

    dilution = safe_float(phase1_top10, 0.0) - current_top10
    if dilution < ctx["dilution_pct"]:
        reason = f"insufficient_dilution: {dilution:.2f}% < {ctx['dilution_pct']:.1f}%"
        logging.info(f"❌ REJECT {pool}: {reason}")
        update_status(pool, "rejected", reason, {
            "current_top10_pct": current_top10,
            "whale_dilution_pct": dilution,
            "current_volume_usd": current_volume,
            "current_market_cap_usd": current_mc,
        })
        return "rejected"

    # 🏆 PASSED
    live = bool(ctx["live_trading"])
    status = "phase2_ready" if live else "phase2_paper"
    logging.info(f"🚀 PHASE 2 PASSED ({status})! {pool} | Vol: ${current_volume:,.0f} | Dilution: {dilution:.2f}%")
    update_status(pool, status, "momentum_confirmed", {
        "current_top10_pct": current_top10,
        "whale_dilution_pct": dilution,
        "current_volume_usd": current_volume,
        "current_market_cap_usd": current_mc,
    })
    send_momentum_alert(pool, mint, current_volume, dilution, current_mc, ctx, live)
    return "passed"


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    logging.info("🚀 Starting Phase 2 v2.2: Regime-Aware Momentum Tracker (HTML-safe Telegram).")
    init_phase2_db()

    ctx = load_regime_context()
    logging.info(f"{ctx['emoji']} Regime context: {ctx['regime']} | live={ctx['live_trading']} | "
                 f"vol_ratio={ctx['volume_ratio']:.1f}x | dilution={ctx['dilution_pct']:.1f}% | "
                 f"size={ctx['size_multiplier']:.1f}x")

    candidates = get_pending_candidates()
    logging.info(f"📋 Found {len(candidates)} fresh candidate(s) to evaluate.")

    counters = {"evaluated": 0, "passed": 0, "rejected": 0, "api_fail": 0}
    reasons: Dict[str, int] = {}

    for c in candidates:
        try:
            counters["evaluated"] += 1
            result = evaluate_candidate(c, ctx)
            if result == "passed":
                counters["passed"] += 1
            elif result == "rejected":
                counters["rejected"] += 1
            else:
                counters["api_fail"] += 1
        except Exception as e:
            logging.error(f"Critical error evaluating {c['pool_address']}: {e}")

    # Re-read latest rejection reasons from DB for an accurate digest
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    cursor.execute("""
        SELECT phase2_reject_reason FROM phase1_candidates
        WHERE phase2_status = 'rejected' AND phase2_reject_reason IS NOT NULL
    """)
    for (raw,) in cursor.fetchall():
        key = reason_prefix(raw or "")
        reasons[key] = reasons.get(key, 0) + 1
    conn.close()

    # Daily digest (proof-of-life) — retries automatically until it succeeds
    last_raw = get_metadata("last_phase2_digest_epoch")
    last = safe_float(last_raw, 0.0)
    if (time.time() - last) >= CONFIG["DIGEST_INTERVAL_SECONDS"]:
        send_daily_digest(counters, reasons, ctx)

    logging.info(f"✅ Phase 2 Run Complete | evaluated={counters['evaluated']} "
                 f"passed={counters['passed']} rejected={counters['rejected']} api_fail={counters['api_fail']}")


if __name__ == "__main__":
    main()
