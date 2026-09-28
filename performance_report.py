"""
📈 Paper-Trade Performance Reporter v5 (performance_report.py)
v5: size-weighted statistics (convergence gate), cancelled-entry bucket
(pending limits that never filled are NOT trades), prove_it exits priced
from stored exit_mcap.
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

TRANCHES = (("tp1", 0.3, 2.0), ("tp2", 0.2, 3.0), ("tp3", 0.2, 5.0), ("tp4", 0.1, 10.0))
TRAIL_KEEP = 0.75
HARD_KEEP = 0.75
MIN_SAMPLE = 20
GREEN_PF = 1.3


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
    except Exception as exc:
        log(f"⚠️ Telegram notify failed: {exc}")


def fetch_live_mcaps(addresses: list) -> dict:
    out = {}
    for i in range(0, len(addresses), 30):
        batch = addresses[i:i + 30]
        try:
            resp = requests.get(TOKENS_URL + ",".join(batch), timeout=15)
            if resp.status_code == 429:
                time.sleep(2)
                continue
            resp.raise_for_status()
            data = resp.json()
        except Exception:
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
    ret, used = 0.0, 0.0
    for name, weight, mult in TRANCHES:
        if int(flags.get(name, 0) or 0) == 1:
            ret += weight * (mult - 1.0) * 100.0
            used += weight
    remainder = max(0.0, 1.0 - used)
    ret += remainder * (exit_mult - 1.0) * 100.0
    return ret


def price_exit(row, entry: float, peak: float, live_map: dict):
    closed = int(col(row, "closed", 0) or 0)
    reason = str(col(row, "close_reason", "") or "")
    exit_mcap = float(col(row, "exit_mcap", 0) or 0)
    entry_status = str(col(row, "entry_status", "active") or "active")

    if reason == "entry_expired" or entry_status == "cancelled":
        return None, "cancelled"
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
    log("📈 Performance Reporter v5 starting...")
    if not os.path.exists(DB_PATH):
        log("❌ DB not found. Nothing to report.")
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not all(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
               for t in ("tp_state", "alerted_tokens")):
        log("❌ Required tables missing.")
        conn.close()
        return

    alerts = conn.execute("SELECT COUNT(*) AS c FROM alerted_tokens").fetchone()["c"] or 0
    rows = conn.execute("SELECT * FROM tp_state").fetchall()
    conn.close()

    open_addrs = [r["token_address"] for r in rows
                  if int(col(r, "closed", 0) or 0) == 0 and str(col(r, "entry_status", "active")) == "active"]
    live_map = fetch_live_mcaps(open_addrs) if open_addrs else {}

    priced, unpriced, opens, cancels, bad = [], [], [], 0, 0
    gated_full = gated_half = 0
    for r in rows:
        entry = float(col(r, "entry_mcap", 0) or 0)
        peak = float(col(r, "peak_mcap", 0) or 0)
        size = float(col(r, "size_mult", 1.0) or 1.0)
        flags = {name: col(r, name, 0) for name, _, _ in TRANCHES}
        mult, tag = price_exit(r, entry, peak, live_map)
        if tag == "cancelled":
            cancels += 1
            continue
        if entry <= 0 and tag != "open":
            bad += 1
            continue
        if size >= 1.0:
            gated_full += 1
        else:
            gated_half += 1
        item = {
            "symbol": col(r, "symbol", "?"),
            "reason": str(col(r, "close_reason", "") or ""),
            "ret": strategy_return_pct(mult, flags) if mult is not None else None,
            "size": size,
            "mfe": ((peak - entry) / entry) * 100.0 if (peak > 0 and entry > 0) else 0.0,
            "mult": mult,
        }
        if tag in ("exact", "reconstructed"):
            priced.append(item)
        elif tag == "open":
            opens.append(item)
        elif tag == "open_unpriced":
            opens.append({**item, "mult": None, "ret": None})
        else:
            unpriced.append(item)

    n = len(priced)
    scaled = [(p["ret"] or 0.0) * p["size"] for p in priced]
    wins = [s for s in scaled if s > 0]
    losses = [s for s in scaled if s <= 0]
    win_rate = (len(wins) / n * 100.0) if n else 0.0
    avg_win, avg_loss = mean(wins), mean(losses)
    expectancy = (win_rate / 100.0 * avg_win) + ((100.0 - win_rate) / 100.0 * avg_loss) if n else 0.0
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    rules = {}
    for p in priced:
        rules.setdefault(p["reason"] or "unknown", []).append((p["ret"] or 0.0) * p["size"])

    lines = ["📊 <b>PAPER-TRADE REPORT v5 (SIZE-WEIGHTED)</b>"]
    lines.append(f"🗂 Alerts: {alerts} | Tracked: {len(rows)} | Priced trades: {n} | Cancelled entries: {cancels}")
    lines.append(f"🧲 Convergence gate: {gated_full} full-size / {gated_half} half-size")
    lines.append(f"🎯 Win rate: <b>{win_rate:.1f}%</b> ({len(wins)}W / {len(losses)}L)")
    lines.append(f"📈 Avg win: <b>{avg_win:+.1f}%</b> | 📉 Avg loss: <b>{avg_loss:+.1f}%</b> (size-weighted)")
    lines.append(f"🧮 Expectancy / trade: <b>{expectancy:+.2f}%</b>")
    pf_txt = "∞" if pf == float("inf") else f"{pf:.2f}"
    lines.append(f"⚖️ Profit factor: <b>{pf_txt}</b>")
    lines.append(f"🏔 Avg MFE: <b>{mean([p['mfe'] for p in priced]):+.1f}%</b>")
    lines.append("")
    lines.append("🧰 <b>Rule breakdown</b> (size-weighted avg):")
    if rules:
        for key, vals in sorted(rules.items(), key=lambda kv: mean(kv[1]), reverse=True):
            lines.append(f"   • {key}: n={len(vals)} | avg {mean(vals):+.1f}%")
    else:
        lines.append("   • (no priced closes yet)")
    if unpriced:
        lines.append(f"⚠️ Unpriced legacy closes: {len(unpriced)}")
    if bad:
        lines.append(f"🗑 Bad entry rows: {bad}")
    if opens:
        live_open = [o for o in opens if o["mult"] is not None]
        lines.append(f"🔓 Open: {len(opens)}" + (f" | avg live {mean([o['mult'] for o in live_open]):.2f}x" if live_open else ""))
        for o in opens[:6]:
            lines.append(f"   • {o['symbol']}: " + (f"{o['mult']:.2f}x" if o["mult"] is not None else "n/a"))
    lines.append("")
    if n < MIN_SAMPLE:
        verdict = f"⏳ SAMPLE TOO SMALL ({n}/{MIN_SAMPLE}). Keep collecting."
    elif expectancy > 0 and (pf == float("inf") or pf >= GREEN_PF):
        verdict = "🟢 GREEN-LIGHT CANDIDATE. Review rule breakdown, then size small."
    elif expectancy > 0:
        verdict = "🟡 MARGINAL: positive expectancy, weak PF. Keep tuning."
    else:
        verdict = "🔴 NEGATIVE EXPECTANCY: do NOT go live."
    lines.append(f"<b>{verdict}</b>")

    report = "\n".join(lines)
    print("\n" + report.replace("<b>", "").replace("</b>", "") + "\n")
    send_telegram(report)
    log("✅ Report complete.")


if __name__ == "__main__":
    main()
