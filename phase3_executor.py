#!/usr/bin/env python3
"""
Solana Momentum Bot - Phase 3: The Executor (PAPER default, LIVE double-gated)

- Manages open positions: 50%@2x, 10%@3x, 10%@5x, 10%@10x, 20% moonbag
  with dynamic trailing stop (35% <$500k MC, 25% <$5M, 20% >$5M).
- Circuit breaker: liquidity collapse (-40% from entry) closes everything.
- PAPER mode simulates fills; LIVE mode uses Jupiter v6 + Jito bundles.
- LIVE requires PAPER_TRADING=false AND regime live_trading=1.

This file reads the notebook written by Phases 0-2.
"""

import os
import json
import time
import base64
import sqlite3
import logging
import random
import requests
from typing import Any, Dict, List, Optional

SOL_MINT = "So11111111111111111111111111111111111111112"

CONFIG = {
    "GECKO_POOL_URL": "https://api.geckoterminal.com/api/v2/networks/solana/pools",
    "JUP_QUOTE_URL": "https://quote-api.jup.ag/v6/quote",
    "JUP_SWAP_URL": "https://quote-api.jup.ag/v6/swap",
    "JITO_BUNDLE_URL": "https://mainnet.block-engine.jito.wtf/api/v1/bundles",
    "SOLANA_RPC_URL": os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),

    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", ""),

    # Safety gates
    "PAPER_TRADING": os.getenv("PAPER_TRADING", "true").strip().lower() in {"1", "true", "yes", "on"},
    "POSITION_SIZE_SOL": float(os.getenv("POSITION_SIZE_SOL", "0.05")),
    "SLIPPAGE_BPS": int(os.getenv("SLIPPAGE_BPS", "500")),
    "JITO_TIP_LAMPORTS": int(os.getenv("JITO_TIP_LAMPORTS", "100000")),
    "MAX_OPEN_POSITIONS": int(os.getenv("MAX_OPEN_POSITIONS", "3")),
    "LIQUIDITY_COLLAPSE_PCT": float(os.getenv("LIQUIDITY_COLLAPSE_PCT", "40.0")),

    "HTTP_SLEEP_SECONDS": float(os.getenv("HTTP_SLEEP_SECONDS", "1.0")),
    "RPC_SLEEP_SECONDS": float(os.getenv("RPC_SLEEP_SECONDS", "0.4")),

    "DB_PATH": os.getenv("DB_PATH", "solana_momentum_phase1.db"),
}

FALLBACK_LADDER = [
    {"sell_pct_of_initial_position": 50.0, "multiple": 2.0},
    {"sell_pct_of_initial_position": 10.0, "multiple": 3.0},
    {"sell_pct_of_initial_position": 10.0, "multiple": 5.0},
    {"sell_pct_of_initial_position": 10.0, "multiple": 10.0},
]
FALLBACK_TRAIL = [
    {"peak_market_cap_less_than_usd": 500_000, "trailing_pct": 35.0},
    {"peak_market_cap_less_than_usd": 5_000_000, "trailing_pct": 25.0},
    {"peak_market_cap_less_than_usd": None, "trailing_pct": 20.0},
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


# ==============================================================================
# HELPERS
# ==============================================================================

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


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


def load_exit_plan() -> Dict[str, Any]:
    raw = get_metadata("exit_plan")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"take_profits": FALLBACK_LADDER, "moonbag_trailing_stop": FALLBACK_TRAIL}


def load_regime_context() -> Dict[str, Any]:
    regime = get_metadata("current_regime") or "COLD"
    live_raw = get_metadata("regime_live_trading")
    return {
        "regime": regime,
        "live_trading": (live_raw == "1") if live_raw is not None else False,
        "size_multiplier": safe_float(get_metadata("position_size_multiplier"), 0.0),
    }


def trail_pct_for_mc(mc: float, trail_table: List[Dict[str, Any]]) -> float:
    for row in trail_table:
        cap = row.get("peak_market_cap_less_than_usd")
        if cap is None or mc < float(cap):
            return safe_float(row.get("trailing_pct"), 20.0)
    return 20.0


# ==============================================================================
# DATABASE
# ==============================================================================

def init_db() -> None:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pool_address TEXT NOT NULL,
            base_mint TEXT NOT NULL,
            mode TEXT NOT NULL,
            initial_sol REAL NOT NULL,
            token_amount REAL NOT NULL,
            entry_price_native REAL NOT NULL,
            entry_liquidity_usd REAL NOT NULL,
            entry_epoch INTEGER NOT NULL,
            sold_pct REAL DEFAULT 0.0,
            targets_hit TEXT DEFAULT '',
            peak_price_native REAL NOT NULL,
            exit_sol REAL DEFAULT 0.0,
            status TEXT DEFAULT 'open',
            close_reason TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    logging.info("✅ Phase 3 schema verified.")


def get_open_positions() -> List[Dict[str, Any]]:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM positions WHERE status = 'open'")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def insert_position(pos: Dict[str, Any]) -> None:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO positions (pool_address, base_mint, mode, initial_sol, token_amount,
            entry_price_native, entry_liquidity_usd, entry_epoch, peak_price_native)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (pos["pool_address"], pos["base_mint"], pos["mode"], pos["initial_sol"],
         pos["token_amount"], pos["entry_price_native"], pos["entry_liquidity_usd"],
         pos["entry_epoch"], pos["entry_price_native"]),
    )
    conn.commit()
    conn.close()


def update_position(pos_id: int, fields: Dict[str, Any]) -> None:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    sets = ", ".join([f"{k} = ?" for k in fields.keys()])
    params = list(fields.values()) + [pos_id]
    cursor.execute(f"UPDATE positions SET {sets} WHERE id = ?", params)
    conn.commit()
    conn.close()


def mint_has_open_position(base_mint: str) -> bool:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM positions WHERE base_mint = ? AND status = 'open' LIMIT 1", (base_mint,))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def get_ready_candidates() -> List[Dict[str, Any]]:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM phase1_candidates
        WHERE phase2_status IN ('phase2_ready', 'phase2_paper')
        ORDER BY discovered_at_epoch DESC LIMIT 10
        """
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def mark_candidate_consumed(pool_address: str) -> None:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE phase1_candidates SET phase2_status = 'position_opened' WHERE pool_address = ?",
        (pool_address,),
    )
    conn.commit()
    conn.close()


# ==============================================================================
# MARKET DATA
# ==============================================================================

def get_pool_snapshot(pool_address: str) -> Optional[Dict[str, Any]]:
    url = f"{CONFIG['GECKO_POOL_URL']}/{pool_address}"
    headers = {"accept": "application/json", "user-agent": "SolanaMomentumPhase3/1.0"}
    try:
        time.sleep(CONFIG["HTTP_SLEEP_SECONDS"])
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return None
        attrs = resp.json().get("data", {}).get("attributes", {})
        return attrs if isinstance(attrs, dict) else None
    except Exception as e:
        logging.warning(f"Pool snapshot failed for {pool_address}: {e}")
        return None


# ==============================================================================
# TELEGRAM (HTML-safe)
# ==============================================================================

import html as _html

def send_telegram(message: str) -> bool:
    token = CONFIG["TELEGRAM_BOT_TOKEN"]
    chat_id = CONFIG["TELEGRAM_CHAT_ID"]
    if not token or not chat_id:
        logging.warning("⚠️ Telegram credentials missing.")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        time.sleep(0.5)
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logging.error(f"Telegram send failed: {e}")
        return False


def alert_entry(pos: Dict[str, Any]) -> None:
    tag = "📝 PAPER ENTRY" if pos["mode"] == "paper" else "🏆 LIVE ENTRY"
    send_telegram(
        f"💥 <b>{tag}</b>\n"
        f"🪙 <code>{_html.escape(pos['base_mint'][:8])}...{_html.escape(pos['base_mint'][-8:])}</code>\n"
        f"💰 Size: {pos['initial_sol']:.3f} SOL\n"
        f"🎯 Ladder: 50%@2x | 10%@3x | 10%@5x | 10%@10x | 20% moonbag"
    )


def alert_sell(pos: Dict[str, Any], label: str, pct: float, multiple: float, realized: float) -> None:
    tag = "📝 PAPER" if pos["mode"] == "paper" else "🏆 LIVE"
    send_telegram(
        f"💸 <b>{tag} {label}</b>\n"
        f"🪙 <code>{_html.escape(pos['base_mint'][:8])}...{_html.escape(pos['base_mint'][-8:])}</code>\n"
        f"📐 Sold {pct:.0f}% at {multiple:.1f}x\n"
        f"🧾 Realized: {realized:.4f} SOL (total {pos['exit_sol']:.4f})"
    )


def alert_close(pos: Dict[str, Any], reason: str, pnl: float) -> None:
    tag = "📝 PAPER" if pos["mode"] == "paper" else "🏆 LIVE"
    emoji = "🟢" if pnl >= 0 else "🔴"
    send_telegram(
        f"🏁 <b>{tag} POSITION CLOSED</b> ({_html.escape(reason)})\n"
        f"🪙 <code>{_html.escape(pos['base_mint'][:8])}...{_html.escape(pos['base_mint'][-8:])}</code>\n"
        f"{emoji} PnL: {pnl:+.4f} SOL on {pos['initial_sol']:.3f} SOL"
    )


# ==============================================================================
# LIVE EXECUTION (Jupiter v6 + Jito) — lazy imports, paper mode never touches this
# ==============================================================================

def rpc_request(method: str, params: List[Any]) -> Optional[Any]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        time.sleep(CONFIG["RPC_SLEEP_SECONDS"])
        resp = requests.post(CONFIG["SOLANA_RPC_URL"], json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            logging.warning(f"RPC error: {data['error'].get('message')}")
            return None
        return data.get("result")
    except Exception as e:
        logging.warning(f"RPC failed: {e}")
        return None


def get_keypair():
    secret = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    if not secret:
        logging.warning("⚠️ SOLANA_PRIVATE_KEY missing.")
        return None
    try:
        from solders.keypair import Keypair
        return Keypair.from_base58(secret)
    except Exception as e:
        logging.error(f"Keypair load failed: {e}")
        return None


def jupiter_quote(input_mint: str, output_mint: str, amount_units: int) -> Optional[Dict[str, Any]]:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_units),
        "slippageBps": str(CONFIG["SLIPPAGE_BPS"]),
    }
    try:
        time.sleep(CONFIG["HTTP_SLEEP_SECONDS"])
        resp = requests.get(CONFIG["JUP_QUOTE_URL"], params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logging.warning(f"Jupiter quote failed: {e}")
        return None


def live_swap(input_mint: str, output_mint: str, amount_units: int) -> Optional[Dict[str, Any]]:
    kp = get_keypair()
    if kp is None:
        return None
    quote = jupiter_quote(input_mint, output_mint, amount_units)
    if not quote:
        return None
    try:
        time.sleep(CONFIG["HTTP_SLEEP_SECONDS"])
        resp = requests.post(CONFIG["JUP_SWAP_URL"], json={
            "quoteResponse": quote,
            "userPublicKey": str(kp.pubkey()),
            "wrapAndUnwrapSol": True,
        }, timeout=20)
        resp.raise_for_status()
        swap_b64 = resp.json().get("swapTransaction")
        if not swap_b64:
            return None

        from solders.transaction import VersionedTransaction
        tx = VersionedTransaction.from_bytes(base64.b64decode(swap_b64))
        sig = kp.sign_message(bytes(tx.message))
        signed = VersionedTransaction.populate(tx.message, [sig])
        signed_b64 = base64.b64encode(bytes(signed)).decode()

        # Jito bundle with tip (fetch tip accounts at runtime — never hardcoded)
        tip_accounts = None
        try:
            tip_resp = requests.post(CONFIG["JITO_BUNDLE_URL"], json={
                "jsonrpc": "2.0", "id": 1, "method": "getTipAccounts", "params": []
            }, timeout=10)
            tip_accounts = tip_resp.json().get("result")
        except Exception:
            tip_accounts = None

        if isinstance(tip_accounts, list) and tip_accounts:
            from solders.system_program import transfer, TransferParams
            from solders.pubkey import Pubkey
            from solders.transaction import Transaction
            from solders.hash import Hash

            bh = rpc_request("getLatestBlockhash", [])
            blockhash_str = (bh or {}).get("value", {}).get("blockhash")
            if blockhash_str:
                tip_ix = transfer(TransferParams(
                    from_pubkey=kp.pubkey(),
                    to_pubkey=Pubkey.from_string(random.choice(tip_accounts)),
                    lamports=CONFIG["JITO_TIP_LAMPORTS"],
                ))
                tip_tx = Transaction.new_with_payer([tip_ix], kp.pubkey())
                tip_tx.sign([kp], Hash.from_string(blockhash_str))
                tip_b64 = base64.b64encode(bytes(tip_tx)).decode()

                bundle_resp = requests.post(CONFIG["JITO_BUNDLE_URL"], json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "sendBundle", "params": [[signed_b64, tip_b64]]
                }, timeout=15)
                if bundle_resp.status_code == 200:
                    return {"signature": "jito_bundle", "out_amount": int(quote.get("outAmount", 0))}

        # Fallback: plain RPC send
        send = rpc_request("sendTransaction", [signed_b64, {"skipPreflight": True, "encoding": "base64"}])
        if send:
            return {"signature": send, "out_amount": int(quote.get("outAmount", 0))}
        return None
    except Exception as e:
        logging.error(f"Live swap failed: {e}")
        return None


# ==============================================================================
# POSITION MANAGEMENT
# ==============================================================================

def sell_slice(pos: Dict[str, Any], pct: float, multiple: float, label: str) -> float:
    """Sells pct% of the INITIAL token amount. Returns SOL realized."""
    if pos["mode"] == "paper":
        realized = pos["initial_sol"] * (pct / 100.0) * multiple
    else:
        units = int(pos["token_amount"] * (pct / 100.0))
        result = live_swap(pos["base_mint"], SOL_MINT, units)
        if not result:
            return 0.0
        realized = result["out_amount"] / 1e9
    return realized


def manage_positions(plan: Dict[str, Any]) -> None:
    ladder = plan.get("take_profits", FALLBACK_LADDER)
    trail_table = plan.get("moonbag_trailing_stop", FALLBACK_TRAIL)

    for pos in get_open_positions():
        snap = get_pool_snapshot(pos["pool_address"])
        if not snap:
            continue

        price = safe_float(snap.get("base_token_price_native_currency"), 0.0)
        liq = safe_float(snap.get("reserve_in_usd"), 0.0)
        mc = safe_float(snap.get("market_cap_usd"), 0.0) or safe_float(snap.get("fdv_usd"), 0.0)
        if price <= 0 or pos["entry_price_native"] <= 0:
            continue

        multiple = price / pos["entry_price_native"]
        peak = max(safe_float(pos["peak_price_native"], 0.0), price)
        hits = set([h for h in (pos["targets_hit"] or "").split(",") if h])
        realized_total = safe_float(pos["exit_sol"], 0.0)
        changed = False

        # Circuit breaker: liquidity collapse
        if pos["entry_liquidity_usd"] > 0 and liq < pos["entry_liquidity_usd"] * (1 - CONFIG["LIQUIDITY_COLLAPSE_PCT"] / 100.0):
            rem = 100.0 - pos["sold_pct"]
            if rem > 0:
                realized = sell_slice(pos, rem, multiple, "circuit_breaker")
                realized_total += realized
                pos["sold_pct"] = 100.0
                pos["exit_sol"] = realized_total
                pos["status"] = "closed"
                pos["close_reason"] = "liquidity_collapse"
                update_position(pos["id"], {"sold_pct": 100.0, "exit_sol": realized_total,
                                            "status": "closed", "close_reason": "liquidity_collapse",
                                            "peak_price_native": peak})
                alert_close(pos, "liquidity collapse", realized_total - pos["initial_sol"])
            continue

        # Take-profit ladder
        for t in ladder:
            key = str(t["multiple"])
            if key in hits:
                continue
            if multiple >= float(t["multiple"]):
                pct = float(t["sell_pct_of_initial_position"])
                realized = sell_slice(pos, pct, multiple, f"tp_{key}x")
                if realized > 0 or pos["mode"] == "paper":
                    hits.add(key)
                    realized_total += realized
                    pos["sold_pct"] = safe_float(pos["sold_pct"], 0.0) + pct
                    pos["exit_sol"] = realized_total
                    changed = True
                    alert_sell(pos, f"TP {key}x", pct, float(t["multiple"]), realized)

        # Moonbag trailing stop
        if pos["sold_pct"] < 100.0:
            trail = trail_pct_for_mc(mc, trail_table)
            peak_multiple = peak / pos["entry_price_native"]
            if peak_multiple >= 1.5 and price <= peak * (1 - trail / 100.0):
                rem = 100.0 - pos["sold_pct"]
                realized = sell_slice(pos, rem, multiple, "trailing_stop")
                realized_total += realized
                pos["sold_pct"] = 100.0
                pos["exit_sol"] = realized_total
                pos["status"] = "closed"
                pos["close_reason"] = f"trailing_stop_{trail:.0f}pct"
                changed = True
                update_position(pos["id"], {"sold_pct": 100.0, "exit_sol": realized_total,
                                            "status": "closed",
                                            "close_reason": pos["close_reason"],
                                            "peak_price_native": peak,
                                            "targets_hit": ",".join(sorted(hits))})
                alert_close(pos, f"trailing stop (-{trail:.0f}% from peak)",
                            realized_total - pos["initial_sol"])
                continue

        if changed or peak != safe_float(pos["peak_price_native"], 0.0):
            update_position(pos["id"], {"peak_price_native": peak,
                                        "targets_hit": ",".join(sorted(hits)),
                                        "sold_pct": pos["sold_pct"],
                                        "exit_sol": realized_total})


# ==============================================================================
# ENTRIES
# ==============================================================================

def open_entries(ctx: Dict[str, Any]) -> None:
    open_count = len(get_open_positions())
    if open_count >= CONFIG["MAX_OPEN_POSITIONS"]:
        return

    entry_max_age = safe_float(get_metadata("entry_max_age_minutes"), 20.0)
    now = time.time()

    for cand in get_ready_candidates():
        if open_count >= CONFIG["MAX_OPEN_POSITIONS"]:
            break

        age_minutes = (now - safe_float(cand["discovered_at_epoch"], now)) / 60.0
        if age_minutes > entry_max_age:
            continue
        if mint_has_open_position(cand["base_mint"]):
            continue

        snap = get_pool_snapshot(cand["pool_address"])
        if not snap:
            continue
        price = safe_float(snap.get("base_token_price_native_currency"), 0.0)
        liq = safe_float(snap.get("reserve_in_usd"), 0.0)
        if price <= 0:
            continue

        # Mode decision: paper by default; live only if both gates open
        live_allowed = (not CONFIG["PAPER_TRADING"]) and ctx["live_trading"]
        mode = "live" if live_allowed else "paper"

        if mode == "live":
            size_sol = CONFIG["POSITION_SIZE_SOL"] * max(ctx["size_multiplier"], 0.0)
            if size_sol <= 0:
                continue
            units = int(size_sol * 1e9)
            result = live_swap(SOL_MINT, cand["base_mint"], units)
            if not result:
                continue
            token_amount = result["out_amount"] / 1e6  # approx; memecoins commonly 6 dp
        else:
            size_sol = CONFIG["POSITION_SIZE_SOL"]
            token_amount = size_sol / price

        pos = {
            "pool_address": cand["pool_address"],
            "base_mint": cand["base_mint"],
            "mode": mode,
            "initial_sol": size_sol,
            "token_amount": token_amount,
            "entry_price_native": price,
            "entry_liquidity_usd": liq,
            "entry_epoch": int(now),
            "peak_price_native": price,
        }
        insert_position(pos)
        mark_candidate_consumed(cand["pool_address"])
        alert_entry(pos)
        open_count += 1
        logging.info(f"✅ {mode.upper()} ENTRY | {cand['base_mint'][:8]} | {size_sol} SOL @ {price}")


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    logging.info(f"🚀 Starting Phase 3 Executor | PAPER={CONFIG['PAPER_TRADING']}")
    init_db()

    ctx = load_regime_context()
    plan = load_exit_plan()
    logging.info(f"🟦 Regime: {ctx['regime']} | live_trading={ctx['live_trading']}")

    if not CONFIG["PAPER_TRADING"] and not ctx["live_trading"]:
        logging.info("🛡️ LIVE requested but regime is COLD → standing down (capital protected).")

    manage_positions(plan)
    open_entries(ctx)

    logging.info("✅ Phase 3 Run Complete.")


if __name__ == "__main__":
    main()
