import requests
import os
import time
import json
from collections import defaultdict

# --- CONFIGURATION ---
ALCHEMY_API_KEY = os.getenv("ALCHEMY_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

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
    """Fetches transactions and counts Buys vs Sells per token."""
    all_transfers = []
    next_key = None
    page_count = 0
    
    while page_count < 5: # Limit to 5 pages (500 txs) for speed
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "alchemy_getAssetTransfers",
            "params": [{
                "fromBlock": "0x0", "toBlock": "latest", "category": ["erc20"],
                "fromAddress": wallet_address, "toAddress": wallet_address, # Get both buys and sells
                "maxCount": "0x64", "excludeZeroValue": True, "order": "asc"
            }]
        }
        if next_key: payload["params"][0]["pageKey"] = next_key
        
        try:
            url = f"https://base-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
            response = requests.post(url, json=payload, timeout=15).json()
            transfers = response.get("result", {}).get("transfers", [])
            all_transfers.extend(transfers)
            
            next_key = response.get("result", {}).get("pageKey")
            if not next_key or len(transfers) == 0: break
            page_count += 1
            time.sleep(0.2)
        except Exception as e:
            print(f"Error: {e}")
            break
            
    return all_transfers

def analyze_wallet(wallet_address):
    print(f"🔍 Analyzing {wallet_address[:10]}...")
    transfers = get_wallet_activity(wallet_address)
    
    if not transfers:
        return {"wallet": wallet_address, "verdict": "👶 NEWBIE (No History)", "score": 0}

    # Track activity per token
    token_activity = defaultdict({"buys": 0, "sells": 0})
    
    for tx in transfers:
        token = tx.get("rawContract", {}).get("address", "").lower()
        if not token: continue
        
        to_addr = tx.get("to", "").lower()
        from_addr = tx.get("from", "").lower()
        
        if to_addr == wallet_address.lower():
            token_activity[token]["buys"] += 1
        elif from_addr == wallet_address.lower():
            token_activity[token]["sells"] += 1

    # Calculate Score
    total_buys = sum(t["buys"] for t in token_activity.values())
    total_sells = sum(t["sells"] for t in token_activity.values())
    
    # A "Round Trip" is a Buy + Sell. High round trips = Active Trader.
    round_trips = sum(min(t["buys"], t["sells"]) for t in token_activity.values())
    unique_tokens = len(token_activity)

    # --- SCORING LOGIC ---
    if total_sells >= 10 and round_trips >= 5:
        verdict = " GOLDMINE (Active Trader)"
        score = 100
    elif total_sells >= 5 and round_trips >= 3:
        verdict = "✅ GOOD (Worth Monitoring)"
        score = 80
    elif total_buys > 10 and total_sells < 2:
        verdict = "🚩 BAG HOLDER (Buys but never sells)"
        score = 20
    elif total_buys < 5 and total_sells < 2:
        verdict = "👶 NEWBIE (Too little data)"
        score = 0
    else:
        verdict = "️ AVERAGE"
        score = 50

    return {
        "wallet": wallet_address,
        "verdict": verdict,
        "score": score,
        "buys": total_buys,
        "sells": total_sells,
        "round_trips": round_trips
    }

def send_report(results):
    report = " **WALLET PROFILER REPORT**\n\n"
    
    # Sort by score
    results.sort(key=lambda x: x["score"], reverse=True)
    
    for r in results:
        emoji = "💎" if r["score"] >= 80 else ("✅" if r["score"] >= 50 else "")
        report += f"{emoji} **{r['verdict']}**\n"
        report += f"   Wallet: `{r['wallet'][:8]}...{r['wallet'][-6:]}`\n"
        report += f"   Stats: {r['buys']} Buys | {r['sells']} Sells | {r['round_trips']} Round Trips\n\n"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": report, "parse_mode": "Markdown"})

def main():
    print("🚀 Starting Profiler...")
    results = []
    for wallet in WALLETS_TO_ANALYZE:
        results.append(analyze_wallet(wallet))
        time.sleep(1)
    
    send_report(results)
    print("✅ Done! Check Telegram.")

if __name__ == "__main__":
    main()
