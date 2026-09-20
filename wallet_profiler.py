import requests
import os
import time
import json
from collections import defaultdict

# --- CONFIGURATION ---
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# PASTE YOUR WALLET ADDRESSES HERE
WALLETS_TO_ANALYZE = [
    "0xbe6d8853ce2c4d14e2d904681760e329a8283869",  # Wallet 1
    "0x6aa989249e423b0f843a2b10cab9ea7eca41c7e8",  # Wallet 2
    "0xe09512e2d80abe8f6249a742f07e84b1b68f9b11",  # Wallet 3
    "0x6aa989249e423b0f843a2b10cab9ea7eca41c7e8",  # Wallet 4
    "0x9f54942b5ac21255b54469528857cbff4cf7f027",  # Wallet 5
    "0x498581ff718922c3f8e6a244956af099b2652b2b",  # Wallet 6
    "0x4d1b821a43fba502c4ea72fd78d2cd04bb97e9f9",  # Wallet 7
]

def get_wallet_activity(wallet_address):
    """Fetches transactions from Alchemy API."""
    all_transfers = []
    next_key = None
    page_count = 0
    
    while page_count < 5:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "alchemy_getAssetTransfers",
            "params": [{
                "fromBlock": "0x0",
                "toBlock": "latest",
                "category": ["erc20"],
                "fromAddress": wallet_address,
                "toAddress": wallet_address,
                "maxCount": "0x64",
                "excludeZeroValue": True,
                "order": "asc"
            }]
        }
        
        if next_key:
            payload["params"][0]["pageKey"] = next_key
        
        try:
            url = f"https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
            response = requests.post(url, json=payload, timeout=15).json()
            transfers = response.get("result", {}).get("transfers", [])
            all_transfers.extend(transfers)
            
            next_key = response.get("result", {}).get("pageKey")
            if not next_key or len(transfers) == 0:
                break
            page_count += 1
            time.sleep(0.2)
        except Exception as e:
            print(f"Error fetching: {e}")
            break
            
    return all_transfers

def analyze_wallet(wallet_address):
    """Analyzes wallet and returns stats."""
    print(f"🔍 Analyzing {wallet_address[:10]}...")
    transfers = get_wallet_activity(wallet_address)
    
    # Initialize with defaults
    result = {
        "wallet": wallet_address,
        "buys": 0,
        "sells": 0,
        "round_trips": 0,
        "verdict": "👶 NEWBIE (No History)",
        "score": 0
    }
    
    if not transfers:
        return result

    token_activity = defaultdict(lambda: {"buys": 0, "sells": 0})
    
    for tx in transfers:
        token = tx.get("rawContract", {}).get("address", "").lower()
        if not token:
            continue
        
        to_addr = tx.get("to", "").lower()
        from_addr = tx.get("from", "").lower()
        
        if to_addr == wallet_address.lower():
            token_activity[token]["buys"] += 1
        elif from_addr == wallet_address.lower():
            token_activity[token]["sells"] += 1

    total_buys = sum(t["buys"] for t in token_activity.values())
    total_sells = sum(t["sells"] for t in token_activity.values())
    round_trips = sum(min(t["buys"], t["sells"]) for t in token_activity.values())

    result["buys"] = total_buys
    result["sells"] = total_sells
    result["round_trips"] = round_trips

    # Scoring logic
    if total_sells >= 10 and round_trips >= 5:
        result["verdict"] = "💎 GOLDMINE (Active Trader)"
        result["score"] = 100
    elif total_sells >= 5 and round_trips >= 3:
        result["verdict"] = "✅ GOOD (Worth Monitoring)"
        result["score"] = 80
    elif total_buys > 10 and total_sells < 2:
        result["verdict"] = "🚩 BAG HOLDER (Buys but never sells)"
        result["score"] = 20
    elif total_buys < 5 and total_sells < 2:
        result["verdict"] = "👶 NEWBIE (Too little data)"
        result["score"] = 0
    else:
        result["verdict"] = "⚠️ AVERAGE"
        result["score"] = 50

    return result

def send_report(results):
    """Sends formatted report to Telegram."""
    report = "📊 **WALLET PROFILER REPORT**\n\n"
    
    results.sort(key=lambda x: x["score"], reverse=True)
    
    for r in results:
        emoji = "" if r["score"] >= 80 else ("✅" if r["score"] >= 50 else "")
        report += f"{emoji} **{r['verdict']}**\n"
        report += f"   Wallet: `{r['wallet'][:8]}...{r['wallet'][-6:]}`\n"
        report += f"   Stats: {r['buys']} Buys | {r['sells']} Sells | {r['round_trips']} Round Trips\n\n"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": report,
        "parse_mode": "Markdown"
    })

def main():
    print("🚀 Starting Profiler...")
    results = []
    
    for wallet in WALLETS_TO_ANALYZE:
        result = analyze_wallet(wallet)
        results.append(result)
        print(f"✅ {wallet[:10]}... - {result['verdict']}")
        time.sleep(1)
    
    send_report(results)
    print("✅ Done! Check Telegram.")

if __name__ == "__main__":
    main()
