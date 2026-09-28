"""
📈 Paper-Trade Performance Reporter (performance_report.py) — v7
Matches the restored ladder: 0.3@2x, 0.2@3x, 0.2@5x, 0.1@10x, remainder at exit.
Keeps MAE stop-width sensitivity table + SCALP_EPOCH cohort isolation.
"""

import os
import sqlite3
import time
import requests
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "solana_breakout.db")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SCALP_EPOCH = os.environ.get("SCALP_EPOCH", "")
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

TRANCHES = (("tp1", 0.3, 2.0), ("tp2", 0.2, 3.0), ("tp3", 0.2, 5.0), ("tp4", 0.1, 10.0))
TRAIL_KEEP = 0.75
HARD_KEEP = 0.80
MIN_SAMPLE = 30
GREEN_PF = 2.0
CANDIDATE_STOPS = (0.10, 0.15, 0.20, 0.25, 0.30)


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        return None


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
    log("📈 Performance Reporter v7 (ladder restored) starting...")
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

    epoch_dt = parse_iso(SCALP_EPOCH)
    alerts = conn.execute("SELECT COUNT(*) AS c FROM alerted_tokens").fetchone()["c"] or 0
    rows = conn.execute("SELECT * FROM tp_state").fetchall()
    conn.close()

    open_addrs = [r["token_address"] for r in rows if int(col(r, "closed", 0) or 0) == 0]
    live_map = fetch_live_mcaps(open_addrs) if open_addrs else {}

    priced, opens, legacy_excluded, unpriced = [], [], 0, 0
    for r in rows:
        started = parse_iso(col(r, "started_ts", ""))
        if epoch_dt is not None and (started is None or started < epoch_dt):
            legacy_excluded += 1
            continue
        entry = float(col(r, "entry_mcap", 0) or 0)
        peak = float(col(r, "peak_mcap", 0) or 0)
        flags = {name: col(r, name, 0) for name, _, _ in TRANCHES}
        if entry <= 0:
            continue
        mult, tag = price_exit(r, entry, peak, live_map)
        item = {
            "symbol": col(r, "symbol", "?"),
            "reason": str(col(r, "close_reason", "") or ""),
            "ret": strategy_return_pct(mult, flags) if mult is not None else None,
            "mfe": ((peak - entry) / entry) * 100.0 if peak > 0 else 0.0,
            "mult": mult,
            "entry": entry,
            "tp1_hit": int(col(r, "tp1", 0) or 0) == 1,
            "mae": float(col(r, "mae_mcap", 0) or 0),
        }
        if tag in ("exact", "reconstructed"):
            priced.append(item)
        elif tag in ("open", "open_unpriced"):
            opens.append(item)
        else:
            unpriced += 1

    n = len(priced)
    wins = [p["ret"] for p in priced if (p["ret"] or 0) > 0]
    losses = [p["ret"] for p in priced if (p["ret"] or 0) <= 0]
    win_rate = (len(wins) / n * 100.0) if n else 0.0
    avg_win, avg_loss = mean(wins), mean(losses)
    expectancy = (win_rate / 100.0 * avg_win) + ((100.0 - win_rate) / 100.0 * avg_loss) if n else 0.0
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    rules = {}
    for p in priced:
        rules.setdefault(p["reason"] or "unknown", []).append(p["ret"] or 0.0)

    lines = ["📊 <b>INTRADAY REPORT v7 (2x/3x ladder)</b>"]
    lines.append(f"🗂 Alerts(all-time): {alerts} | Legacy excluded: {legacy_excluded} | Priced: {n}")
    lines.append(f"🎯 Win rate: <b>{win_rate:.1f}%</b> ({len(wins)}W / {len(losses)}L)")
    lines.append(f"📈 Avg win: <b>{avg_win:+.1f}%</b> | 📉 Avg loss: <b>{avg_loss:+.1f}%</b>")
    lines.append(f"🧮 Expectancy / trade: <b>{expectancy:+.2f}%</b>")
    pf_txt = "∞" if pf == float("inf") else f"{pf:.2f}"
    lines.append(f"⚖️ Profit factor: <b>{pf_txt}</b> (target ≥ {GREEN_PF})")
    lines.append(f"🏔 Avg MFE: <b>{mean([p['mfe'] for p in priced]):+.1f}%</b>")
    lines.append("")
    lines.append("🧰 <b>Exit rule breakdown</b>:")
    if rules:
        for key, vals in sorted(rules.items(), key=lambda kv: mean(kv[1]), reverse=True):
            lines.append(f"   • {key}: n={len(vals)} | avg {mean(vals):+.1f}%")
    else:
        lines.append("   • (no priced closes yet in this cohort)")

    tp1_trades = [p for p in priced if p["tp1_hit"]]
    lines.append("")
    lines.append("🧪 <b>STOP-WIDTH SENSITIVITY (whipsaw tax on TP1 winners):</b>")
    if tp1_trades:
        for s in CANDIDATE_STOPS:
            killed = sum(1 for p in tp1_trades
                         if p["mae"] > 0 and p["mae"] <= p["entry"] * (1.0 - s))
            pct = killed / len(tp1_trades) * 100.0
            lines.append(f"   • Stop -{s * 100:.0f}%: kills {killed}/{len(tp1_trades)} winners ({pct:.0f}%)")
    else:
        lines.append("   • (no TP1 winners in cohort yet - keep collecting)")

    if unpriced:
        lines.append(f"⚠️ Unpriced closes in cohort: {unpriced}")
    if opens:
        live_open = [o for o in opens if o["mult"] is not None]
        lines.append(f"🔓 Open: {len(opens)}" + (f" | avg live {mean([o['mult'] for o in live_open]):.2f}x" if live_open else ""))
        for o in opens[:6]:
            lines.append(f"   • {o['symbol']}: " + (f"{o['mult']:.2f}x" if o["mult"] is not None else "n/a"))
    lines.append("")
    if n < MIN_SAMPLE:
        verdict = f"⏳ COHORT TOO SMALL ({n}/{MIN_SAMPLE}). Keep collecting."
    elif expectancy > 0 and (pf == float("inf") or pf >= GREEN_PF):
        verdict = "🟢 GREEN-LIGHT: PF ≥ 2.0 with positive expectancy."
    elif expectancy > 0:
        verdict = "🟡 MARGINAL: positive expectancy, PF below 2.0. Compress losses further."
    else:
        verdict = "🔴 NEGATIVE EXPECTANCY: do NOT go live."
    lines.append(f"<b>{verdict}</b>")

    report = "\n".join(lines)
    print("\n" + report.replace("<b>", "").replace("</b>", "") + "\n")
    send_telegram(report)
    log("✅ Report complete.")


if __name__ == "__main__":
    main()
