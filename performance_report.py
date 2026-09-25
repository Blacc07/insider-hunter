"""
📈 Paper-Trade Performance Reporter (performance_report.py)
Reads solana_breakout.db and computes the statistics that decide whether
the breakout strategy goes live:

  - Win rate, avg win / avg loss (ladder-weighted strategy returns)
  - Expectancy per trade = (Win% x AvgWin) + (Loss% x AvgLoss[negative])
  - Profit factor = gross profits / gross losses
  - MFE (best peak) and rule-by-rule breakdown
  - Live mark-to-market for open positions

Exit pricing (documented, never guessed):
  exact        -> tp_tracker stored exit_mcap > 0
  reconstructed-> legacy rows: trailing = peak x 0.70, hard stop = 0.60x,
                   liquidity gone = 0
  unpriced     -> legacy time_decay / max_age closes (reported separately)
Run via Actions -> performance-report -> Run workflow (read-only).
"""

import os
import sqlite3
import time
import requests
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "solana_breakout.db")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

# Ladder tranches: (flag name, fraction of initial size, exit multiple)
TRANCHES = (("tp1", 0.5, 2.0), ("tp2", 0.1, 3.0), ("tp3", 0.1, 5.0), ("tp4", 0.1, 10.0))
TRAIL_KEEP = 0.70   # trailing stop fires at peak x 0.70
HARD_KEEP = 0.60    # hard stop fires at entry x 0.60
MIN_SAMPLE = 20     # trades needed before a GREEN LIGHT verdict
GREEN_PF = 1.3      # minimum profit factor for a green light


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def col(row, name, default):
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def mean(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return (sum(values) / len(values)) if values else 0.0


def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except requests.exceptions.RequestException as exc:
        log(f"⚠️ Telegram notify failed (non-fatal): {exc}")


def fetch_live_mcaps(addresses: list) -> dict:
    out = {}
    for i in range(0, len(addresses), 30):
        batch = addresses[i:i + 30]
        try:
            resp = requests.get(TOKENS_URL + ",".join(batch), timeout=15)
            if resp.status_code == 429:
                log("⚠️ DexScreener 429 during live valuation.")
                time.sleep(2)
                continue
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log(f"⚠️ Live valuation fetch failed: {exc}")
            time.sleep(1)
            continue
        pairs = data.get("pairs") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not isinstance(pairs, list):
            continue
        for p in pairs:
            if not isinstance(p, dict):
                continue
            token = (p.get("baseToken") or {}).get("address") or ""
            mcap = p.get("marketCap") or p.get("fdv") or 0
            try:
                mcap = float(mcap)
            except (TypeError, ValueError):
                continue
            if token and mcap > 0 and (token not in out or mcap > out[token]):
                out[token] = mcap
        time.sleep(1)
    return out


def strategy_return_pct(exit_mult, flags: dict) -> float:
    """Ladder-weighted return: locked tranches at their multiples,
    remainder at the final exit multiple."""
    ret = 0.0
    used = 0.0
    for name, weight, mult in TRANCHES:
        if int(flags.get(name, 0) or 0) == 1:
            ret += weight * (mult - 1.0) * 100.0
            used += weight
    remainder = max(0.0, 1.0 - used)
    ret += remainder * (exit_mult - 1.0) * 100.0
    return ret


def price_exit(row, entry: float, peak: float, live_map: dict):
    """Returns (exit_multiple, pricing_tag) or (None, tag)."""
    closed = int(col(row, "closed", 0) or 0)
    reason = str(col(row, "close_reason", "") or "")
    exit_mcap = float(col(row, "exit_mcap", 0) or 0)

    if closed == 0:
        live = live_map.get(row["token_address"])
        if live is None or live <= 0 or entry <= 0:
            return None, "open_unpriced"
        return live / entry, "open"

    if exit_mcap > 0 and entry > 0:
        return exit_mcap / entry, "exact"
    if reason == "trailing_stop" and peak > 0 and entry > 0:
        return (peak * TRAIL_KEEP) / entry, "reconstructed"
    if reason == "hard_stop":
        return HARD_KEEP, "reconstructed"
    if reason == "liquidity_gone":
        return 0.0, "reconstructed"
    return None, "unpriced_legacy"


def main() -> None:
    log("📈 Performance Reporter starting...")
    if not os.path.exists(DB_PATH):
        log("❌ DB not found. Nothing to report.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not all(
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
        for t in ("tp_state", "alerted_tokens")
    ):
        log("❌ Required tables missing. Nothing to report.")
        conn.close()
        return

    alerts = conn.execute("SELECT COUNT(*) AS c FROM alerted_tokens").fetchone()["c"] or 0
    rows = conn.execute("SELECT * FROM tp_state").fetchall()
    conn.close()

    open_addrs = [r["token_address"] for r in rows if int(col(r, "closed", 0) or 0) == 0]
    live_map = fetch_live_mcaps(open_addrs) if open_addrs else {}

    priced, unpriced, opens, bad = [], [], [], 0
    for r in rows:
        entry = float(col(r, "entry_mcap", 0) or 0)
        peak = float(col(r, "peak_mcap", 0) or 0)
        flags = {name: col(r, name, 0) for name, _, _ in TRANCHES}
        if entry <= 0:
            bad += 1
            continue
        mult, tag = price_exit(r, entry, peak, live_map)
        item = {
            "symbol": col(r, "symbol", "?"),
            "reason": str(col(r, "close_reason", "") or ""),
            "ret": strategy_return_pct(mult, flags) if mult is not None else None,
            "mfe": ((peak - entry) / entry) * 100.0 if peak > 0 else 0.0,
            "mult": mult,
            "flags": flags,
        }
        if tag in ("exact", "reconstructed"):
            item["pricing"] = tag
            priced.append(item)
        elif tag == "open":
            opens.append(item)
        elif tag == "open_unpriced":
            opens.append({**item, "mult": None, "ret": None})
        else:
            unpriced.append(item)

    n = len(priced)
    wins = [p for p in priced if (p["ret"] or 0) > 0]
    losses = [p for p in priced if (p["ret"] or 0) <= 0]
    win_rate = (len(wins) / n * 100.0) if n else 0.0
    avg_win = mean([p["ret"] for p in wins])
    avg_loss = mean([p["ret"] for p in losses])
    expectancy = (win_rate / 100.0 * avg_win) + ((100.0 - win_rate) / 100.0 * avg_loss) if n else 0.0
    gross_win = sum(p["ret"] for p in wins)
    gross_loss = abs(sum(p["ret"] for p in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0

    rules = {}
    for p in priced:
        key = p["reason"] or "unknown"
        rules.setdefault(key, []).append(p["ret"] or 0.0)

    lines = []
    lines.append("📊 <b>PAPER-TRADE PERFORMANCE REPORT</b>")
    lines.append(f"🗂 Alerts: {alerts} | Tracked: {len(rows)} | Priced trades: {n}")
    lines.append(f"🎯 Win rate: <b>{win_rate:.1f}%</b> ({len(wins)}W / {len(losses)}L)")
    lines.append(f"📈 Avg win: <b>{avg_win:+.1f}%</b> | 📉 Avg loss: <b>{avg_loss:+.1f}%</b>")
    lines.append(f"🧮 Expectancy / trade: <b>{expectancy:+.2f}%</b>")
    pf_txt = "∞" if pf == float("inf") else f"{pf:.2f}"
    lines.append(f"⚖️ Profit factor: <b>{pf_txt}</b>")
    lines.append(f"🏔 Avg MFE (peak): <b>{mean([p['mfe'] for p in priced]):+.1f}%</b>")
    lines.append("")
    lines.append("🧰 <b>Rule breakdown</b> (avg strategy return):")
    if rules:
        for key, vals in sorted(rules.items(), key=lambda kv: mean(kv[1]), reverse=True):
            lines.append(f"   • {key}: n={len(vals)} | avg {mean(vals):+.1f}%")
    else:
        lines.append("   • (no priced closes yet)")
    if unpriced:
        reasons = {}
        for u in unpriced:
            reasons[u["reason"] or "unknown"] = reasons.get(u["reason"] or "unknown", 0) + 1
        lines.append(f"⚠️ Unpriced legacy closes (excluded, not guessed): {len(unpriced)} {reasons}")
    if bad:
        lines.append(f"🗑 Rows with bad entry data: {bad}")
    if opens:
        live_open = [o for o in opens if o["mult"] is not None]
        lines.append(f"🔓 Open positions: {len(opens)}" +
                     (f" | avg live {mean([o['mult'] for o in live_open]):.2f}x" if live_open else ""))
        for o in opens[:6]:
            mtxt = f"{o['mult']:.2f}x" if o["mult"] is not None else "n/a"
            lines.append(f"   • {o['symbol']}: {mtxt}")
    lines.append("")
    if n < MIN_SAMPLE:
        verdict = f"⏳ SAMPLE TOO SMALL ({n}/{MIN_SAMPLE}). Keep collecting."
    elif expectancy > 0 and (pf == float("inf") or pf >= GREEN_PF):
        verdict = "🟢 GREEN-LIGHT CANDIDATE: positive expectancy & solid PF. Review rule breakdown, then size small."
    elif expectancy > 0:
        verdict = "🟡 MARGINAL: positive expectancy but weak profit factor. Tune filters before live size."
    else:
        verdict = "🔴 NEGATIVE EXPECTANCY: do NOT go live. Tune discovery filters / exit rules first."
    lines.append(f"<b>{verdict}</b>")

    report = "\n".join(lines)
    print("\n" + report.replace("<b>", "").replace("</b>", "") + "\n")
    send_telegram(report)
    log("✅ Report complete.")


if __name__ == "__main__":
    main()
