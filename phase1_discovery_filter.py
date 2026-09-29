#!/usr/bin/env python3
"""
Solana Momentum Bot - Phase 1 + Phase 0 (Regime Engine)

Phase 0 (Regime Engine):
- Computes the median initial liquidity of every scanned batch.
- Classifies the market regime: HOT / WARM / COLD.
- Adapts liquidity floor, safety caps, and position size multiplier.
- Sends a Telegram Regime Report on regime change or once per day.
- Stores adaptive parameters in the notebook for Phase 2 / Phase 3.

Phase 1 (Discovery & Filtering):
- Discovers new Solana pools.
- Applies regime-adaptive hard safety and liquidity filters.
- Stores valid candidates for Phase 2.

This module does NOT execute trades.
"""

import os
import json
import time
import base64
import sqlite3
import logging
import argparse
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ==============================================================================
# CONSTANTS
# ==============================================================================

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Regime band boundaries (based on smoothed median new-pool liquidity)
HOT_MIN_MEDIAN_LIQ = 30000.0
WARM_MIN_MEDIAN_LIQ = 10000.0

REGIME_LOOKBACK_RUNS = 6          # rolling window for stabilization
REGIME_REPORT_INTERVAL_SECONDS = 86400  # daily summary if no change


def str_to_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_allowed_quote_mints() -> List[str]:
    env_value = os.getenv("ALLOWED_QUOTE_MINTS", "").strip()
    if not env_value:
        return [SOL_MINT, USDC_MINT]
    return [m.strip() for m in env_value.split(",") if m.strip()]


CONFIG = {
    "GECKO_NEW_POOLS_URL": "https://api.geckoterminal.com/api/v2/networks/solana/new_pools",
    "SOLANA_RPC_URL": os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),

    # Base (HOT) filters
    "MAX_POOL_AGE_MINUTES": float(os.getenv("MAX_POOL_AGE_MINUTES", "180")),
    "HARD_MAX_POOL_AGE_MINUTES": float(os.getenv("HARD_MAX_POOL_AGE_MINUTES", "360")),
    "OVERLAP_BUFFER_MINUTES": float(os.getenv("OVERLAP_BUFFER_MINUTES", "5")),
    "ENTRY_MAX_AGE_MINUTES": float(os.getenv("ENTRY_MAX_AGE_MINUTES", "20")),
    "ALLOWED_QUOTE_MINTS": parse_allowed_quote_mints(),
    "ALLOW_TOKEN_2022": str_to_bool(os.getenv("ALLOW_TOKEN_2022", "false"), False),

    # Rate limit + runtime protection
    "MAX_PAGES": int(os.getenv("MAX_PAGES", "2")),
    "MAX_POOLS_PER_RUN": int(os.getenv("MAX_POOLS_PER_RUN", "80")),
    "HTTP_SLEEP_SECONDS": float(os.getenv("HTTP_SLEEP_SECONDS", "1.0")),
    "RPC_SLEEP_SECONDS": float(os.getenv("RPC_SLEEP_SECONDS", "0.25")),
    "ITEM_SLEEP_SECONDS": float(os.getenv("ITEM_SLEEP_SECONDS", "0.25")),

    # Telegram
    "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", ""),

    "DB_PATH": os.getenv("DB_PATH", "solana_momentum_phase1.db"),
}


EXIT_PLAN = {
    "strategy_name": "solana_momentum_scalper_moonbag",
    "entry_model": "microstructure_momentum_no_insider_hunting",
    "take_profits": [
        {"sell_pct_of_initial_position": 50.0, "multiple": 2.0},
        {"sell_pct_of_initial_position": 10.0, "multiple": 3.0},
        {"sell_pct_of_initial_position": 10.0, "multiple": 5.0},
        {"sell_pct_of_initial_position": 10.0, "multiple": 10.0},
    ],
    "moonbag_pct_of_initial_position": 20.0,
    "moonbag_trailing_stop": [
        {"peak_market_cap_less_than_usd": 500_000, "trailing_pct": 35.0},
        {"peak_market_cap_less_than_usd": 5_000_000, "trailing_pct": 25.0},
        {"peak_market_cap_less_than_usd": None, "trailing_pct": 20.0},
    ],
    "notes": [
        "50% is sold at 2x to secure principal and initial profit.",
        "Final 20% is the moonbag with dynamic trailing stop.",
        "Phase 3 MUST re-check pool age against entry_max_age_minutes.",
        "Phase 3 MUST multiply position size by position_size_multiplier.",
        "Phase 3 MUST NOT open live trades when regime_live_trading = 0.",
    ],
}


# ==============================================================================
# PHASE 0: REGIME DEFINITIONS
# ==============================================================================

def regime_params_for(regime: str) -> Dict[str, Any]:
    """
    Anti-scam compensation principle:
    - When momentum thresholds relax (WARM), safety thresholds tighten.
    - COLD = stand-down / paper mode to protect capital.
    """
    if regime == "HOT":
        return {
            "name": "HOT",
            "emoji": "🟥",
            "liq_floor": 30000.0,
            "top10_gross_reject_pct": 95.0,
            "required_volume_ratio": 1.5,
            "required_dilution_pct": 5.0,
            "position_size_multiplier": 1.0,
            "live_trading": True,
            "description": "Healthy liquidity. Full rules, full size.",
        }
    if regime == "WARM":
        return {
            "name": "WARM",
            "emoji": "🟨",
            "liq_floor": 10000.0,
            "top10_gross_reject_pct": 90.0,   # tightened (anti-scam)
            "required_volume_ratio": 1.2,     # relaxed momentum
            "required_dilution_pct": 6.0,     # tightened (anti-scam)
            "position_size_multiplier": 0.5,
            "live_trading": True,
            "description": "Thinner liquidity. Smaller size, tighter safety.",
        }
    return {
        "name": "COLD",
        "emoji": "🟦",
        "liq_floor": 30000.0,
        "top10_gross_reject_pct": 95.0,
        "required_volume_ratio": 1.5,
        "required_dilution_pct": 5.0,
        "position_size_multiplier": 0.0,
        "live_trading": False,
        "description": "Starved liquidity. Stand-down / paper mode.",
    }


def classify_regime(smoothed_median: float) -> str:
    if smoothed_median >= HOT_MIN_MEDIAN_LIQ:
        return "HOT"
    if smoothed_median >= WARM_MIN_MEDIAN_LIQ:
        return "WARM"
    return "COLD"


def median_of(values: List[float]) -> Optional[float]:
    if not values:
        return None
    vals = sorted(values)
    n = len(vals)
    mid = n // 2
    if n % 2 == 1:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


# ==============================================================================
# LOGGING
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# ==============================================================================
# DATABASE
# ==============================================================================

def init_db() -> None:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()

    cursor.executescript(
        """
        CREATE TABLE IF NOT EXISTS strategy_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS seen_pools (
            pool_address TEXT PRIMARY KEY,
            base_mint TEXT,
            first_seen_epoch INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS phase1_candidates (
            pool_address TEXT PRIMARY KEY,
            base_mint TEXT NOT NULL,
            quote_mint TEXT,
            dex TEXT,
            pool_name TEXT,
            liquidity_usd REAL,
            market_cap REAL,
            fdv REAL,
            base_token_price_native_currency REAL,
            quote_token_price_native_currency REAL,
            created_at TEXT,
            age_minutes REAL,
            mint_owner TEXT,
            mint_authority_enabled INTEGER,
            freeze_authority_enabled INTEGER,
            supply_raw INTEGER,
            decimals INTEGER,
            top10_gross_pct REAL,
            regime_at_discovery TEXT,
            raw_json TEXT,
            discovered_at_epoch INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS phase1_rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pool_address TEXT,
            base_mint TEXT,
            reason TEXT NOT NULL,
            raw_json TEXT,
            created_epoch INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS regime_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_epoch INTEGER NOT NULL,
            pools_scanned INTEGER NOT NULL,
            median_liq REAL,
            smoothed_median REAL,
            regime TEXT NOT NULL,
            passed INTEGER NOT NULL,
            rejected INTEGER NOT NULL
        );
        """
    )

    # Safe schema migration for older notebooks
    try:
        cursor.execute(
            "ALTER TABLE phase1_candidates ADD COLUMN regime_at_discovery TEXT"
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()
    logging.info("✅ Database initialized (Phase 0 + Phase 1).")


def get_metadata(key: str) -> Optional[str]:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    cursor.execute(
        "SELECT value FROM strategy_metadata WHERE key = ? LIMIT 1",
        (key,)
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def set_metadata(key: str, value: str) -> None:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO strategy_metadata (key, value) VALUES (?, ?)",
        (key, value)
    )
    conn.commit()
    conn.close()


def store_exit_plan() -> None:
    set_metadata("exit_plan", json.dumps(EXIT_PLAN, indent=2))
    set_metadata("entry_max_age_minutes", str(CONFIG["ENTRY_MAX_AGE_MINUTES"]))
    logging.info("✅ Exit plan + entry freshness gate stored for Phase 3.")


def record_regime_history(run_epoch: int, scanned: int, median_liq: Optional[float],
                          smoothed: Optional[float], regime: str,
                          passed: int, rejected: int) -> None:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO regime_history
        (run_epoch, pools_scanned, median_liq, smoothed_median, regime, passed, rejected)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (run_epoch, scanned, median_liq, smoothed, regime, passed, rejected)
    )
    conn.commit()
    conn.close()


def get_recent_medians(limit: int = REGIME_LOOKBACK_RUNS) -> List[float]:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT median_liq FROM regime_history
        WHERE median_liq IS NOT NULL
        ORDER BY run_epoch DESC LIMIT ?
        """,
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [float(r[0]) for r in rows]


def is_seen(pool_address: str) -> bool:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM seen_pools WHERE pool_address = ? LIMIT 1",
        (pool_address,)
    )
    row = cursor.fetchone()
    conn.close()
    return row is not None


def mark_seen(pool_address: str, base_mint: str) -> None:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO seen_pools (pool_address, base_mint, first_seen_epoch)
        VALUES (?, ?, ?)
        """,
        (pool_address, base_mint, int(time.time()))
    )
    conn.commit()
    conn.close()


def insert_candidate(pool: Dict[str, Any]) -> None:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO phase1_candidates (
            pool_address, base_mint, quote_mint, dex, pool_name,
            liquidity_usd, market_cap, fdv,
            base_token_price_native_currency, quote_token_price_native_currency,
            created_at, age_minutes, mint_owner,
            mint_authority_enabled, freeze_authority_enabled,
            supply_raw, decimals, top10_gross_pct,
            regime_at_discovery, raw_json, discovered_at_epoch
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pool["pool_address"], pool["base_mint"], pool["quote_mint"],
            pool["dex"], pool["pool_name"], pool["liquidity_usd"],
            pool["market_cap"], pool["fdv"],
            pool["base_token_price_native_currency"],
            pool["quote_token_price_native_currency"],
            pool["created_at"], pool["age_minutes"], pool["mint_owner"],
            int(pool["mint_authority_enabled"]), int(pool["freeze_authority_enabled"]),
            pool["supply_raw"], pool["decimals"], pool["top10_gross_pct"],
            pool["regime_at_discovery"], json.dumps(pool["raw_json"]),
            int(time.time()),
        )
    )
    conn.commit()
    conn.close()


def insert_rejection(pool_address: str, base_mint: str, reason: str,
                     raw_json: Dict[str, Any]) -> None:
    conn = sqlite3.connect(CONFIG["DB_PATH"])
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO phase1_rejections
        (pool_address, base_mint, reason, raw_json, created_epoch)
        VALUES (?, ?, ?, ?, ?)
        """,
        (pool_address, base_mint, reason, json.dumps(raw_json), int(time.time()))
    )
    conn.commit()
    conn.close()


# ==============================================================================
# TELEGRAM (REGIME REPORT)
# ==============================================================================

def send_regime_report(params: Dict[str, Any], smoothed: float, scanned: int,
                       passed: int, rejected: int, changed: bool) -> None:
    token = CONFIG["TELEGRAM_BOT_TOKEN"]
    chat_id = CONFIG["TELEGRAM_CHAT_ID"]

    if not token or not chat_id:
        logging.warning("Telegram credentials missing. Skipping regime report.")
        return

    headline = "🔄 REGIME CHANGE" if changed else "📅 DAILY REGIME SUMMARY"

    message = (
        f"🌡️ *SOLANA MARKET REGIME REPORT*\n"
        f"{headline}\n\n"
        f"{params['emoji']} *Regime:* {params['name']}\n"
        f"💧 Smoothed median new-pool liquidity: ${smoothed:,.0f}\n"
        f"🔎 Pools scanned: {scanned} | ✅ Passed: {passed} | ❌ Rejected: {rejected}\n\n"
        f"⚙️ *Active rules:*\n"
        f"• Liquidity floor: ${params['liq_floor']:,.0f}\n"
        f"• Top-10 gross cap: {params['top10_gross_reject_pct']:.0f}%\n"
        f"• Required volume ratio: {params['required_volume_ratio']:.1f}x\n"
        f"• Required whale dilution: {params['required_dilution_pct']:.1f}%\n"
        f"• Position size multiplier: {params['position_size_multiplier']:.1f}x\n\n"
        f"📝 {params['description']}"
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        time.sleep(0.5)
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logging.info("✅ Telegram regime report sent.")
        else:
            logging.warning(f"Telegram API error: {resp.text}")
    except Exception as e:
        logging.error(f"Failed to send regime report: {e}")


# ==============================================================================
# CRON-DELAY TOLERANCE
# ==============================================================================

def compute_effective_max_age(run_start_epoch: int):
    base = CONFIG["MAX_POOL_AGE_MINUTES"]
    hard = CONFIG["HARD_MAX_POOL_AGE_MINUTES"]
    buffer = CONFIG["OVERLAP_BUFFER_MINUTES"]

    last_run_raw = get_metadata("last_run_epoch")
    gap_minutes = 0.0

    if last_run_raw:
        try:
            last_run_epoch = float(last_run_raw)
            gap_minutes = max(0.0, (run_start_epoch - last_run_epoch) / 60.0)
        except Exception:
            gap_minutes = 0.0
        effective = max(base, gap_minutes + buffer)
    else:
        effective = base

    return min(effective, hard), gap_minutes


# ==============================================================================
# SAFE PARSING HELPERS
# ==============================================================================

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def clean_gecko_id(raw_id: Any) -> str:
    if not raw_id:
        return ""
    raw_id = str(raw_id)
    if "_" in raw_id:
        return raw_id.split("_")[-1].strip()
    return raw_id.strip()


def extract_relationship_id(item: Dict[str, Any], relationship_key: str) -> str:
    relationships = item.get("relationships", {})
    if not isinstance(relationships, dict):
        return ""
    relationship = relationships.get(relationship_key, {})
    if not isinstance(relationship, dict):
        return ""
    data = relationship.get("data", {})
    if isinstance(data, list):
        if len(data) == 0:
            return ""
        data = data[0]
    if not isinstance(data, dict):
        return ""
    return clean_gecko_id(data.get("id", ""))


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def age_minutes_from_datetime(created_dt: Optional[datetime]) -> Optional[float]:
    if not created_dt:
        return None
    now = datetime.now(timezone.utc)
    if created_dt.tzinfo is None:
        created_dt = created_dt.replace(tzinfo=timezone.utc)
    return (now - created_dt).total_seconds() / 60.0


# ==============================================================================
# HTTP / RPC HELPERS
# ==============================================================================

def http_get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    headers = {"accept": "application/json", "user-agent": "SolanaMomentumPhase1/1.1"}
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            time.sleep(CONFIG["HTTP_SLEEP_SECONDS"])
            response = requests.get(url, params=params, headers=headers, timeout=20)
            if response.status_code == 429:
                wait = 10.0 * attempt
                logging.warning(f"HTTP 429 rate limit. Sleeping {wait:.2f}s.")
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logging.warning(f"HTTP GET failed attempt {attempt}/{max_retries}: {exc}")
            time.sleep(2.0 * attempt)
    return None


def rpc_request(method: str, params: List[Any]) -> Optional[Dict[str, Any]]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    headers = {"Content-Type": "application/json"}
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            time.sleep(CONFIG["RPC_SLEEP_SECONDS"])
            response = requests.post(CONFIG["SOLANA_RPC_URL"], json=payload,
                                     headers=headers, timeout=25)
            if response.status_code == 429:
                wait = 10.0 * attempt
                logging.warning(f"RPC 429 rate limit. Sleeping {wait:.2f}s.")
                time.sleep(wait)
                continue
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                error_obj = data.get("error", {})
                if error_obj.get("code") == 429:
                    time.sleep(10.0 * attempt)
                    continue
                logging.warning(f"RPC error: {error_obj.get('code')} | {error_obj.get('message')}")
                return None
            return data.get("result")
        except Exception as exc:
            logging.warning(f"RPC request failed attempt {attempt}/{max_retries}: {exc}")
            time.sleep(2.0 * attempt)
    return None


# ==============================================================================
# SOLANA MINT SAFETY CHECKS
# ==============================================================================

def parse_mint_account_base64(data_b64: str) -> Optional[Dict[str, Any]]:
    try:
        raw = base64.b64decode(data_b64)
        if len(raw) < 82:
            return None
        mint_authority_option = int.from_bytes(raw[0:4], byteorder="little")
        supply_raw = int.from_bytes(raw[36:44], byteorder="little")
        decimals = int(raw[44])
        is_initialized = int(raw[45]) == 1
        freeze_authority_option = int.from_bytes(raw[46:50], byteorder="little")
        return {
            "mint_authority_enabled": mint_authority_option != 0,
            "supply_raw": supply_raw,
            "decimals": decimals,
            "is_initialized": is_initialized,
            "freeze_authority_enabled": freeze_authority_option != 0,
        }
    except Exception as exc:
        logging.warning(f"Failed to parse mint account: {exc}")
        return None


def check_mint_safety(mint_address: str) -> Dict[str, Any]:
    result = rpc_request("getAccountInfo", [mint_address, {"encoding": "base64"}])
    if not isinstance(result, dict):
        return {"ok": False, "reason": "rpc_get_account_info_failed"}

    value = result.get("value")
    if not isinstance(value, dict):
        return {"ok": False, "reason": "mint_account_not_found"}

    owner = value.get("owner", "")
    lamports = safe_int(value.get("lamports"), 0)
    executable = bool(value.get("executable", False))

    if lamports <= 0:
        return {"ok": False, "reason": "mint_account_has_zero_lamports"}
    if executable:
        return {"ok": False, "reason": "mint_account_is_executable"}
    if owner == TOKEN_2022_PROGRAM_ID and not CONFIG["ALLOW_TOKEN_2022"]:
        return {"ok": False, "reason": "token_2022_not_allowed"}
    if owner not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}:
        return {"ok": False, "reason": "mint_owner_not_standard_spl_token_program"}

    data_field = value.get("data")
    data_b64 = None
    if isinstance(data_field, list) and len(data_field) > 0:
        data_b64 = data_field[0]
    elif isinstance(data_field, str):
        data_b64 = data_field
    if not data_b64:
        return {"ok": False, "reason": "mint_account_data_missing"}

    parsed = parse_mint_account_base64(data_b64)
    if not parsed:
        return {"ok": False, "reason": "mint_account_data_parse_failed"}
    if not parsed["is_initialized"]:
        return {"ok": False, "reason": "mint_not_initialized"}
    if parsed["mint_authority_enabled"]:
        return {"ok": False, "reason": "mint_authority_enabled"}
    if parsed["freeze_authority_enabled"]:
        return {"ok": False, "reason": "freeze_authority_enabled"}

    return {
        "ok": True,
        "reason": "mint_safe",
        "owner": owner,
        "mint_authority_enabled": parsed["mint_authority_enabled"],
        "freeze_authority_enabled": parsed["freeze_authority_enabled"],
        "supply_raw": parsed["supply_raw"],
        "decimals": parsed["decimals"],
    }


def get_top10_gross_holder_pct(mint_address: str, supply_raw: int) -> Optional[float]:
    if supply_raw <= 0:
        return None
    result = rpc_request("getTokenLargestAccounts", [mint_address])
    if not isinstance(result, dict):
        return None
    accounts = result.get("value", [])
    if not isinstance(accounts, list) or len(accounts) == 0:
        return None
    top10_raw = 0
    for account in accounts[:10]:
        if not isinstance(account, dict):
            continue
        amount_raw = safe_int(account.get("amount"), 0)
        if amount_raw > 0:
            top10_raw += amount_raw
    if top10_raw <= 0:
        return None
    return (top10_raw / supply_raw) * 100.0


# ==============================================================================
# DISCOVERY
# ==============================================================================

def fetch_new_pools(page: int) -> List[Dict[str, Any]]:
    data = http_get_json(CONFIG["GECKO_NEW_POOLS_URL"], params={"page": page})
    if not isinstance(data, dict):
        logging.warning(f"GeckoTerminal returned invalid data for page {page}.")
        return []
    pools = data.get("data", [])
    return pools if isinstance(pools, list) else []


def extract_batch_liquidities(items: List[Dict[str, Any]]) -> List[float]:
    liqs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes", {})
        if not isinstance(attrs, dict):
            continue
        liq = safe_float(attrs.get("reserve_in_usd"), 0.0)
        if liq > 0:
            liqs.append(liq)
    return liqs


# ==============================================================================
# PROCESSING (REGIME-ADAPTIVE)
# ==============================================================================

def process_pool_item(item: Dict[str, Any], max_age_minutes: float,
                      params: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return {"status": "rejected", "reason": "invalid_item"}

    attrs = item.get("attributes", {})
    if not isinstance(attrs, dict):
        attrs = {}

    pool_address = clean_gecko_id(attrs.get("address", ""))
    if not pool_address:
        pool_address = clean_gecko_id(item.get("id", ""))

    base_mint = extract_relationship_id(item, "base_token")
    quote_mint = extract_relationship_id(item, "quote_token")
    dex = extract_relationship_id(item, "dex")

    if not pool_address:
        insert_rejection(pool_address, base_mint, "missing_pool_address", item)
        return {"status": "rejected", "reason": "missing_pool_address"}

    if not base_mint:
        insert_rejection(pool_address, base_mint, "missing_base_mint", item)
        return {"status": "rejected", "reason": "missing_base_mint"}

    if not force and is_seen(pool_address):
        return {"status": "duplicate", "reason": "already_seen"}

    pool_name = attrs.get("name", "")
    liquidity_usd = safe_float(attrs.get("reserve_in_usd"), 0.0)
    market_cap = safe_float(attrs.get("market_cap"), 0.0)
    fdv = safe_float(attrs.get("fdv"), 0.0)
    base_token_price_native_currency = safe_float(attrs.get("base_token_price_native_currency"), 0.0)
    quote_token_price_native_currency = safe_float(attrs.get("quote_token_price_native_currency"), 0.0)

    created_at = attrs.get("pool_created_at", "")
    created_dt = parse_iso_datetime(created_at)
    age_minutes = age_minutes_from_datetime(created_dt)

    # Filter 1: Regime-adaptive liquidity floor
    if liquidity_usd < params["liq_floor"]:
        reason = f"liquidity_below_minimum: {liquidity_usd:.2f} < {params['liq_floor']:.0f}"
        insert_rejection(pool_address, base_mint, reason, item)
        mark_seen(pool_address, base_mint)
        return {"status": "rejected", "reason": reason}

    # Filter 2: Pool age vs effective (gap-aware) window
    if age_minutes is None:
        reason = "missing_pool_created_at"
        insert_rejection(pool_address, base_mint, reason, item)
        mark_seen(pool_address, base_mint)
        return {"status": "rejected", "reason": reason}
    if age_minutes > max_age_minutes:
        reason = f"pool_older_than_effective_window: {age_minutes:.2f}m > {max_age_minutes:.2f}m"
        insert_rejection(pool_address, base_mint, reason, item)
        mark_seen(pool_address, base_mint)
        return {"status": "rejected", "reason": reason}

    # Filter 3: Quote token
    if quote_mint not in CONFIG["ALLOWED_QUOTE_MINTS"]:
        reason = f"quote_mint_not_allowed: {quote_mint}"
        insert_rejection(pool_address, base_mint, reason, item)
        mark_seen(pool_address, base_mint)
        return {"status": "rejected", "reason": reason}

    # Filter 4: Mint safety (never adapts)
    mint_check = check_mint_safety(base_mint)
    if not mint_check.get("ok", False):
        reason = f"mint_safety_failed: {mint_check.get('reason', 'unknown')}"
        insert_rejection(pool_address, base_mint, reason, item)
        mark_seen(pool_address, base_mint)
        return {"status": "rejected", "reason": reason}

    # Filter 5: Regime-adaptive top-10 gross sanity guard
    supply_raw = safe_int(mint_check.get("supply_raw"), 0)
    top10_gross_pct = get_top10_gross_holder_pct(base_mint, supply_raw)
    if top10_gross_pct is not None:
        if top10_gross_pct > params["top10_gross_reject_pct"]:
            reason = f"top10_gross_holder_pct_too_high: {top10_gross_pct:.2f}%"
            insert_rejection(pool_address, base_mint, reason, item)
            mark_seen(pool_address, base_mint)
            return {"status": "rejected", "reason": reason}

    candidate = {
        "pool_address": pool_address,
        "base_mint": base_mint,
        "quote_mint": quote_mint,
        "dex": dex,
        "pool_name": pool_name,
        "liquidity_usd": liquidity_usd,
        "market_cap": market_cap,
        "fdv": fdv,
        "base_token_price_native_currency": base_token_price_native_currency,
        "quote_token_price_native_currency": quote_token_price_native_currency,
        "created_at": created_at,
        "age_minutes": age_minutes,
        "mint_owner": mint_check.get("owner", ""),
        "mint_authority_enabled": bool(mint_check.get("mint_authority_enabled", True)),
        "freeze_authority_enabled": bool(mint_check.get("freeze_authority_enabled", True)),
        "supply_raw": supply_raw,
        "decimals": safe_int(mint_check.get("decimals"), 0),
        "top10_gross_pct": top10_gross_pct,
        "regime_at_discovery": params["name"],
        "raw_json": item,
    }

    insert_candidate(candidate)
    mark_seen(pool_address, base_mint)

    logging.info(
        "✅ PASS | "
        f"pool={pool_address} | base={base_mint} | "
        f"liq=${liquidity_usd:,.2f} | age={age_minutes:.2f}m | "
        f"mc=${market_cap:,.2f} | regime={params['name']}"
    )
    return {"status": "passed", "reason": "phase1_passed"}


# ==============================================================================
# MAIN
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Solana Momentum Bot Phase 1 + Phase 0 Regime Engine"
    )
    parser.add_argument("--pages", type=int, default=CONFIG["MAX_PAGES"])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    run_start_epoch = int(time.time())

    init_db()
    store_exit_plan()

    effective_max_age, gap_minutes = compute_effective_max_age(run_start_epoch)

    logging.info("🚀 Starting Phase 1 + Phase 0 (Regime Engine).")
    logging.info(f"Gap since last run: {gap_minutes:.2f} minutes.")
    logging.info(f"Effective discovery window: {effective_max_age:.2f} minutes.")

    # --------------------------------------------------
    # PASS A: Fetch batch first so we can measure the market
    # --------------------------------------------------
    items: List[Dict[str, Any]] = []
    stop = False
    for page in range(1, args.pages + 1):
        logging.info(f"📄 Fetching new pools page {page}...")
        pools = fetch_new_pools(page)
        if not pools:
            break
        items.extend(pools)
        if len(items) >= CONFIG["MAX_POOLS_PER_RUN"]:
            items = items[:CONFIG["MAX_POOLS_PER_RUN"]]
            stop = True
            break
        time.sleep(CONFIG["HTTP_SLEEP_SECONDS"])
    if stop:
        logging.info(f"🛑 Capped at MAX_POOLS_PER_RUN ({CONFIG['MAX_POOLS_PER_RUN']}).")

    # --------------------------------------------------
    # PHASE 0: Measure temperature and classify regime
    # --------------------------------------------------
    batch_liqs = extract_batch_liquidities(items)
    median_liq = median_of(batch_liqs)

    prev_regime = get_metadata("current_regime")

    if median_liq is None:
        regime = prev_regime if prev_regime else "COLD"
        smoothed = None
        logging.warning("⚠️ No liquidity data this run. Keeping previous regime.")
    else:
        recent = get_recent_medians()
        smoothed = median_of(recent + [median_liq])
        regime = classify_regime(smoothed if smoothed is not None else median_liq)

    params = regime_params_for(regime)

    logging.info(f"🌡️ Batch median liquidity: {median_liq if median_liq is None else f'${median_liq:,.0f}'}")
    logging.info(f"🌡️ Smoothed median (last {REGIME_LOOKBACK_RUNS} runs): "
                 f"{'n/a' if smoothed is None else f'${smoothed:,.0f}'}")
    logging.info(f"{params['emoji']} REGIME: {params['name']} | {params['description']}")
    logging.info(f"⚙️ Liquidity floor: ${params['liq_floor']:,.0f} | "
                 f"Top-10 cap: {params['top10_gross_reject_pct']:.0f}% | "
                 f"Size multiplier: {params['position_size_multiplier']:.1f}x")

    # --------------------------------------------------
    # PASS B: Process with regime-adaptive thresholds
    # --------------------------------------------------
    passed = 0
    rejected = 0
    duplicates = 0
    errors = 0

    for item in items:
        try:
            result = process_pool_item(item, effective_max_age, params, force=args.force)
            status = result.get("status", "")
            if status == "passed":
                passed += 1
            elif status == "duplicate":
                duplicates += 1
            else:
                rejected += 1
        except Exception as exc:
            errors += 1
            logging.error(f"Unexpected error processing pool item: {exc}")
        time.sleep(CONFIG["ITEM_SLEEP_SECONDS"])

    # --------------------------------------------------
    # Persist regime state + run state
    # --------------------------------------------------
    record_regime_history(run_start_epoch, len(items), median_liq, smoothed,
                          regime, passed, rejected)

    set_metadata("current_regime", regime)
    set_metadata("position_size_multiplier", str(params["position_size_multiplier"]))
    set_metadata("required_volume_ratio", str(params["required_volume_ratio"]))
    set_metadata("required_dilution_pct", str(params["required_dilution_pct"]))
    set_metadata("regime_live_trading", "1" if params["live_trading"] else "0")
    set_metadata("last_run_epoch", str(run_start_epoch))
    set_metadata("last_effective_window_minutes", f"{effective_max_age:.2f}")

    # --------------------------------------------------
    # Telegram regime report (on change or daily)
    # --------------------------------------------------
    if smoothed is not None:
        last_report_raw = get_metadata("last_regime_report_epoch")
        last_report = float(last_report_raw) if last_report_raw else 0.0
        changed = (prev_regime != regime)
        due_daily = (run_start_epoch - last_report) >= REGIME_REPORT_INTERVAL_SECONDS

        if changed or due_daily:
            send_regime_report(params, smoothed, len(items), passed, rejected, changed)
            set_metadata("last_regime_report_epoch", str(run_start_epoch))

    logging.info("==============================================")
    logging.info("✅ Phase 1 + Phase 0 complete.")
    logging.info(f"Scanned: {len(items)} | Passed: {passed} | "
                 f"Rejected: {rejected} | Duplicates: {duplicates} | Errors: {errors}")
    logging.info(f"Regime: {params['emoji']} {params['name']}")
    logging.info("==============================================")


if __name__ == "__main__":
    main()
