import requests
import os
import time
from datetime import datetime
from collections import defaultdict

# --- CONFIGURATION ---
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# The 6 wallets you found (replace with actual addresses)
WALLETS_TO_ANALYZE = [
    "0x...",  # Wallet 1
    "0x...",  # Wallet 2
    "0x...",  # Wallet 3
    "0x...",  # Wallet 4
    "0x...",  # Wallet 5
    "0x...",  # Wallet 6
]

def get_wallet_transactions(wallet_address):
    """Fetches ALL ERC20 transfers for a wallet."""
    all_transfers = []
    next_key = None
    
    while True:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "alchemy_getAssetTransfers",
            "params": [{
                "fromBlock": "0x0",
                "toBlock": "latest",
                "category": ["erc20"],
                "fromAddress": wallet_address,
                "maxCount": "0x64",  # 100 per request
                "excludeZeroValue": True,
                "order": "asc"
            }]
        }
        
        if next_key:
            payload["params"][0]["pageKey"] = next_key
        
        url = f"https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
        response = requests.post(url, json=payload).json()
        
        transfers = response.get("result", {}).get("transfers", [])
        all_transfers.extend(transfers)
        
        next_key = response.get("result", {}).get("pageKey")
        if not next_key or len(transfers) < 100:
            break
        
        time.sleep(0.5)  # Rate limiting
    
    return all_transfers

def analyze_wallet(wallet_address):
    """Analyzes a wallet and returns detailed stats."""
    print(f"\n🔍 Analyzing {wallet_address}...")
    
    transfers = get_wallet_transactions(wallet_address)
    
    if not transfers:
        return {
            "wallet": wallet_address,
            "total_trades": 0,
            "status": "No history found"
        }
    
    # Track buys and sells
    token_positions = defaultdict(list)  # token -> list of (buy_time, buy_price, amount)
    completed_trades = []
    
    for tx in transfers:
        token = tx.get("asset", "").lower()
        to_addr = tx.get("to", "").lower()
        from_addr = tx.get("from", "").lower()
        timestamp = tx.get("blockTimestamp", "")
        value = tx.get("value", 0)
        
        # Determine if buy or sell
        if to_addr == wallet_address.lower():
            # This is a BUY (tokens coming TO wallet)
            token_positions[token].append({
                "type": "buy",
                "time": timestamp,
                "value": float(value) if value else 0
            })
        elif from_addr == wallet_address.lower():
            # This is a SELL (tokens going FROM wallet)
            if token in token_positions and token_positions[token]:
                buy_info = token_positions[token].pop(0)  # FIFO
                completed_trades.append({
                    "token": token,
                    "buy_time": buy_info["time"],
                    "sell_time": timestamp,
                    "buy_value": buy_info["value"],
                    "sell_value": float(value) if value else 0
                })
    
    # Calculate statistics
    total_trades = len(completed_trades)
    
    if total_trades == 0:
        return {
            "wallet": wallet_address,
            "total_trades": 0,
            "status": "No completed trades"
        }
    
    # Win/Loss analysis
    wins = 0
    losses = 0
    total_profit = 0
    total_hold_time = 0
    profitable_trades = 0
    
    for trade in completed_trades:
        profit = trade["sell_value"] - trade["buy_value"]
        total_profit += profit
        
        if profit > 0:
            wins += 1
            profitable_trades += 1
        else:
            losses += 1
        
        # Calculate hold time (simplified)
        try:
            buy_dt = datetime.fromisoformat(trade["buy_time"].replace("Z", "+00:00"))
            sell_dt = datetime.fromisoformat(trade["sell_time"].replace("Z", "+00:00"))
            hold_seconds = (sell_dt - buy_dt).total_seconds()
            total_hold_time += hold_seconds
        except:
            pass
    
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    avg_profit = total_profit / total_trades if total_trades > 0 else 0
    avg_hold_minutes = (total_hold_time / total_trades / 60) if total_trades > 0 else 0
    profit_factor = (wins / losses) if losses > 0 else wins
    
    # Determine wallet quality
    if win_rate >= 60 and avg_hold_minutes < 60 and profit_factor > 1.5:
        quality = "💎 GOLDMINE"
    elif win_rate >= 50:
        quality = "✅ GOOD"
    elif win_rate >= 40:
        quality = "️ AVERAGE"
    else:
        quality = "❌ AVOID"
    
    return {
        "wallet": wallet_address,
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 2),
        "total_profit_usd": round(total_profit, 2),
        "avg_profit_per_trade": round(avg_profit, 2),
        "avg_hold_minutes": round(avg_hold_minutes, 2),
        "profit_factor": round(profit_factor, 2),
        "quality": quality,
        "status": "Active"
    }

def send_analysis_report(results):
    """Sends a detailed report to Telegram."""
    report = "📊 **WALLET PROFILER REPORT**\n\n"
    
    # Sort by quality
    goldmines = [r for r in results if "GOLDMINE" in r.get("quality", "")]
    good = [r for r in results if "GOOD" in r.get("quality", "")]
    average = [r for r in results if "AVERAGE" in r.get("quality", "")]
    avoid = [r for r in results if "AVOID" in r.get("quality", "")]
    
    if goldmines:
        report += "💎 **GOLDMINES** (Follow These!):\n"
        for r in goldmines:
            report += f"• `{r['wallet'][:10]}...`\n"
            report += f"  Win Rate: {r['win_rate']}% | Trades: {r['total_trades']}\n"
            report += f"  Avg Profit: ${r['avg_profit_per_trade']} | Hold: {r['avg_hold_minutes']}min\n\n"
    
    if good:
        report += "✅ **GOOD** (Consider Following):\n"
        for r in good:
            report += f"• `{r['wallet'][:10]}...` - {r['win_rate']}% WR\n"
    
    if average:
        report += "\n️ **AVERAGE** (Monitor Only):\n"
        for r in average:
            report += f"• `{r['wallet'][:10]}...` - {r['win_rate']}% WR\n"
    
    if avoid:
        report += "\n❌ **AVOID** (Low Quality):\n"
        for r in avoid:
            report += f"• `{r['wallet'][:10]}...` - {r['win_rate']}% WR\n"
    
    report += f"\n📈 **Total Analyzed:** {len(results)} wallets"
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": report,
        "parse_mode": "Markdown"
    })

def main():
    print("🚀 Starting Wallet Profiler...")
    results = []
    
    for wallet in WALLETS_TO_ANALYZE:
        stats = analyze_wallet(wallet)
        results.append(stats)
        print(f"✅ {stats['wallet'][:10]}... - {stats.get('quality', 'N/A')}")
        time.sleep(2)  # Rate limiting
    
    # Send report
    send_analysis_report(results)
    
    # Save to file for reference
    with open("wallet_analysis.json", "w") as f:
        import json
        json.dump(results, f, indent=2)
    
    print("\n✅ Analysis complete! Check Telegram for report.")

if __name__ == "__main__":
    main()
