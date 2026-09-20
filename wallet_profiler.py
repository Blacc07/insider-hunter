import requests
import os
import time
import json
from datetime import datetime
from collections import defaultdict

# --- CONFIGURATION ---
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# The 7 wallets you found (replace with actual addresses)
WALLETS_TO_ANALYZE = [
    "0xbe6d8853ce2c4d14e2d904681760e329a8283869",  # Wallet 1
    "0x6aa989249e423b0f843a2b10cab9ea7eca41c7e8",  # Wallet 2
    "0xe09512e2d80abe8f6249a742f07e84b1b68f9b11",  # Wallet 3
    "0x6aa989249e423b0f843a2b10cab9ea7eca41c7e8",  # Wallet 4
    "0x9f54942b5ac21255b54469528857cbff4cf7f027",  # Wallet 5
    "0x498581ff718922c3f8e6a244956af099b2652b2b",  # Wallet 6
    "0x4d1b821a43fba502c4ea72fd78d2cd04bb97e9f9",  # Wallet 7
]


MAX_TRANSACTIONS = 1000  # SAFETY LIMIT: Max transactions to analyze per wallet
MAX_PAGES = 10  # SAFETY LIMIT: Max API pages to fetch

def get_wallet_transactions(wallet_address):
    """Fetches ERC20 transfers with SAFETY LIMITS."""
    all_transfers = []
    next_key = None
    page_count = 0
    
    print(f"  📥 Fetching transactions for {wallet_address[:10]}...")
    
    while page_count < MAX_PAGES:
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
        
        try:
            url = f"https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
            response = requests.post(url, json=payload, timeout=30).json()
            
            transfers = response.get("result", {}).get("transfers", [])
            all_transfers.extend(transfers)
            
            print(f"    Page {page_count + 1}: Found {len(transfers)} transactions (Total: {len(all_transfers)})")
            
            # SAFETY: Stop if we hit the transaction limit
            if len(all_transfers) >= MAX_TRANSACTIONS:
                print(f"  ️ Reached {MAX_TRANSACTIONS} transaction limit, stopping...")
                break
            
            next_key = response.get("result", {}).get("pageKey")
            if not next_key or len(transfers) == 0:
                print(f"  ✅ No more pages to fetch")
                break
            
            page_count += 1
            time.sleep(0.3)  # Rate limiting
            
        except Exception as e:
            print(f"  ❌ Error fetching page {page_count}: {e}")
            break
    
    return all_transfers[:MAX_TRANSACTIONS]

def analyze_wallet(wallet_address):
    """Analyzes a wallet and returns detailed stats."""
    print(f"\n🔍 Analyzing {wallet_address}...")
    
    try:
        transfers = get_wallet_transactions(wallet_address)
    except Exception as e:
        print(f"  ❌ Failed to fetch transactions: {e}")
        return {
            "wallet": wallet_address,
            "total_trades": 0,
            "status": f"Error: {str(e)}"
        }
    
    if not transfers:
        print(f"  ⚠️ No transactions found")
        return {
            "wallet": wallet_address,
            "total_trades": 0,
            "status": "No history found"
        }
    
    print(f"  📊 Processing {len(transfers)} transactions...")
    
    # Track buys and sells
    token_positions = defaultdict(list)
    completed_trades = []
    
    for tx in transfers:
        token = tx.get("asset", "").lower()
        to_addr = tx.get("to", "").lower()
        from_addr = tx.get("from", "").lower()
        timestamp = tx.get("blockTimestamp", "")
        value = tx.get("value", 0)
        
        if to_addr == wallet_address.lower():
            token_positions[token].append({
                "type": "buy",
                "time": timestamp,
                "value": float(value) if value else 0
            })
        elif from_addr == wallet_address.lower():
            if token in token_positions and token_positions[token]:
                buy_info = token_positions[token].pop(0)
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
    
    wins = 0
    losses = 0
    total_profit = 0
    total_hold_time = 0
    
    for trade in completed_trades:
        profit = trade["sell_value"] - trade["buy_value"]
        total_profit += profit
        
        if profit > 0:
            wins += 1
        else:
            losses += 1
        
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
        report += "\n⚠️ **AVERAGE** (Monitor Only):\n"
        for r in average:
            report += f"• `{r['wallet'][:10]}...` - {r['win_rate']}% WR\n"
    
    if avoid:
        report += "\n **AVOID** (Low Quality):\n"
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
    print(f"⏱️  Safety limit: {MAX_TRANSACTIONS} transactions max per wallet\n")
    
    results = []
    
    for i, wallet in enumerate(WALLETS_TO_ANALYZE, 1):
        print(f"\n{'='*60}")
        print(f"Wallet {i}/{len(WALLETS_TO_ANALYZE)}")
        print('='*60)
        
        stats = analyze_wallet(wallet)
        results.append(stats)
        
        print(f"\n✅ {stats['wallet'][:10]}... - {stats.get('quality', 'N/A')}")
        print(f"   Trades: {stats.get('total_trades', 0)} | Win Rate: {stats.get('win_rate', 0)}%")
        
        time.sleep(1)  # Rate limiting between wallets
    
    print("\n" + "="*60)
    print("📤 Sending report to Telegram...")
    send_analysis_report(results)
    
    with open("wallet_analysis.json", "w") as f:
        json.dump(results, f, indent=2)
    
    print("\n✅ Analysis complete! Check Telegram for report.")

if __name__ == "__main__":
    main()
